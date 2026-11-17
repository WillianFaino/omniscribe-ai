"""Motor de Transcrição e Síntese 100% Offline do Escutaí.

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
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import unicodedata

import numpy as np

from audio_engine import TARGET_SAMPLE_RATE, enhance_speech_audio, resample_to_16k
from voiceprint_service import (
    DEFAULT_MATCH_THRESHOLD,
    DEFAULT_OVERLAP_MARGIN,
    VoiceprintService,
    _to_float32_mono,
)

logger = logging.getLogger("escutai.offline_asr")

DENOISER_MODEL_NAME = "gtcrn_simple.onnx"
DENOISER_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speech-enhancement-models/gtcrn_simple.onnx"
)

WHISPER_MODELS: Dict[str, Dict[str, str]] = {
    "small": {
        "label": "Whisper Small INT8 (~250 MB • Equilibrado PT-BR)",
        "dir_name": "sherpa-onnx-whisper-small",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-small.tar.bz2",
        "prefix": "small",
    },
    "turbo": {
        "label": "Whisper Large-v3 Turbo INT8 (~538 MB • Máxima Precisão PT-BR)",
        "dir_name": "sherpa-onnx-whisper-turbo",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-turbo.tar.bz2",
        "prefix": "turbo",
    },
    "base": {
        "label": "Whisper Base INT8 (~110 MB • Mais leve/rápido)",
        "dir_name": "sherpa-onnx-whisper-base",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-base.tar.bz2",
        "prefix": "base",
    },
}

_WHISPER_NOISE_RE = re.compile(
    r"^(?:\[.*\]|\(.*\)|legendas?\b.*|obrigad[oa] por assistir[!.]*|\.{2,}|\s*|"
    r"([a-zA-ZÀ-ÿ])\1{4,}[.!?]*)$",
    flags=re.IGNORECASE,
)
_TURN_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s+\*\*(.+?)\*\*:\s*(.*)$")
_DECISION_CUES = (
    "decid", "definid", "combinad", "fechad", "aprovad", "acordad", "vamos ",
    "ficou ", "precisa", "prazo", "entregar", "amanhã", "segunda", "terça",
    "quarta", "quinta", "sexta", "deploy", "corrigir", "criar", "subir",
)
_BUILTIN_VOCABULARY = (
    "Escutaí",
    "OmniScribe",
    "Vozito",
    "ClickUp",
    "Bizagi",
    "BPMN",
    "WASAPI",
    "Voiceprint",
    "Whisper",
    "Gemini",
    "Ollama",
    "Power BI",
    "Controlsul",
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
    # Remove marcadores fechados ou truncados (ex.: "[MÚSICA", "[Som de", "(risos)", "[aplausos]")
    text = re.sub(
        r"\s*[\(\[](?:risos?|m[úu]sica|aplausos?|sil[êe]ncio|som|barulho|ru[íi]do|music|laughter|applause|noise|sound)(?:\s+[^\]\)]{1,30})?(?:[\)\]]|$)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    # Colapsa alongamentos absurdos (ex.: "Eeeeeeeeeeeeeeeee" -> vazio se isolado)
    text = re.sub(r"\b([a-zA-ZÀ-ÿ])\1{4,}[.!?,;]*\b", "", text, flags=re.IGNORECASE)
    # Colapsa alucinações curtas repetidas do Whisper (ex.: "Obrigado. Obrigado. Obrigado.")
    text = re.sub(r"(\b[\wÀ-ÿ]{2,20}[.!?]?)(?:\s+\1){3,}", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,;-")
    if not text or (len(text) <= 2 and not text.isalnum()) or _WHISPER_NOISE_RE.match(text):
        return ""
    return text


def _phonetic_norm(value: str) -> str:
    """Normaliza uma palavra/expressão para esqueleto fonético PT-BR."""
    raw = unicodedata.normalize("NFD", value or "")
    stripped = "".join(c for c in raw if unicodedata.category(c) != "Mn").lower()
    stripped = re.sub(r"[^a-z0-9]", "", stripped)
    if not stripped:
        return ""
    stripped = re.sub(r"^h", "", stripped)
    stripped = stripped.replace("ph", "f").replace("ch", "x").replace("sh", "x")
    stripped = stripped.replace("qu", "k").replace("ck", "k").replace("y", "i").replace("w", "u")
    stripped = stripped.replace("c", "k").replace("ss", "s").replace("sc", "s").replace("z", "s")
    stripped = re.sub(r"(.)\1+", r"\1", stripped)
    return stripped


def _extract_glossary_terms(context_notes: str = "", enrolled_names: Optional[List[str]] = None) -> List[str]:
    """Constrói lista ordenada de termos prioritários para ancoragem pós-ASR."""
    ignored = {"participantes", "participante", "termos", "termo", "glossario", "glossário", "reuniao", "reunião"}
    terms: List[str] = []
    seen_low = set()

    def _add(item: str) -> None:
        cleaned = re.sub(r"\s+", " ", (item or "")).strip(" .,:;-•")
        if len(cleaned) < 4 or cleaned.lower() in ignored or cleaned.lower() in seen_low:
            return
        seen_low.add(cleaned.lower())
        terms.append(cleaned)

    for nm in (enrolled_names or []):
        _add(nm)
        for part in nm.split():
            if len(part) >= 4:
                _add(part)

    if context_notes:
        for seg in re.split(r"[,;\n/|]+", context_notes):
            clean_seg = re.sub(r"^(?:participantes?|termos?|gloss[áa]rio|nomes?)\s*:\s*", "", seg.strip(), flags=re.IGNORECASE)
            _add(clean_seg)
            for tok in re.findall(r"[A-Za-zÀ-ÿ0-9_\-]{4,}", clean_seg):
                _add(tok)

    for builtin in _BUILTIN_VOCABULARY:
        _add(builtin)

    terms.sort(key=lambda t: (-len(t.split()), -len(t)))
    return terms


def _apply_glossary_and_phonetic_fixes(
    text: str,
    context_notes: str = "",
    enrolled_names: Optional[List[str]] = None,
) -> str:
    """Corrige termos foneticamente próximos ao glossário do usuário e nomes cadastrados."""
    if not text:
        return text

    terms = _extract_glossary_terms(context_notes, enrolled_names)
    ctx_low = (context_notes or "").lower()

    # Correções diretas comuns em falas PT-BR corporativas quando o glossário inclui termos relacionados
    out = re.sub(r"\bHomens\s+Square\b", "OmniScribe", text, flags=re.IGNORECASE)
    out = re.sub(r"\bOmnis?\s+Square\b", "OmniScribe", out, flags=re.IGNORECASE)
    if "clickup" in ctx_low or "click up" in ctx_low:
        out = re.sub(r"\b(?:pick-up|pickup|clique\s+up|click\s+app)\b", "ClickUp", out, flags=re.IGNORECASE)
    if "bizagi" in ctx_low:
        out = re.sub(r"\b(?:bisagi|biza\s*gi|biza)\b", "Bizagi", out, flags=re.IGNORECASE)
    if "bpmn" in ctx_low:
        out = re.sub(r"\b(?:bpm\s+n|bpn)\b", "BPMN", out, flags=re.IGNORECASE)

    # Matching fonético geral em janelas de 1 a 2 palavras contra os termos cadastrados
    term_entries = [(t, _phonetic_norm(t), len(t.split())) for t in terms]
    term_entries = [(t, ph, wc) for t, ph, wc in term_entries if len(ph) >= 4]
    if not term_entries:
        return out

    words = out.split()
    if not words:
        return out

    result: List[str] = []
    i = 0
    while i < len(words):
        replaced = False
        for span_len in (2, 1):
            if i + span_len > len(words):
                continue
            raw_span = " ".join(words[i : i + span_len])
            lead_match = re.match(r"^([^\wÀ-ÿ]*)(.*?)([^\wÀ-ÿ]*)$", raw_span)
            if not lead_match:
                continue
            pre_p, core, post_p = lead_match.group(1), lead_match.group(2), lead_match.group(3)
            if len(core) < 4:
                continue
            ph_core = _phonetic_norm(core)
            if len(ph_core) < 4:
                continue

            best_term = None
            best_ratio = 0.0
            for term, ph_term, wc in term_entries:
                if wc != span_len:
                    continue
                if core.lower() == term.lower():
                    best_term = None
                    break
                thresh = 0.80 if len(ph_term) >= 7 else 0.88
                r = SequenceMatcher(None, ph_core, ph_term).ratio()
                if r >= thresh and r > best_ratio:
                    best_ratio = r
                    best_term = term
            if best_term:
                result.append(f"{pre_p}{best_term}{post_p}")
                i += span_len
                replaced = True
                break
        if not replaced:
            result.append(words[i])
            i += 1
    return " ".join(result)


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
    """Serviço de reconhecimento de fala, isolamento neural de voz, diarização e atas locais."""

    def __init__(
        self,
        models_dir: Optional[Path] = None,
        voiceprint_service: Optional[VoiceprintService] = None,
        default_variant: str = "small",
    ):
        base_dir = _get_base_dir()
        meipass = getattr(sys, "_MEIPASS", None)
        if models_dir:
            self.models_dir = Path(models_dir)
        else:
            cand = base_dir / "models" / "asr"
            if not cand.exists() and meipass and (Path(meipass) / "models" / "asr").exists():
                cand = Path(meipass) / "models" / "asr"
            self.models_dir = cand

        den_cand = base_dir / "models" / "denoiser"
        if not den_cand.exists() and meipass and (Path(meipass) / "models" / "denoiser").exists():
            den_cand = Path(meipass) / "models" / "denoiser"
        self.denoiser_dir = den_cand
        self.denoiser_model_path = self.denoiser_dir / DENOISER_MODEL_NAME

        self.voiceprint_service = voiceprint_service or VoiceprintService()
        self.default_variant = default_variant if default_variant in WHISPER_MODELS else "small"

        self._lock = threading.Lock()
        self._denoiser_lock = threading.Lock()
        self._recognizer = None
        self._denoiser = None
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
            ok_vp, msg_vp = self.voiceprint_service.ensure_models_downloaded(on_progress=progress_cb)
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

            req = urllib.request.Request(spec["url"], headers={"User-Agent": "Escutai/2.0"})
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

            # Remove pesos FP32 redundantes se os equivalentes INT8 existirem (reduz ~500 MB)
            for int8_file in target_dir.rglob("*.int8.onnx"):
                fp32_sibling = int8_file.with_name(int8_file.name.replace(".int8.onnx", ".onnx"))
                if fp32_sibling.exists():
                    fp32_sibling.unlink(missing_ok=True)

            if self._resolve_model_files(var) is None:
                return False, f"Arquivos Whisper inválidos após extração em {target_dir}."

            self.ensure_denoiser_ready(progress_cb=progress_cb)

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

    def ensure_denoiser_ready(
        self,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> Tuple[bool, str]:
        """Garante o modelo neural GTCRN (~520 KB) para redução de ruído e isolamento vocal."""
        if self.denoiser_model_path.exists() and self.denoiser_model_path.stat().st_size > 50_000:
            return True, "Modelo neural de isolamento de voz (GTCRN) pronto."
        try:
            self._ensure_sherpa()
            self.denoiser_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = self.denoiser_model_path.with_suffix(".onnx.tmp")
            if progress_cb:
                progress_cb("Baixando isolador neural de voz (GTCRN ~520 KB)...", 0.92)
            req = urllib.request.Request(DENOISER_MODEL_URL, headers={"User-Agent": "Escutai/2.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(tmp_file, "wb") as f:
                    f.write(resp.read())
            if tmp_file.stat().st_size < 50_000:
                tmp_file.unlink(missing_ok=True)
                return False, "Download incompleto do modelo GTCRN."
            tmp_file.replace(self.denoiser_model_path)
            return True, "Modelo neural GTCRN pronto."
        except Exception as e:
            logger.warning(f"Falha ao baixar modelo GTCRN: {e}")
            return False, f"GTCRN indisponível: {e}"

    def _ensure_denoiser(self):
        with self._denoiser_lock:
            if self._denoiser is not None:
                return self._denoiser
            ok, msg = self.ensure_denoiser_ready()
            if not ok:
                raise RuntimeError(msg)
            so = self._ensure_sherpa()
            threads = min(4, max(2, (os.cpu_count() or 4) // 2))
            cfg = so.OfflineSpeechDenoiserConfig(
                model=so.OfflineSpeechDenoiserModelConfig(
                    gtcrn=so.OfflineSpeechDenoiserGtcrnModelConfig(model=str(self.denoiser_model_path)),
                    num_threads=threads,
                    debug=False,
                    provider="cpu",
                )
            )
            self._denoiser = so.OfflineSpeechDenoiser(cfg)
            return self._denoiser

    def denoise_and_isolate_voice(
        self,
        samples_f32: np.ndarray,
        sample_rate: int = TARGET_SAMPLE_RATE,
    ) -> np.ndarray:
        """Executa isolamento neural GTCRN para separar voz humana de ruído/eco de fundo."""
        if len(samples_f32) == 0:
            return samples_f32
        arr = samples_f32.astype(np.float32)
        if sample_rate != TARGET_SAMPLE_RATE:
            pcm_i16 = np.clip(np.round(arr * 32768.0), -32768, 32767).astype(np.int16)
            arr = _to_float32_mono(resample_to_16k(pcm_i16, sample_rate, TARGET_SAMPLE_RATE))

        try:
            denoiser = self._ensure_denoiser()
            max_chunk = 30 * TARGET_SAMPLE_RATE
            parts: List[np.ndarray] = []
            with self._denoiser_lock:
                for pos in range(0, len(arr), max_chunk):
                    sub = arr[pos : pos + max_chunk]
                    if len(sub) < 160:
                        parts.append(sub)
                        continue
                    res = denoiser.run(sub, TARGET_SAMPLE_RATE)
                    parts.append(np.asarray(res.samples, dtype=np.float32))
            return np.concatenate(parts) if len(parts) > 1 else (parts[0] if parts else arr)
        except Exception as e:
            logger.warning(f"Fallback sem denoiser neural GTCRN: {e}")
            return arr

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
            threads = min(8, max(4, (os.cpu_count() or 4) - 2))
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
        results = self._transcribe_batch_samples([samples_f32], variant=variant)
        return results[0] if results else ""

    def _transcribe_batch_samples(
        self,
        chunks: List[np.ndarray],
        variant: Optional[str] = None,
    ) -> List[str]:
        """Decodifica turnos em lote (`decode_streams`) limitados a <=29,5s cada."""
        if not chunks:
            return []
        rec = self._ensure_recognizer(variant)
        min_len = int(0.25 * TARGET_SAMPLE_RATE)
        max_len = int(29.5 * TARGET_SAMPLE_RATE)
        outputs = [""] * len(chunks)

        valid_entries: List[Tuple[int, np.ndarray]] = []
        for idx, ch in enumerate(chunks):
            if len(ch) >= min_len:
                valid_entries.append((idx, ch[:max_len].astype(np.float32)))

        if not valid_entries:
            return outputs

        batch_size = 6
        with self._lock:
            for b_start in range(0, len(valid_entries), batch_size):
                batch = valid_entries[b_start : b_start + batch_size]
                streams = []
                for _orig_idx, wav_arr in batch:
                    s = rec.create_stream()
                    s.accept_waveform(TARGET_SAMPLE_RATE, wav_arr)
                    streams.append(s)
                if len(streams) > 1 and hasattr(rec, "decode_streams"):
                    rec.decode_streams(streams)
                else:
                    for s in streams:
                        rec.decode_stream(s)
                for (orig_idx, _), s in zip(batch, streams):
                    raw_txt = getattr(s.result, "text", "") or ""
                    outputs[orig_idx] = _clean_whisper_text(raw_txt)

        return outputs

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
        if not self.voiceprint_service:
            return "Participante 1"
        try:
            return self.voiceprint_service.identify_speaker_segment(
                seg_f32, TARGET_SAMPLE_RATE, unknown_clusters=unknown_clusters
            )
        except Exception:
            return "Participante 1"

    @staticmethod
    def _merge_same_speaker_turns(
        diarized_segs: List[Tuple[float, float, str, np.ndarray]],
        max_gap_sec: float = 2.5,
        max_turn_sec: float = 24.0,
    ) -> List[Tuple[float, float, str, np.ndarray]]:
        """Mescla trechos consecutivos do mesmo locutor para decodificação rápida e maior contexto."""
        if not diarized_segs:
            return []
        pad = np.zeros(int(0.14 * TARGET_SAMPLE_RATE), dtype=np.float32)
        merged: List[Tuple[float, float, str, np.ndarray]] = []
        for s_t, e_t, spk, seg in diarized_segs:
            if (
                merged
                and merged[-1][2] == spk
                and (s_t - merged[-1][1]) <= max_gap_sec
                and (e_t - merged[-1][0]) <= max_turn_sec
            ):
                p_s, _p_e, p_spk, p_seg = merged[-1]
                merged[-1] = (p_s, max(_p_e, e_t), p_spk, np.concatenate([p_seg, pad, seg]))
            else:
                merged.append((s_t, e_t, spk, seg))
        return merged

    def _process_channel_turns(
        self,
        chan_f32: np.ndarray,
        sr: int,
        block_offset_seconds: float,
        variant: Optional[str],
        clusters: List[Tuple[str, np.ndarray, int]],
        fixed_speaker: Optional[str] = None,
        context_notes: str = "",
        enrolled_names: Optional[List[str]] = None,
        isolate_voice: bool = False,
        clarify_audio: bool = True,
        enhancement_mode: str = "clarify_room",
        strip_voce_suffix: bool = False,
    ) -> List[Tuple[float, float, str, str]]:
        """Pipeline dual-branch: VAD+Voiceprint no timbre natural -> restauração/isolamento no ASR."""
        if len(chan_f32) == 0:
            return []

        raw_f32 = chan_f32
        if sr != TARGET_SAMPLE_RATE:
            pcm_i16 = np.clip(np.round(raw_f32 * 32768.0), -32768, 32767).astype(np.int16)
            raw_f32 = _to_float32_mono(resample_to_16k(pcm_i16, sr, TARGET_SAMPLE_RATE))

        # Ramo A (timbre natural 16 kHz): VAD + Voiceprint preservam formantes originais dos perfis
        vad_segs = self._segment_channel(raw_f32, TARGET_SAMPLE_RATE)
        if not vad_segs:
            return []

        if fixed_speaker is not None:
            diarized = [(s_t, e_t, fixed_speaker, seg) for s_t, e_t, seg in vad_segs]
        elif self.voiceprint_service:
            diarized = self.voiceprint_service.diarize_speech_segments(
                vad_segs, TARGET_SAMPLE_RATE, unknown_clusters=clusters
            )
        else:
            diarized = [(s_t, e_t, "Participante 1", seg) for s_t, e_t, seg in vad_segs]

        # Ramo B (ASR restaurado): isolamento neural GTCRN e/ou EQ vocal apenas para decodificação Whisper
        eff_mode = (
            enhancement_mode
            if enhancement_mode in ("none", "light", "clarify_room", "denoise_and_clarify", "neural_voice_isolation")
            else ("clarify_room" if clarify_audio else "none")
        )
        asr_f32 = raw_f32
        if isolate_voice or eff_mode == "neural_voice_isolation":
            asr_f32 = self.denoise_and_isolate_voice(asr_f32, TARGET_SAMPLE_RATE)
        if eff_mode != "none":
            pcm_i16 = np.clip(np.round(asr_f32 * 32768.0), -32768, 32767).astype(np.int16)
            asr_f32 = _to_float32_mono(enhance_speech_audio(pcm_i16, TARGET_SAMPLE_RATE, mode=eff_mode))

        diarized_asr: List[Tuple[float, float, str, np.ndarray]] = []
        for s_t, e_t, spk, raw_seg in diarized:
            i0 = max(0, int(round(s_t * TARGET_SAMPLE_RATE)))
            i1 = min(len(asr_f32), int(round(e_t * TARGET_SAMPLE_RATE)))
            asr_seg = asr_f32[i0:i1] if i1 > i0 else raw_seg
            diarized_asr.append((s_t, e_t, spk, asr_seg))

        merged = self._merge_same_speaker_turns(diarized_asr)
        texts = self._transcribe_batch_samples([m[3] for m in merged], variant=variant)

        events: List[Tuple[float, float, str, str]] = []
        for (s_t, e_t, spk, _seg), raw_txt in zip(merged, texts):
            if not raw_txt:
                continue
            fixed_txt = _apply_glossary_and_phonetic_fixes(
                raw_txt, context_notes=context_notes, enrolled_names=enrolled_names
            )
            if not fixed_txt:
                continue
            final_spk = spk
            if strip_voce_suffix and final_spk.endswith("(Você)"):
                final_spk = "Willian"
            events.append((block_offset_seconds + s_t, block_offset_seconds + e_t, final_spk, fixed_txt))
        return events

    def transcribe_wav_block(
        self,
        wav_path: Path,
        block_offset_seconds: float = 0.0,
        variant: Optional[str] = None,
        session_unknown_clusters: Optional[List[Tuple[str, np.ndarray, int]]] = None,
        context_notes: str = "",
        enrolled_names: Optional[List[str]] = None,
        isolate_voice: bool = False,
        clarify_audio: bool = True,
        enhancement_mode: str = "clarify_room",
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
        events: List[Tuple[float, float, str, str]] = []

        if nchannels >= 2:
            stereo = arr.reshape(-1, nchannels)
            left_f = _to_float32_mono(stereo[:, 0])
            right_f = _to_float32_mono(stereo[:, 1])

            events.extend(
                self._process_channel_turns(
                    right_f, sr, block_offset_seconds, variant, clusters,
                    fixed_speaker="Willian (Você)", context_notes=context_notes,
                    enrolled_names=enrolled_names, isolate_voice=isolate_voice,
                    clarify_audio=clarify_audio, enhancement_mode=enhancement_mode,
                )
            )
            events.extend(
                self._process_channel_turns(
                    left_f, sr, block_offset_seconds, variant, clusters,
                    fixed_speaker=None, context_notes=context_notes,
                    enrolled_names=enrolled_names, isolate_voice=isolate_voice,
                    clarify_audio=clarify_audio, enhancement_mode=enhancement_mode,
                )
            )
        else:
            mono_f = _to_float32_mono(arr)
            events.extend(
                self._process_channel_turns(
                    mono_f, sr, block_offset_seconds, variant, clusters,
                    fixed_speaker=None, context_notes=context_notes,
                    enrolled_names=enrolled_names, isolate_voice=isolate_voice,
                    clarify_audio=clarify_audio, enhancement_mode=enhancement_mode,
                    strip_voce_suffix=True,
                )
            )

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

    parser = argparse.ArgumentParser(description="Gerenciador de modelos offline do Escutaí")
    parser.add_argument("--prepare", choices=list(WHISPER_MODELS.keys()), default="small")
    args = parser.parse_args()

    svc = OfflineAsrService(default_variant=args.prepare)
    ok, msg = svc.ensure_models_ready(
        variant=args.prepare,
        progress_cb=lambda m, p: print(f"[{int(p * 100):3d}%] {m}", flush=True),
    )
    print(f"{'[OK]' if ok else '[ERRO]'} {msg}")
    sys.exit(0 if ok else 1)
