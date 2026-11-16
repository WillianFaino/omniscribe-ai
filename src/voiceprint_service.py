"""Serviço de Biometria Vocal (Voiceprint / Speaker Identification) do OmniScribe AI.

Utiliza `sherpa-onnx` (WeSpeaker CAM++ LM + Silero VAD) em CPU para:
1. Cadastrar perfis biométricos de voz (via frases curtas no microfone ou trechos de WAV salvo);
2. Detectar e identificar automaticamente locutores em blocos Mono (presencial) ou Canal L (remoto)
   antes da chamada ao Gemini, ancorando nomes reais sem depender de apresentações na reunião.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.request
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from audio_engine import TARGET_SAMPLE_RATE, resample_to_16k

logger = logging.getLogger("omniscribe.voiceprint")

EMBEDDING_MODEL_NAME = "wespeaker_en_voxceleb_resnet34_LM.onnx"
EMBEDDING_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"
)
VAD_MODEL_NAME = "silero_vad.onnx"
VAD_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "asr-models/silero_vad.onnx"
)

DEFAULT_MATCH_THRESHOLD = 0.56
DEFAULT_OVERLAP_MARGIN = 0.08
DEFAULT_UNKNOWN_CLUSTER_THRESHOLD = 0.58

sherpa_onnx = None
HAS_SHERPA_ONNX: Optional[bool] = None


def _ensure_sherpa_loaded() -> bool:
    """Carrega `sherpa_onnx` sob demanda para preservar o tempo de abertura da GUI."""
    global sherpa_onnx, HAS_SHERPA_ONNX
    if HAS_SHERPA_ONNX is not None:
        return HAS_SHERPA_ONNX
    try:
        import sherpa_onnx as _sherpa
        sherpa_onnx = _sherpa
        HAS_SHERPA_ONNX = True
    except ImportError:
        sherpa_onnx = None
        HAS_SHERPA_ONNX = False
    return HAS_SHERPA_ONNX


def _get_base_dir() -> Path:
    """Retorna o diretório base da aplicação (compatível com script e PyInstaller)."""
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


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-12:
        return vec.astype(np.float32)
    return (vec / norm).astype(np.float32)


def _to_float32_mono(samples: np.ndarray) -> np.ndarray:
    arr = np.asarray(samples)
    if arr.ndim > 1:
        arr = arr[:, 0]
    if arr.dtype == np.int16 or np.issubdtype(arr.dtype, np.integer):
        return (arr.astype(np.float32) / 32768.0).clip(-1.0, 1.0)
    return arr.astype(np.float32).clip(-1.0, 1.0)


class VoiceprintService:
    """Gerencia modelos ONNX, cadastro de perfis vocais e identificação em blocos WAV."""

    def __init__(
        self,
        models_dir: Optional[Path] = None,
        profiles_path: Optional[Path] = None,
    ):
        base_dir = _get_base_dir()
        if models_dir:
            self.models_dir = Path(models_dir)
        else:
            cand = base_dir / "models" / "voiceprint"
            meipass = getattr(sys, "_MEIPASS", None)
            if not cand.exists() and meipass and (Path(meipass) / "models" / "voiceprint").exists():
                cand = Path(meipass) / "models" / "voiceprint"
            self.models_dir = cand
        self.profiles_path = (
            Path(profiles_path)
            if profiles_path
            else (base_dir / "atas_reunioes" / "voice_profiles.json")
        )

        self.embedding_model_path = self.models_dir / EMBEDDING_MODEL_NAME
        self.vad_model_path = self.models_dir / VAD_MODEL_NAME

        self._inference_lock = threading.Lock()
        self._extractor = None
        self._vad = None
        self._profiles: Dict[str, Dict] = {}
        self._load_profiles()

    # ---------------- PERSISTÊNCIA DE PERFIS ----------------
    def _load_profiles(self) -> None:
        if not self.profiles_path.exists():
            self._profiles = {}
            return
        try:
            with open(self.profiles_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            profs = raw.get("profiles", {})
            loaded: Dict[str, Dict] = {}
            for name, entry in profs.items():
                vec = np.asarray(entry.get("embedding", []), dtype=np.float32)
                if vec.size > 0:
                    loaded[str(name)] = {
                        "embedding": _l2_normalize(vec),
                        "samples_count": int(entry.get("samples_count", 1)),
                        "total_speech_seconds": float(entry.get("total_speech_seconds", 0.0)),
                        "updated_at": str(entry.get("updated_at", "")),
                    }
            self._profiles = loaded
        except Exception as e:
            logger.warning(f"Erro ao carregar perfis de voz ({self.profiles_path}): {e}")
            self._profiles = {}

    def _save_profiles(self) -> None:
        self.profiles_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": EMBEDDING_MODEL_NAME,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "profiles": {
                name: {
                    "embedding": data["embedding"].astype(float).tolist(),
                    "samples_count": data["samples_count"],
                    "total_speech_seconds": round(data["total_speech_seconds"], 2),
                    "updated_at": data["updated_at"],
                }
                for name, data in self._profiles.items()
            },
        }
        tmp_path = self.profiles_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        tmp_path.replace(self.profiles_path)

    def list_profiles(self) -> List[Dict]:
        """Retorna lista ordenada dos perfis biométricos cadastrados."""
        self._load_profiles()
        items = []
        for name in sorted(self._profiles.keys(), key=str.lower):
            d = self._profiles[name]
            items.append(
                {
                    "name": name,
                    "samples_count": d["samples_count"],
                    "total_speech_seconds": d["total_speech_seconds"],
                    "dim": int(d["embedding"].size),
                    "updated_at": d["updated_at"],
                }
            )
        return items

    def has_profiles(self) -> bool:
        self._load_profiles()
        return len(self._profiles) > 0

    def delete_profile(self, name: str) -> bool:
        """Remove um perfil biométrico pelo nome."""
        self._load_profiles()
        if name in self._profiles:
            del self._profiles[name]
            self._save_profiles()
            return True
        return False

    def export_profiles(
        self,
        output_path: Path,
        profile_names: Optional[List[str]] = None,
    ) -> int:
        """Exporta um ou mais perfis biométricos para um arquivo JSON portável."""
        self._load_profiles()
        if profile_names is not None:
            wanted = set(profile_names)
            selected = {k: v for k, v in self._profiles.items() if k in wanted}
        else:
            selected = dict(self._profiles)

        if not selected:
            raise ValueError("Nenhum perfil de voz disponível para exportar.")

        sample_dim = next(iter(selected.values()))["embedding"].size
        payload = {
            "schema_version": 1,
            "format": "omniscribe-voiceprints",
            "model": EMBEDDING_MODEL_NAME,
            "dim": int(sample_dim),
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "profiles": {
                name: {
                    "embedding": data["embedding"].astype(float).tolist(),
                    "samples_count": int(data["samples_count"]),
                    "total_speech_seconds": round(float(data["total_speech_seconds"]), 2),
                    "updated_at": data["updated_at"],
                }
                for name, data in selected.items()
            },
        }
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return len(selected)

    def import_profiles(
        self,
        input_path: Path,
        conflict_strategy: str = "merge",
    ) -> Dict[str, int]:
        """Importa perfis biométricos de um arquivo JSON com validação de modelo e mesclagem.

        conflict_strategy:
          - 'merge': combina vetores L2-normalizados ponderados por `samples_count` quando o nome já existir.
          - 'overwrite': substitui o perfil local pelo importado.
          - 'skip': mantém o perfil local intacto e importa apenas novos nomes.
        """
        if conflict_strategy not in ("merge", "overwrite", "skip"):
            raise ValueError(f"Estratégia de conflito inválida: {conflict_strategy}")

        input_path = Path(input_path)
        if not input_path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {input_path}")

        with open(input_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
            raise ValueError("Arquivo JSON inválido: estrutura de perfis de voz não encontrada.")

        source_model = raw.get("model")
        if source_model and source_model != EMBEDDING_MODEL_NAME:
            raise ValueError(
                f"Modelo incompatível no arquivo ({source_model}). Esperado: {EMBEDDING_MODEL_NAME}."
            )

        self._load_profiles()
        expected_dim = (
            next(iter(self._profiles.values()))["embedding"].size
            if self._profiles
            else int(raw.get("dim") or 256)
        )

        added = 0
        updated = 0
        skipped = 0
        now_iso = datetime.now().isoformat(timespec="seconds")

        for raw_name, entry in raw["profiles"].items():
            clean_name = str(raw_name).strip()
            if not clean_name or not isinstance(entry, dict):
                continue

            vec = np.asarray(entry.get("embedding", []), dtype=np.float32)
            if vec.ndim != 1 or vec.size == 0 or float(np.linalg.norm(vec)) <= 1e-8:
                continue
            if vec.size != expected_dim:
                raise ValueError(
                    f"Dimensão incompatível para '{clean_name}': {vec.size} != {expected_dim}."
                )

            imp_emb = _l2_normalize(vec)
            imp_count = max(1, int(entry.get("samples_count", 1)))
            imp_secs = max(0.0, float(entry.get("total_speech_seconds", 0.0)))
            imp_updated = str(entry.get("updated_at", now_iso))

            if clean_name in self._profiles:
                if conflict_strategy == "skip":
                    skipped += 1
                    continue
                elif conflict_strategy == "overwrite":
                    self._profiles[clean_name] = {
                        "embedding": imp_emb,
                        "samples_count": imp_count,
                        "total_speech_seconds": imp_secs,
                        "updated_at": now_iso,
                    }
                    updated += 1
                else:  # merge
                    cur = self._profiles[clean_name]
                    cur_w = float(max(1, cur["samples_count"]))
                    combined = cur["embedding"] * cur_w + imp_emb * float(imp_count)
                    self._profiles[clean_name] = {
                        "embedding": _l2_normalize(combined),
                        "samples_count": int(cur["samples_count"]) + imp_count,
                        "total_speech_seconds": float(cur["total_speech_seconds"]) + imp_secs,
                        "updated_at": now_iso,
                    }
                    updated += 1
            else:
                self._profiles[clean_name] = {
                    "embedding": imp_emb,
                    "samples_count": imp_count,
                    "total_speech_seconds": imp_secs,
                    "updated_at": imp_updated,
                }
                added += 1

        if added == 0 and updated == 0 and skipped == 0:
            raise ValueError("Nenhum perfil válido encontrado no arquivo selecionado.")

        self._save_profiles()
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "total_after": len(self._profiles),
        }

    # ---------------- MODELOS ONNX & DOWNLOAD ----------------
    def is_runtime_available(self) -> bool:
        return _ensure_sherpa_loaded()

    def are_models_ready(self) -> bool:
        return (
            self.embedding_model_path.exists()
            and self.embedding_model_path.stat().st_size > 100_000
            and self.vad_model_path.exists()
            and self.vad_model_path.stat().st_size > 10_000
        )

    def is_ready_for_matching(self) -> bool:
        return self.is_runtime_available() and self.are_models_ready() and self.has_profiles()

    def ensure_models_downloaded(
        self,
        on_progress: Optional[Callable[[str, float], None]] = None,
    ) -> Tuple[bool, str]:
        """Baixa os modelos ONNX oficiais (`CAM++_LM` + `silero_vad`) caso ainda não existam localmente."""
        if not self.is_runtime_available():
            return False, "Biblioteca 'sherpa-onnx' não instalada no ambiente Python."

        self.models_dir.mkdir(parents=True, exist_ok=True)
        targets = [
            (EMBEDDING_MODEL_NAME, EMBEDDING_MODEL_URL, self.embedding_model_path, 100_000),
            (VAD_MODEL_NAME, VAD_MODEL_URL, self.vad_model_path, 10_000),
        ]

        try:
            for idx, (label, url, dest, min_bytes) in enumerate(targets):
                if dest.exists() and dest.stat().st_size > min_bytes:
                    if on_progress:
                        on_progress(f"{label} já disponível.", (idx + 1) / float(len(targets)))
                    continue

                tmp_file = dest.with_suffix(".onnx.tmp")
                if on_progress:
                    on_progress(f"Baixando {label}...", idx / float(len(targets)))

                req = urllib.request.Request(url, headers={"User-Agent": "OmniScribe-AI/1.3"})
                with urllib.request.urlopen(req, timeout=90) as resp:
                    total_size = int(resp.headers.get("Content-Length", "0") or 0)
                    downloaded = 0
                    with open(tmp_file, "wb") as out_f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            out_f.write(chunk)
                            downloaded += len(chunk)
                            if on_progress and total_size > 0:
                                file_frac = min(1.0, downloaded / float(total_size))
                                overall = (idx + file_frac) / float(len(targets))
                                on_progress(
                                    f"Baixando {label} ({downloaded // 1048576}/{max(1, total_size // 1048576)} MB)...",
                                    overall,
                                )

                if tmp_file.stat().st_size < min_bytes:
                    tmp_file.unlink(missing_ok=True)
                    return False, f"Download incompleto de {label}."

                tmp_file.replace(dest)

            return True, "Modelos biométricos prontos para uso offline."
        except Exception as e:
            logger.exception(f"Falha ao baixar modelos voiceprint: {e}")
            return False, f"Falha no download dos modelos: {e}"

    def _ensure_engines_initialized(self) -> bool:
        """Inicializa extractor e VAD em thread-safety após garantir os arquivos ONNX."""
        if not self.is_runtime_available() or not self.are_models_ready():
            return False

        with self._inference_lock:
            if self._extractor is None:
                ext_cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(self.embedding_model_path),
                    num_threads=2,
                    debug=False,
                    provider="cpu",
                )
                self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(ext_cfg)

            if self._vad is None:
                silero_cfg = sherpa_onnx.SileroVadModelConfig(
                    model=str(self.vad_model_path),
                    threshold=0.45,
                    min_silence_duration=0.30,
                    min_speech_duration=0.35,
                    window_size=512,
                    max_speech_duration=15.0,
                )
                vad_cfg = sherpa_onnx.VadModelConfig(
                    silero_vad=silero_cfg,
                    sample_rate=TARGET_SAMPLE_RATE,
                    num_threads=1,
                    provider="cpu",
                    debug=False,
                )
                self._vad = sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=60)

        return True

    # ---------------- EXTRAÇÃO DE EMBEDDINGS & VAD ----------------
    def extract_speech_segments(
        self,
        samples_float32: np.ndarray,
        sample_rate: int = TARGET_SAMPLE_RATE,
    ) -> List[Tuple[float, float, np.ndarray]]:
        """Detecta segmentos com voz humana via Silero VAD retornando `(inicio_s, fim_s, float32_16k)`."""
        if len(samples_float32) == 0:
            return []

        if sample_rate != TARGET_SAMPLE_RATE:
            int16_in = np.clip(np.round(samples_float32 * 32768.0), -32768, 32767).astype(np.int16)
            samples_float32 = _to_float32_mono(resample_to_16k(int16_in, sample_rate, TARGET_SAMPLE_RATE))

        if not self._ensure_engines_initialized():
            return [(0.0, len(samples_float32) / float(TARGET_SAMPLE_RATE), samples_float32)]

        segments: List[Tuple[float, float, np.ndarray]] = []
        win_size = 512

        with self._inference_lock:
            self._vad.reset()
            total = len(samples_float32)
            for idx in range(0, total, win_size):
                chunk = samples_float32[idx : idx + win_size]
                if len(chunk) < win_size:
                    chunk = np.pad(chunk, (0, win_size - len(chunk)), mode="constant")
                self._vad.accept_waveform(chunk)
                while not self._vad.empty():
                    seg = self._vad.front
                    seg_arr = np.asarray(seg.samples, dtype=np.float32)
                    s_sec = seg.start / float(TARGET_SAMPLE_RATE)
                    e_sec = s_sec + len(seg_arr) / float(TARGET_SAMPLE_RATE)
                    segments.append((s_sec, e_sec, seg_arr))
                    self._vad.pop()

            self._vad.flush()
            while not self._vad.empty():
                seg = self._vad.front
                seg_arr = np.asarray(seg.samples, dtype=np.float32)
                s_sec = seg.start / float(TARGET_SAMPLE_RATE)
                e_sec = s_sec + len(seg_arr) / float(TARGET_SAMPLE_RATE)
                segments.append((s_sec, e_sec, seg_arr))
                self._vad.pop()
            self._vad.reset()

        return segments

    def compute_embedding(
        self,
        samples_float32: np.ndarray,
        sample_rate: int = TARGET_SAMPLE_RATE,
    ) -> Optional[np.ndarray]:
        """Calcula o vetor de voiceprint L2-normalizado para um trecho de fala."""
        if len(samples_float32) < int(0.4 * sample_rate):
            return None
        if not self._ensure_engines_initialized():
            return None

        with self._inference_lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(int(sample_rate), samples_float32.astype(np.float32))
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return None
            vec = np.asarray(self._extractor.compute(stream), dtype=np.float32)

        if vec.size == 0 or float(np.linalg.norm(vec)) <= 1e-8:
            return None
        return _l2_normalize(vec)

    # ---------------- CADASTRO DE PERFIS (ENROLLMENT) ----------------
    def enroll_profile_from_arrays(
        self,
        name: str,
        samples_list: List[np.ndarray],
        sample_rate: int = TARGET_SAMPLE_RATE,
        append: bool = True,
    ) -> Dict:
        """Extrai voiceprint de uma ou mais amostras de fala e salva no perfil `name`."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Informe um nome válido para o perfil de voz.")

        if not self.is_runtime_available():
            raise RuntimeError("Biblioteca 'sherpa-onnx' não está instalada.")
        if not self.are_models_ready():
            ok, msg = self.ensure_models_downloaded()
            if not ok:
                raise RuntimeError(msg)

        self._load_profiles()
        embeddings: List[np.ndarray] = []
        total_speech_secs = 0.0

        for raw_samples in samples_list:
            mono_f = _to_float32_mono(raw_samples)
            if len(mono_f) == 0:
                continue
            if sample_rate != TARGET_SAMPLE_RATE:
                int16_arr = np.clip(np.round(mono_f * 32768.0), -32768, 32767).astype(np.int16)
                mono_f = _to_float32_mono(resample_to_16k(int16_arr, sample_rate, TARGET_SAMPLE_RATE))

            # Usa VAD para remover pausas/silêncios da amostra antes de extrair a biometria
            vad_segs = self.extract_speech_segments(mono_f, TARGET_SAMPLE_RATE)
            speech_audio = np.concatenate([s[2] for s in vad_segs]) if vad_segs else mono_f

            if len(speech_audio) < int(0.5 * TARGET_SAMPLE_RATE):
                # Fallback se fala foi baixa demais pro VAD mas ainda há energia útil
                rms = float(np.sqrt(np.mean(mono_f * mono_f))) if len(mono_f) else 0.0
                if rms > 0.004 and len(mono_f) >= int(0.5 * TARGET_SAMPLE_RATE):
                    speech_audio = mono_f
                else:
                    continue

            emb = self.compute_embedding(speech_audio, TARGET_SAMPLE_RATE)
            if emb is not None:
                embeddings.append(emb)
                total_speech_secs += len(speech_audio) / float(TARGET_SAMPLE_RATE)

        if not embeddings:
            raise ValueError(
                "Não foi detectada fala humana suficiente nas amostras (mínimo ~1s de voz audível)."
            )

        new_centroid = _l2_normalize(np.mean(np.stack(embeddings, axis=0), axis=0))
        new_count = len(embeddings)

        if append and clean_name in self._profiles:
            existing = self._profiles[clean_name]
            old_w = float(max(1, existing["samples_count"]))
            combined = existing["embedding"] * old_w + new_centroid * float(new_count)
            final_emb = _l2_normalize(combined)
            final_count = existing["samples_count"] + new_count
            final_secs = existing["total_speech_seconds"] + total_speech_secs
        else:
            final_emb = new_centroid
            final_count = new_count
            final_secs = total_speech_secs

        now_iso = datetime.now().isoformat(timespec="seconds")
        self._profiles[clean_name] = {
            "embedding": final_emb,
            "samples_count": final_count,
            "total_speech_seconds": final_secs,
            "updated_at": now_iso,
        }
        self._save_profiles()

        return {
            "name": clean_name,
            "samples_count": final_count,
            "total_speech_seconds": round(final_secs, 2),
            "dim": int(final_emb.size),
            "updated_at": now_iso,
        }

    def enroll_profile_from_wav_segment(
        self,
        name: str,
        wav_path: Path,
        start_seconds: float = 0.0,
        end_seconds: Optional[float] = None,
        channel: int = -1,
        append: bool = True,
    ) -> Dict:
        """Cadastra/atualiza um perfil a partir de um trecho `[start_seconds .. end_seconds]` de um arquivo WAV."""
        wav_path = Path(wav_path)
        if not wav_path.exists():
            raise FileNotFoundError(f"Arquivo WAV não encontrado: {wav_path}")

        with wave.open(str(wav_path), "rb") as wf:
            nchannels = int(wf.getnchannels())
            sampwidth = int(wf.getsampwidth())
            sr = int(wf.getframerate())
            total_frames = int(wf.getnframes())
            if sampwidth != 2:
                raise ValueError("Apenas WAV PCM 16-bit é suportado.")

            s_frame = max(0, min(total_frames, int(round(max(0.0, start_seconds) * sr))))
            e_frame = (
                total_frames
                if end_seconds is None
                else max(s_frame, min(total_frames, int(round(end_seconds * sr))))
            )
            if e_frame <= s_frame:
                raise ValueError("Intervalo de tempo inválido (fim deve ser maior que início).")

            wf.setpos(s_frame)
            raw = wf.readframes(e_frame - s_frame)

        arr = np.frombuffer(raw, dtype=np.int16)
        if nchannels > 1:
            arr = arr.reshape(-1, nchannels)
            if channel in (0, 1) and channel < nchannels:
                mono_int16 = arr[:, channel]
            else:
                # Seleciona automaticamente o canal com maior energia no trecho escolhido
                energies = [float(np.mean(arr[:, c].astype(np.float64) ** 2)) for c in range(min(2, nchannels))]
                best_ch = int(np.argmax(energies))
                mono_int16 = arr[:, best_ch]
        else:
            mono_int16 = arr

        return self.enroll_profile_from_arrays(
            name=name,
            samples_list=[mono_int16],
            sample_rate=sr,
            append=append,
        )

    # ---------------- IDENTIFICAÇÃO EM BLOCOS ----------------
    def _match_embedding(
        self,
        emb: np.ndarray,
        unknown_clusters: List[Tuple[str, np.ndarray, int]],
        match_threshold: float,
        overlap_margin: float,
    ) -> str:
        scores = [
            (name, float(np.dot(emb, pdata["embedding"])))
            for name, pdata in self._profiles.items()
        ]
        scores.sort(key=lambda item: item[1], reverse=True)

        if scores and scores[0][1] >= match_threshold:
            top_name, top_score = scores[0]
            if len(scores) > 1 and top_score < 0.78:
                sec_name, sec_score = scores[1]
                if sec_score >= (match_threshold - 0.06) and (top_score - sec_score) <= overlap_margin:
                    return f"⚡ Possível sobreposição ({top_name} / {sec_name})"
            return f"**{top_name}** (score {top_score:.2f})"

        # Agrupa locutores desconhecidos consistentes dentro do bloco
        best_u_idx = -1
        best_u_score = -1.0
        for idx, (_uname, uemb, _ucnt) in enumerate(unknown_clusters):
            sc = float(np.dot(emb, uemb))
            if sc > best_u_score:
                best_u_score = sc
                best_u_idx = idx

        if best_u_idx >= 0 and best_u_score >= DEFAULT_UNKNOWN_CLUSTER_THRESHOLD:
            uname, uemb, ucnt = unknown_clusters[best_u_idx]
            new_uemb = _l2_normalize((uemb * float(ucnt) + emb) / float(ucnt + 1))
            unknown_clusters[best_u_idx] = (uname, new_uemb, ucnt + 1)
            return f"**{uname}**"

        new_label = f"Locutor Desconhecido {len(unknown_clusters) + 1}"
        unknown_clusters.append((new_label, emb, 1))
        return f"**{new_label}**"

    def analyze_wav_speakers(
        self,
        wav_path: Path,
        block_offset_seconds: float = 0.0,
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
        overlap_margin: float = DEFAULT_OVERLAP_MARGIN,
    ) -> str:
        """Gera pistas biométricas temporais para guiar a diarização do Gemini sem chamadas extras."""
        if not self.is_ready_for_matching():
            return ""

        wav_path = Path(wav_path)
        if not wav_path.exists():
            return ""

        with wave.open(str(wav_path), "rb") as wf:
            nchannels = int(wf.getnchannels())
            sampwidth = int(wf.getsampwidth())
            sr = int(wf.getframerate())
            total_frames = int(wf.getnframes())
            if sampwidth != 2 or total_frames == 0:
                return ""
            raw = wf.readframes(total_frames)

        arr = np.frombuffer(raw, dtype=np.int16)
        if nchannels > 1:
            # Em Estéreo Online, R já é exclusivamente Willian; analisamos apenas Canal L (participantes remotos)
            arr = arr.reshape(-1, nchannels)[:, 0]
            scope_title = "PISTAS BIOMÉTRICAS DE PARTICIPANTES REMOTOS (CANAL ESQUERDO L)"
        else:
            scope_title = "PISTAS BIOMÉTRICAS DE LOCUTORES NA SALA (CANAL MONO PRESENCIAL)"

        mono_f = _to_float32_mono(arr)
        vad_segments = self.extract_speech_segments(mono_f, sr)
        if not vad_segments:
            return ""

        win_samples = int(round(2.4 * TARGET_SAMPLE_RATE))
        hop_samples = int(round(1.2 * TARGET_SAMPLE_RATE))
        min_tail_samples = int(round(0.65 * TARGET_SAMPLE_RATE))

        timeline: List[Tuple[float, float, str]] = []
        unknown_clusters: List[Tuple[str, np.ndarray, int]] = []

        for seg_start, seg_end, seg_arr in vad_segments:
            seg_len = len(seg_arr)
            if seg_len < min_tail_samples:
                continue

            if seg_len <= int(round(2.8 * TARGET_SAMPLE_RATE)):
                emb = self.compute_embedding(seg_arr, TARGET_SAMPLE_RATE)
                if emb is not None:
                    label = self._match_embedding(emb, unknown_clusters, match_threshold, overlap_margin)
                    timeline.append((seg_start, seg_end, label))
            else:
                pos = 0
                while pos < seg_len:
                    end_pos = min(seg_len, pos + win_samples)
                    sub = seg_arr[pos:end_pos]
                    if len(sub) < min_tail_samples and pos > 0:
                        break
                    emb = self.compute_embedding(sub, TARGET_SAMPLE_RATE)
                    if emb is not None:
                        label = self._match_embedding(emb, unknown_clusters, match_threshold, overlap_margin)
                        w_s = seg_start + (pos / float(TARGET_SAMPLE_RATE))
                        w_e = seg_start + (end_pos / float(TARGET_SAMPLE_RATE))
                        timeline.append((w_s, w_e, label))
                    if end_pos >= seg_len:
                        break
                    pos += hop_samples

        if not timeline:
            return ""

        # Mescla janelas consecutivas com mesmo locutor para manter o prompt compacto
        merged: List[Tuple[float, float, str]] = []
        for s_t, e_t, lbl in timeline:
            base_lbl = lbl.split(" (score ")[0]
            if merged and merged[-1][2].split(" (score ")[0] == base_lbl and (s_t - merged[-1][1]) <= 1.2:
                prev_s, _prev_e, prev_lbl = merged[-1]
                merged[-1] = (prev_s, max(_prev_e, e_t), prev_lbl)
            else:
                merged.append((s_t, e_t, lbl))

        lines = [
            f"\n--- {scope_title} ---",
            "Use estas âncoras acústicas de Voiceprint como guia prioritário para nomear quem fala em cada trecho (confirme sempre pelo contexto e preserve falas sobrepostas):",
        ]
        for s_t, e_t, lbl in merged[:30]:
            abs_s = _format_mmss(block_offset_seconds + s_t)
            abs_e = _format_mmss(block_offset_seconds + e_t)
            lines.append(f"- [{abs_s} - {abs_e}] -> {lbl}")
        lines.append("----------------------------------------------------------------------\n")
        return "\n".join(lines) + "\n"
