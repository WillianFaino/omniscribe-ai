"""Motor de Transcrição e Síntese 100% Offline do OmniScribe AI.

Pipeline local sem dependência de LLM em nuvem:
1. ASR local via `sherpa-onnx` OfflineRecognizer (Whisper `small` ou `base` INT8 ONNX);
2. Segmentação de fala via Silero VAD + identificação biométrica via VoiceprintService
   (separação física L/R em Estéreo Online e clusterização acústica em Mono Presencial);
3. Geração da Ata Executiva via Ollama local (quando ativo em localhost:11434) ou
   sintetizador determinístico estruturado em Markdown.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tarfile
import threading
import urllib.error
import urllib.request
import wave
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from audio_engine import TARGET_SAMPLE_RATE, resample_to_16k
from voiceprint_service import (
    DEFAULT_MATCH_THRESHOLD,
    DEFAULT_OVERLAP_MARGIN,
    VoiceprintService,
    _to_float32_mono,
)

logger = logging.getLogger("omniscribe.offline_asr")

WHISPER_MODELS: Dict[str, Dict[str, str]] = {
    "small": {
        "label": "Whisper Small INT8 (~250 MB • Melhor PT-BR)",
        "dir_name": "sherpa-onnx-whisper-small",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-small.tar.bz2",
        "prefix": "small",
    },
    "base": {
        "label": "Whisper Base INT8 (~110 MB • Mais leve/rápido)",
        "dir_name": "sherpa-onnx-whisper-base",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-base.tar.bz2",
        "prefix": "base",
    },
}

_WHISPER_NOISE_RE = re.compile(
    r"^(?:\[.*\]|\(.*\)|legendas?\b.*|obrigad[oa] por assistir[!.]*|\.{2,}|\s*)$",
    flags=re.IGNORECASE,
)
_TURN_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s+\*\*(.+?)\*\*:\s*(.*)$")
_DECISION_CUES = (
    "decid", "definid", "combinad", "fechad", "aprovad", "acordad", "vamos ",
    "ficou ", "precisa", "prazo", "entregar", "amanhã", "segunda", "terça",
    "quarta", "quinta", "sexta", "deploy", "corrigir", "criar", "subir",
)


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _format_mmss(seconds: float) -> str:
    secs = int(max(0, round(seconds)))
    hours = secs // 3600
    mins = (secs % 3600) // 60
    rem = secs % 60
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{rem:02d}"
    return f"{mins:02d}:{rem:02d}"


def _clean_whisper_text(raw_text: str) -> str:
    text = re.sub(r"\s+", " ", (raw_text or "")).strip()
    if not text or _WHISPER_NOISE_RE.match(text):
        return ""
    # Colapsa alucinações curtas repetidas do Whisper (ex.: "Obrigado. Obrigado. Obrigado.")
    text = re.sub(r"(\b[\wÀ-ÿ]{2,20}[.!?]?)(?:\s+\1){3,}", r"\1", text, flags=re.IGNORECASE)
    return text.strip()


def _safe_extract_tar_bz2(archive_path: Path, dest_root: Path) -> None:
    dest_resolved = dest_root.resolve()
    with tarfile.open(str(archive_path), "r:bz2") as tf:
        for member in tf.getmembers():
            target = (dest_resolved / member.name).resolve()
            if not target.is_relative_to(dest_resolved):
                raise RuntimeError(f"Entrada inválida no arquivo tar: {member.name}")
        tf.extractall(path=str(dest_resolved))


def list_ollama_models(base_url: str = "http://127.0.0.1:11434", timeout: float = 1.2) -> Tuple[bool, List[str]]:
    """Verifica rapidamente se o servidor Ollama local está ativo e lista modelos de chat disponíveis."""
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        models = []
        for entry in data.get("models", []):
            name = str(entry.get("name", "")).strip()
            low = name.lower()
            if name and not any(tok in low for tok in ("embed", "nomic", "mxbai", "bge-")):
                models.append(name)
        return True, models
    except Exception:
        return False, []


class OfflineAsrService:
    """Serviço de reconhecimento de fala, diarização local e geração de atas sem API em nuvem."""

    def __init__(
        self,
        models_dir: Optional[Path] = None,
        voiceprint_service: Optional[VoiceprintService] = None,
        default_variant: str = "small",
    ):
        base_dir = _get_base_dir()
        if models_dir:
            self.models_dir = Path(models_dir)
        else:
            cand = base_dir / "models" / "asr"
            meipass = getattr(sys, "_MEIPASS", None)
            if not cand.exists() and meipass and (Path(meipass) / "models" / "asr").exists():
                cand = Path(meipass) / "models" / "asr"
            self.models_dir = cand
        self.voiceprint_service = voiceprint_service or VoiceprintService()
        self.default_variant = default_variant if default_variant in WHISPER_MODELS else "small"

        self._lock = threading.Lock()
        self._recognizer = None
        self._loaded_variant: Optional[str] = None
        self._sherpa_onnx = None

    def _ensure_sherpa(self):
        if self._sherpa_onnx is None:
            import sherpa_onnx as _so
            self._sherpa_onnx = _so
        return self._sherpa_onnx

    def _resolve_model_files(self, variant: str) -> Optional[Tuple[Path, Path, Path]]:
        spec = WHISPER_MODELS.get(variant, WHISPER_MODELS["small"])
        model_dir = self.models_dir / spec["dir_name"]
        if not model_dir.exists():
            return None

        prefix = spec["prefix"]
        enc_cands = [
            model_dir / f"{prefix}-encoder.int8.onnx",
            model_dir / f"{prefix}-encoder.onnx",
        ] + sorted(model_dir.rglob("*encoder*.onnx"))
        dec_cands = [
            model_dir / f"{prefix}-decoder.int8.onnx",
            model_dir / f"{prefix}-decoder.onnx",
        ] + sorted(model_dir.rglob("*decoder*.onnx"))
        tok_cands = [
            model_dir / f"{prefix}-tokens.txt",
        ] + sorted(model_dir.rglob("*tokens*.txt"))

        enc = next((p for p in enc_cands if p.is_file() and p.stat().st_size > 10000), None)
        dec = next((p for p in dec_cands if p.is_file() and p.stat().st_size > 10000), None)
        tok = next((p for p in tok_cands if p.is_file() and p.stat().st_size > 100), None)
        if enc and dec and tok:
            return enc, dec, tok
        return None

    def is_ready(self, variant: Optional[str] = None) -> Tuple[bool, str]:
        """Verifica se `sherpa-onnx` e os pesos Whisper INT8 estão disponíveis localmente."""
        var = variant if variant in WHISPER_MODELS else self.default_variant
        try:
            self._ensure_sherpa()
        except Exception as e:
            return False, f"Dependência sherpa-onnx indisponível: {e}"

        files = self._resolve_model_files(var)
        if files is None:
            return False, f"Modelo offline Whisper ({var}) ainda não baixado em {self.models_dir}."
        return True, f"Whisper ({var}) pronto para uso 100% offline."

    def ensure_models_ready(
        self,
        variant: Optional[str] = None,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> Tuple[bool, str]:
        """Garante download dos pesos Whisper ONNX e dos modelos VAD/Voiceprint locais."""
        var = variant if variant in WHISPER_MODELS else self.default_variant
        spec = WHISPER_MODELS[var]
        self.models_dir.mkdir(parents=True, exist_ok=True)

        # 1. Garante Silero VAD + WeSpeaker (usados na segmentação e diarização local)
        if self.voiceprint_service:
            ok_vp, msg_vp = self.voiceprint_service.ensure_models_downloaded(progress_cb=progress_cb)
            if not ok_vp:
                logger.warning(f"Aviso ao baixar modelos auxiliares VAD/Voiceprint: {msg_vp}")

        # 2. Baixa e extrai Whisper ONNX se ainda não existir
        if self._resolve_model_files(var) is not None:
            return True, f"Modelos offline Whisper ({var}) já estão prontos."

        tmp_archive = self.models_dir / f".{spec['dir_name']}.tar.bz2.tmp"
        extract_tmp = self.models_dir / f".extract_{spec['dir_name']}"
        target_dir = self.models_dir / spec["dir_name"]

        try:
            if progress_cb:
                progress_cb(f"Baixando Whisper {var} ONNX ({spec['label']})...", 0.05)

            req = urllib.request.Request(spec["url"], headers={"User-Agent": "OmniScribeAI/2.0"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(tmp_archive, "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb and total > 0:
                            frac = min(0.85, 0.05 + 0.80 * (downloaded / float(total)))
                            mb_done = downloaded / (1024 * 1024)
                            mb_tot = total / (1024 * 1024)
                            progress_cb(f"Baixando Whisper {var} ({mb_done:.1f}/{mb_tot:.1f} MB)...", frac)

            if progress_cb:
                progress_cb(f"Extraindo modelo Whisper {var}...", 0.90)

            if extract_tmp.exists():
                shutil.rmtree(extract_tmp, ignore_errors=True)
            extract_tmp.mkdir(parents=True, exist_ok=True)
            _safe_extract_tar_bz2(tmp_archive, extract_tmp)

            # Move subpasta extraída para `models/asr/sherpa-onnx-whisper-<var>`
            sub_dir = extract_tmp / spec["dir_name"]
            if not sub_dir.exists():
                subdirs = [d for d in extract_tmp.iterdir() if d.is_dir()]
                sub_dir = subdirs[0] if subdirs else extract_tmp

            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            shutil.move(str(sub_dir), str(target_dir))

            if self._resolve_model_files(var) is None:
                return False, f"Arquivos Whisper inválidos após extração em {target_dir}."

            if progress_cb:
                progress_cb(f"Whisper {var} pronto para uso offline!", 1.0)
            return True, f"Modelo offline Whisper ({var}) baixado e validado com sucesso."

        except Exception as e:
            logger.exception(f"Falha ao baixar/extrair Whisper {var}: {e}")
            return False, f"Falha ao preparar Whisper {var}: {e}"
        finally:
            tmp_archive.unlink(missing_ok=True)
            if extract_tmp.exists():
                shutil.rmtree(extract_tmp, ignore_errors=True)

    def _ensure_recognizer(self, variant: Optional[str] = None):
        var = variant if variant in WHISPER_MODELS else self.default_variant
        with self._lock:
            if self._recognizer is not None and self._loaded_variant == var:
                return self._recognizer

            files = self._resolve_model_files(var)
            if files is None:
                raise RuntimeError(f"Modelo Whisper '{var}' não encontrado em {self.models_dir}.")

            so = self._ensure_sherpa()
            enc, dec, tok = files
            threads = min(4, max(2, os.cpu_count() or 2))
            self._recognizer = so.OfflineRecognizer.from_whisper(
                encoder=str(enc),
                decoder=str(dec),
                tokens=str(tok),
                language="pt",
                task="transcribe",
                num_threads=threads,
                decoding_method="greedy_search",
            )
            self._loaded_variant = var
            return self._recognizer

    def _transcribe_samples(self, samples_f32: np.ndarray, variant: Optional[str] = None) -> str:
        if len(samples_f32) < int(0.25 * TARGET_SAMPLE_RATE):
            return ""
        rec = self._ensure_recognizer(variant)
        with self._lock:
            stream = rec.create_stream()
            stream.accept_waveform(TARGET_SAMPLE_RATE, samples_f32.astype(np.float32))
            rec.decode_stream(stream)
            raw_txt = getattr(stream.result, "text", "") or ""
        return _clean_whisper_text(raw_txt)

    def _segment_channel(self, mono_f32: np.ndarray, sample_rate: int) -> List[Tuple[float, float, np.ndarray]]:
        """Extrai segmentos de fala (~<=15s) via Silero VAD ou janelas fixas de fallback se VAD indisponível."""
        if sample_rate != TARGET_SAMPLE_RATE:
            pcm_i16 = np.clip(mono_f32 * 32767.0, -32768, 32767).astype(np.int16)
            mono_f32 = _to_float32_mono(resample_to_16k(pcm_i16, sample_rate, TARGET_SAMPLE_RATE))

        if len(mono_f32) == 0:
            return []

        if self.voiceprint_service:
            try:
                segs = self.voiceprint_service.extract_speech_segments(mono_f32, TARGET_SAMPLE_RATE)
                if segs:
                    return segs
            except Exception as e:
                logger.debug(f"VAD fallback por janelas: {e}")

        # Fallback por energia quando Silero VAD não detectar trechos ou estiver ausente
        rms = float(np.sqrt(np.mean(mono_f32 * mono_f32)))
        if rms < 0.005:
            return []

        win = 18 * TARGET_SAMPLE_RATE
        out: List[Tuple[float, float, np.ndarray]] = []
        for pos in range(0, len(mono_f32), win):
            sub = mono_f32[pos : pos + win]
            if len(sub) >= int(0.4 * TARGET_SAMPLE_RATE):
                out.append((pos / float(TARGET_SAMPLE_RATE), (pos + len(sub)) / float(TARGET_SAMPLE_RATE), sub))
        return out

    def _identify_speaker(
        self,
        seg_f32: np.ndarray,
        unknown_clusters: List[Tuple[str, np.ndarray, int]],
    ) -> str:
        if not self.voiceprint_service or not self.voiceprint_service.vad_model_path.exists():
            return "Participante 1"
        try:
            if not self.voiceprint_service.embedding_model_path.exists():
                return "Participante 1"
            emb = self.voiceprint_service.compute_embedding(seg_f32, TARGET_SAMPLE_RATE)
            if emb is None:
                return "Participante 1"
            raw_lbl = self.voiceprint_service._match_embedding(
                emb, unknown_clusters, DEFAULT_MATCH_THRESHOLD, DEFAULT_OVERLAP_MARGIN
            )
            return raw_lbl.split(" (score ")[0].strip()
        except Exception:
            return "Participante 1"

    def transcribe_wav_block(
        self,
        wav_path: Path,
        block_offset_seconds: float = 0.0,
        variant: Optional[str] = None,
        session_unknown_clusters: Optional[List[Tuple[str, np.ndarray, int]]] = None,
    ) -> str:
        """Transcreve um bloco WAV (1ch Mono ou 2ch Estéreo L/R) 100% offline com diarização."""
        wav_path = Path(wav_path)
        with wave.open(str(wav_path), "rb") as wf:
            nchannels = int(wf.getnchannels())
            sampwidth = int(wf.getsampwidth())
            sr = int(wf.getframerate())
            frames = int(wf.getnframes())
            if sampwidth != 2 or frames == 0 or sr <= 0:
                return "*[Silêncio / Sem fala identificada no período]*"
            raw = wf.readframes(frames)

        arr = np.frombuffer(raw, dtype=np.int16)
        clusters = session_unknown_clusters if session_unknown_clusters is not None else []
        events: List[Tuple[float, float, str, str]] = []  # (start_abs, end_abs, speaker, text)

        if nchannels >= 2:
            stereo = arr.reshape(-1, nchannels)
            left_f = _to_float32_mono(stereo[:, 0])
            right_f = _to_float32_mono(stereo[:, 1])

            for s_t, e_t, seg in self._segment_channel(right_f, sr):
                txt = self._transcribe_samples(seg, variant=variant)
                if txt:
                    events.append((block_offset_seconds + s_t, block_offset_seconds + e_t, "Willian (Você)", txt))

            for s_t, e_t, seg in self._segment_channel(left_f, sr):
                txt = self._transcribe_samples(seg, variant=variant)
                if txt:
                    spk = self._identify_speaker(seg, clusters)
                    events.append((block_offset_seconds + s_t, block_offset_seconds + e_t, spk, txt))
        else:
            mono_f = _to_float32_mono(arr)
            for s_t, e_t, seg in self._segment_channel(mono_f, sr):
                txt = self._transcribe_samples(seg, variant=variant)
                if txt:
                    spk = self._identify_speaker(seg, clusters)
                    if spk.endswith("(Você)"):
                        spk = "Willian"
                    events.append((block_offset_seconds + s_t, block_offset_seconds + e_t, spk, txt))

        if not events:
            return "*[Silêncio / Sem fala identificada no período]*"

        events.sort(key=lambda item: item[0])

        # Agrupa falas simultâneas L+R ou turnos sobrepostos no mesmo intervalo
        lines: List[str] = []
        idx = 0
        while idx < len(events):
            s_a, e_a, spk_a, txt_a = events[idx]
            if idx + 1 < len(events):
                s_b, e_b, spk_b, txt_b = events[idx + 1]
                overlap_sec = min(e_a, e_b) - max(s_a, s_b)
                if spk_a != spk_b and (overlap_sec >= 0.35 or abs(s_a - s_b) <= 0.6):
                    lines.append(f"[{_format_mmss(s_a)}] ⚡ [Falas Sobrepostas / Interrupção]:")
                    lines.append(f"  - **{spk_a}**: {txt_a}")
                    lines.append(f"  - **{spk_b}**: {txt_b}")
                    idx += 2
                    continue

            lines.append(f"[{_format_mmss(s_a)}] **{spk_a}**: {txt_a}")
            idx += 1

        return "\n".join(lines)

    def generate_offline_minutes(
        self,
        title: str,
        date_str: str,
        duration_str: str,
        transcript_md: str,
        minutes_prompt_template: str,
        preferred_ollama_model: str = "",
    ) -> Tuple[str, str]:
        """Gera Ata Executiva via Ollama local (se disponível) ou síntese estruturada determinística."""
        ok_ollama, models = list_ollama_models()
        if ok_ollama and models:
            chosen = preferred_ollama_model.strip()
            if not chosen or chosen not in models:
                chosen = next(
                    (
                        m for m in models
                        if any(k in m.lower() for k in ("qwen", "llama", "gemma", "mistral", "phi"))
                    ),
                    models[0],
                )
            try:
                prompt = minutes_prompt_template.format(
                    title=title,
                    date_str=date_str,
                    duration_str=duration_str,
                    full_transcript=transcript_md,
                )
                payload = json.dumps(
                    {
                        "model": chosen,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.2},
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    "http://127.0.0.1:11434/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read().decode("utf-8", errors="replace"))
                txt = str(body.get("response", "")).strip()
                if txt and len(txt) > 80:
                    return txt, f"Ollama Local ({chosen})"
            except Exception as e:
                logger.warning(f"Falha ao sintetizar Ata via Ollama ({chosen}), usando síntese local: {e}")

        return self._synthesize_deterministic_minutes(title, date_str, duration_str, transcript_md), "Síntese Local Estruturada"

    @staticmethod
    def _synthesize_deterministic_minutes(title: str, date_str: str, duration_str: str, transcript_md: str) -> str:
        """Gera Ata Executiva organizada em Markdown 100% offline sem LLM externa."""
        speaker_turns: Counter = Counter()
        speaker_words: Counter = Counter()
        block_highlights: List[Tuple[str, List[str]]] = []
        current_block_title = "Bloco Principal"
        current_items: List[str] = []
        decisions: List[str] = []
        questions: List[str] = []

        for raw in (transcript_md or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("### Bloco"):
                if current_items:
                    block_highlights.append((current_block_title, current_items))
                current_block_title = line.lstrip("# ").strip()
                current_items = []
                continue

            m = _TURN_RE.match(line)
            if not m:
                continue
            ts, spk, txt = m.group(1), m.group(2).strip(), m.group(3).strip()
            speaker_turns[spk] += 1
            speaker_words[spk] += len(txt.split())

            if len(current_items) < 5 and len(txt) >= 12:
                excerpt = txt if len(txt) <= 180 else txt[:177].rstrip() + "..."
                current_items.append(f"`[{ts}]` **{spk}:** {excerpt}")

            low = txt.lower()
            if any(cue in low for cue in _DECISION_CUES) and len(decisions) < 12 and len(txt) >= 15:
                decisions.append(f"- `[{ts}]` **{spk}:** {txt}")
            if "?" in txt and len(questions) < 10 and len(txt) >= 12:
                questions.append(f"- `[{ts}]` **{spk}:** {txt}")

        if current_items:
            block_highlights.append((current_block_title, current_items))

        speakers_list = ", ".join(speaker_turns.keys()) if speaker_turns else "Não identificados"

        out = [
            f"# Ata Executiva da Reunião: {title}",
            f"**Data:** {date_str}",
            f"**Duração Total:** {duration_str}",
            f"**Modo de Processamento:** 100% Offline (Whisper ONNX + Síntese Local)",
            f"**Participantes Identificados:** {speakers_list}",
            "",
            "---",
            "",
            "## 1. Resumo Executivo",
            (
                f"Reunião **{title}** processada integralmente em modo offline, totalizando **{duration_str}** "
                f"de gravação e **{sum(speaker_turns.values())}** turnos de fala registrados."
            ),
            "",
            "## 2. Participação por Locutor",
        ]
        if speaker_turns:
            for spk, count in speaker_turns.most_common():
                out.append(f"- **{spk}**: {count} falas (~{speaker_words[spk]} palavras)")
        else:
            out.append("- Nenhum turno estruturado detectado.")

        out.extend(["", "## 3. Linha do Tempo e Destaques por Bloco"])
        if block_highlights:
            for b_title, items in block_highlights:
                out.append(f"### {b_title}")
                for it in items:
                    out.append(f"- {it}")
        else:
            out.append("- Sem destaques textuais disponíveis.")

        out.extend(["", "## 4. Possíveis Decisões e Direcionamentos Identificados"])
        out.extend(decisions if decisions else ["- Nenhum marcador explícito de decisão detectado na transcrição."])

        out.extend(["", "## 5. Perguntas e Pontos em Aberto"])
        out.extend(questions if questions else ["- Nenhuma pergunta aberta identificada."])
        out.append("")
        return "\n".join(out)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gerenciador de modelos offline OmniScribe AI")
    parser.add_argument("--prepare", choices=list(WHISPER_MODELS.keys()), default="small")
    args = parser.parse_args()

    svc = OfflineAsrService(default_variant=args.prepare)
    ok, msg = svc.ensure_models_ready(
        variant=args.prepare,
        progress_cb=lambda m, p: print(f"[{int(p * 100):3d}%] {m}", flush=True),
    )
    print(f"{'[OK]' if ok else '[ERRO]'} {msg}")
    sys.exit(0 if ok else 1)
