"""Motor de áudio do OmniScribe AI com suporte a WASAPI Loopback (sistema/reuniões) e microfone local.

Realiza captura dual sincronizada ou mono presencial, resampling anti-aliasing para 16 kHz,
pré-processamento acústico (HPF 80 Hz + AGC suave com limiter -1 dBFS), noise gate suave,
fatiamento em pausas naturais com overlap de 2,5s e cálculo de VU meter em tempo real.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

# Importação defensiva do scipy para resampling polifásico de alta precisão
try:
    from scipy.signal import resample_poly as _scipy_resample_poly
    HAS_SCIPY_SIGNAL = True
except ImportError:
    _scipy_resample_poly = None
    HAS_SCIPY_SIGNAL = False

# Importação defensiva de pyaudiowpatch com fallback para pyaudio padrão
try:
    import pyaudiowpatch as pyaudio
    HAS_PYAUDIOWPATCH = True
except ImportError:
    try:
        import pyaudio
        HAS_PYAUDIOWPATCH = False
    except ImportError:
        pyaudio = None
        HAS_PYAUDIOWPATCH = False

logger = logging.getLogger("omniscribe.audio_engine")

TARGET_SAMPLE_RATE = 16000  # Taxa padrão otimizada para modelos de voz do Gemini
DEFAULT_CHUNK_FRAMES = 2048
NOISE_GATE_ATTENUATION_GAIN = 0.15
NOISE_GATE_HOLD_SECONDS = 0.15
BLOCK_OVERLAP_SECONDS = 2.5
PAUSE_WAIT_MAX_SECONDS = 4.0
PAUSE_RMS_THRESHOLD = 0.22


def listar_dispositivos_audio() -> Tuple[List[Dict], List[Dict]]:
    """Lista dispositivos WASAPI disponíveis no sistema Windows.

    Retorna duas listas de dicionários:
        (loopback_devices, mic_devices)
    Onde cada dicionário possui:
        {"index": int, "name": str, "channels": int, "sample_rate": int, "is_default": bool}
    """
    loopback_devices: List[Dict] = []
    mic_devices: List[Dict] = []

    if pyaudio is None:
        logger.warning("PyAudio/pyaudiowpatch não está instalado.")
        # Retorna lista mock para permitir desenvolvimento e testes de interface
        mock_loop = [{
            "index": 1,
            "name": "Alto-falantes (Realtek Audio) [Loopback]",
            "channels": 2,
            "sample_rate": 48000,
            "is_default": True
        }]
        mock_mic = [{
            "index": 2,
            "name": "Microfone (Realtek Audio)",
            "channels": 2,
            "sample_rate": 48000,
            "is_default": True
        }]
        return mock_loop, mock_mic

    p = pyaudio.PyAudio()
    try:
        # Tenta obter a API WASAPI
        wasapi_index = None
        default_output_index = None
        default_input_index = None

        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            wasapi_index = wasapi_info.get("index")
            default_output_index = wasapi_info.get("defaultOutputDevice")
            default_input_index = wasapi_info.get("defaultInputDevice")
        except Exception as e:
            logger.info(f"Host API WASAPI não identificada: {e}. Tentando fallback genérico.")

        # Obtém o dispositivo padrão de loopback (se pyaudiowpatch suportar)
        default_loopback_index = None
        if hasattr(p, "get_default_wasapi_loopback"):
            try:
                def_loop = p.get_default_wasapi_loopback()
                if def_loop:
                    default_loopback_index = def_loop.get("index")
            except Exception as e:
                logger.debug(f"Falha ao obter default WASAPI loopback: {e}")

        # 1. Dispositivos de Saída (Loopback)
        if hasattr(p, "get_loopback_device_info_generator"):
            try:
                for dev in p.get_loopback_device_info_generator():
                    is_def = False
                    if default_loopback_index is not None and dev.get("index") == default_loopback_index:
                        is_def = True
                    elif default_output_index is not None and dev.get("index") == default_output_index:
                        is_def = True

                    loopback_devices.append({
                        "index": int(dev["index"]),
                        "name": str(dev.get("name", f"Dispositivo {dev['index']}")),
                        "channels": int(dev.get("maxInputChannels", 2)),
                        "sample_rate": int(dev.get("defaultSampleRate", 48000)),
                        "is_default": is_def
                    })
            except Exception as e:
                logger.warning(f"Erro ao listar loopbacks via gerador: {e}")

        # 2. Dispositivos de Entrada (Microfones) e varredura geral
        device_count = p.get_device_count()
        for i in range(device_count):
            try:
                dev = p.get_device_info_by_index(i)
                dev_host_api = dev.get("hostApi")

                # Se WASAPI estiver presente, filtrar apenas dispositivos WASAPI
                if wasapi_index is not None and dev_host_api != wasapi_index:
                    continue

                is_loopback = dev.get("isLoopbackDevice", False)
                max_in = dev.get("maxInputChannels", 0)

                # Microfones: canais de entrada > 0 e NÃO é dispositivo de loopback
                if not is_loopback and max_in > 0:
                    is_def = (dev.get("index") == default_input_index)
                    mic_devices.append({
                        "index": int(dev["index"]),
                        "name": str(dev.get("name", f"Microfone {dev['index']}")),
                        "channels": int(max_in),
                        "sample_rate": int(dev.get("defaultSampleRate", 44100)),
                        "is_default": is_def
                    })

                # Se pyaudiowpatch não tiver gerador, captura loopbacks marcados
                if not loopback_devices and is_loopback and max_in > 0:
                    is_def = (dev.get("index") == default_output_index)
                    loopback_devices.append({
                        "index": int(dev["index"]),
                        "name": str(dev.get("name", f"Loopback {dev['index']}")),
                        "channels": int(max_in),
                        "sample_rate": int(dev.get("defaultSampleRate", 48000)),
                        "is_default": is_def
                    })
            except Exception as e:
                logger.debug(f"Erro ao inspecionar dispositivo {i}: {e}")
                continue

        # Fallbacks: garantir que se existirem dispositivos, ao menos um tenha is_default=True
        if loopback_devices and not any(d["is_default"] for d in loopback_devices):
            loopback_devices[0]["is_default"] = True

        if mic_devices and not any(d["is_default"] for d in mic_devices):
            mic_devices[0]["is_default"] = True

    finally:
        p.terminate()

    return loopback_devices, mic_devices


def resample_to_16k(audio_mono: np.ndarray, orig_rate: int, target_rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    """Reamostra um vetor mono int16 para target_rate com filtragem anti-aliasing.

    Usa `scipy.signal.resample_poly` quando disponível; caso contrário, aplica um
    filtro passa-baixa FIR com janela de Hamming em NumPy antes da decimação.
    """
    if orig_rate == target_rate or len(audio_mono) == 0 or orig_rate <= 0 or target_rate <= 0:
        return np.asarray(audio_mono, dtype=np.int16)

    duration = len(audio_mono) / float(orig_rate)
    num_target_samples = int(round(duration * target_rate))
    if num_target_samples == 0:
        return np.empty(0, dtype=np.int16)

    samples_f = audio_mono.astype(np.float64)

    if HAS_SCIPY_SIGNAL and _scipy_resample_poly is not None:
        divisor = math.gcd(int(target_rate), int(orig_rate))
        up = int(target_rate) // divisor
        down = int(orig_rate) // divisor
        resampled = _scipy_resample_poly(samples_f, up, down)
        if len(resampled) > num_target_samples:
            resampled = resampled[:num_target_samples]
        elif len(resampled) < num_target_samples:
            resampled = np.pad(resampled, (0, num_target_samples - len(resampled)), mode="edge")
        return np.clip(np.round(resampled), -32768, 32767).astype(np.int16)

    # Fallback NumPy: filtro passa-baixa FIR com janela de Hamming anti-aliasing antes de decimar
    if target_rate < orig_rate and len(samples_f) >= 8:
        fc = 0.45 * (float(target_rate) / float(orig_rate))
        ratio = float(orig_rate) / float(target_rate)
        num_taps = min(127, max(41, int(round(16.0 * ratio)) | 1))
        if len(samples_f) < num_taps:
            num_taps = max(5, (len(samples_f) - 1) | 1)

        n = np.arange(num_taps, dtype=np.float64) - (num_taps - 1) / 2.0
        kernel = 2.0 * fc * np.sinc(2.0 * fc * n) * np.hamming(num_taps)
        kernel /= np.sum(kernel)

        pad = num_taps // 2
        padded = np.pad(samples_f, pad, mode="edge")
        samples_f = np.convolve(padded, kernel, mode="valid")

    orig_times = np.linspace(0.0, duration, len(samples_f), endpoint=False)
    target_times = np.linspace(0.0, duration, num_target_samples, endpoint=False)

    resampled = np.interp(target_times, orig_times, samples_f)
    return np.clip(np.round(resampled), -32768, 32767).astype(np.int16)


def _apply_highpass_fft(samples: np.ndarray, sample_rate: int, cutoff_hz: float = 80.0) -> np.ndarray:
    """Aplica filtro passa-alta linear-phase (~80 Hz) via FIR Hamming e convolução FFT."""
    if len(samples) < 32 or sample_rate <= cutoff_hz * 2.5:
        return samples

    # Escolhe ordem ímpar proporcional à taxa de amostragem (~601 taps em 16 kHz)
    num_taps = min(1201, max(101, int(round(0.0375 * sample_rate)) | 1))
    max_taps = (len(samples) // 2) * 2 - 1
    if max_taps < 15:
        return samples
    num_taps = min(num_taps, max_taps) | 1

    fc = cutoff_hz / float(sample_rate)
    m = np.arange(num_taps, dtype=np.float64) - (num_taps - 1) / 2.0
    kernel_lp = 2.0 * fc * np.sinc(2.0 * fc * m) * np.hamming(num_taps)
    kernel_lp /= np.sum(kernel_lp)

    # Inversão espectral para obter passa-alta complementar (delta - LPF)
    kernel_hp = -kernel_lp
    kernel_hp[num_taps // 2] += 1.0

    pad = num_taps // 2
    padded = np.pad(samples, pad, mode="edge")
    nfft = 1 << math.ceil(math.log2(len(padded) + num_taps - 1))
    filtered_full = np.fft.irfft(
        np.fft.rfft(padded, n=nfft) * np.fft.rfft(kernel_hp, n=nfft),
        n=nfft,
    )
    return filtered_full[num_taps - 1 : num_taps - 1 + len(samples)]


def enhance_speech_audio(samples_int16: np.ndarray, sample_rate: int) -> np.ndarray:
    """Pré-processa áudio de fala para máxima clareza em reuniões presenciais e online.

    Etapas aplicadas:
    1. Remoção de offset DC e filtragem passa-alta suave (~80 Hz) para eliminar ruído grave.
    2. Normalização dinâmica por janelas (AGC suave de RMS/pico) com guarda de piso de ruído.
    3. Limiter soft-knee anti-clipping em -1 dBFS.
    """
    arr = np.asarray(samples_int16, dtype=np.int16)
    if len(arr) == 0 or sample_rate <= 0:
        return arr.copy()

    # Evita trabalho desnecessário em blocos digitais totalmente silenciosos
    if not np.any(arr):
        return arr.copy()

    x = arr.astype(np.float64) / 32768.0
    x -= np.mean(x)
    x = _apply_highpass_fft(x, sample_rate, cutoff_hz=80.0)

    # Parâmetros do AGC por janelas de 100 ms e limiter em -1 dBFS
    target_rms = 10.0 ** (-18.0 / 20.0)   # ~0.1259 (-18 dBFS)
    peak_limit = 10.0 ** (-1.0 / 20.0)    # ~0.8913 (-1 dBFS)
    silence_rms = 10.0 ** (-48.0 / 20.0)  # ~0.0040 (não amplifica piso de silêncio)
    max_gain = 10.0 ** (16.0 / 20.0)      # +16 dB máx. para participantes distantes na sala
    min_gain = 0.25

    win_len = max(1, int(round(0.10 * sample_rate)))
    num_samples = len(x)
    num_frames = max(1, math.ceil(num_samples / float(win_len)))

    raw_gains = np.ones(num_frames, dtype=np.float64)
    frame_centers = np.zeros(num_frames, dtype=np.float64)

    for idx in range(num_frames):
        start = idx * win_len
        end = min(num_samples, start + win_len)
        seg = x[start:end]
        frame_centers[idx] = 0.5 * (start + end - 1)

        frm_rms = float(np.sqrt(np.mean(seg * seg)))
        frm_peak = float(np.max(np.abs(seg)))

        if frm_rms < silence_rms:
            raw_gains[idx] = 1.0
        else:
            desired = min(max_gain, max(min_gain, target_rms / max(frm_rms, 1e-8)))
            peak_cap = peak_limit / max(frm_peak, 1e-8)
            raw_gains[idx] = min(desired, peak_cap)

    # Suavização assimétrica: ataque rápido ao reduzir ganho, liberação gradual ao elevar
    smooth_gains = np.empty_like(raw_gains)
    smooth_gains[0] = raw_gains[0]
    for idx in range(1, num_frames):
        prev_g = smooth_gains[idx - 1]
        curr_g = raw_gains[idx]
        alpha = 0.65 if curr_g < prev_g else 0.22
        smooth_gains[idx] = prev_g + alpha * (curr_g - prev_g)

    if num_frames == 1:
        gain_env = np.full(num_samples, smooth_gains[0], dtype=np.float64)
    else:
        gain_env = np.interp(np.arange(num_samples, dtype=np.float64), frame_centers, smooth_gains)

    y = x * gain_env

    # Limiter soft-knee em -1 dBFS para preservar transientes sem clipping duro
    knee = 0.85 * peak_limit
    headroom = peak_limit - knee
    abs_y = np.abs(y)
    over_mask = abs_y > knee
    if np.any(over_mask):
        overshoot = (abs_y[over_mask] - knee) / headroom
        y[over_mask] = np.sign(y[over_mask]) * (knee + headroom * np.tanh(overshoot))

    y = np.clip(y, -peak_limit, peak_limit)
    return np.clip(np.round(y * 32768.0), -32768, 32767).astype(np.int16)


def get_wav_info(wav_path: Path) -> Dict:
    """Lê metadados básicos de um arquivo WAV (canais, taxa, duração e tamanho)."""
    wav_path = Path(wav_path)
    info = {
        "channels": 1,
        "sample_rate": TARGET_SAMPLE_RATE,
        "frames": 0,
        "duration_seconds": 0.0,
        "size_bytes": wav_path.stat().st_size if wav_path.exists() else 0,
    }
    try:
        with wave.open(str(wav_path), "rb") as wf:
            ch = int(wf.getnchannels())
            sr = int(wf.getframerate())
            frames = int(wf.getnframes())
            info["channels"] = ch
            info["sample_rate"] = sr
            info["frames"] = frames
            info["duration_seconds"] = (frames / float(sr)) if sr > 0 else 0.0
    except Exception as e:
        logger.debug(f"Não foi possível ler metadados de {wav_path}: {e}")
    return info


def _find_pause_cut_frame(audio_array: np.ndarray, nominal_frame: int, sample_rate: int, search_window_sec: float = 2.0) -> int:
    """Encontra o ponto de menor energia (~150 ms) ao redor do corte nominal para não partir palavras."""
    total_frames = len(audio_array)
    radius = int(round(search_window_sec * sample_rate))
    start = max(0, nominal_frame - radius)
    end = min(total_frames, nominal_frame + radius)
    win = max(1, int(round(0.15 * sample_rate)))
    if end - start <= win:
        return min(total_frames, nominal_frame)

    segment = audio_array[start:end].astype(np.float32)
    if segment.ndim > 1:
        mono_energy = np.mean(segment * segment, axis=1)
    else:
        mono_energy = segment * segment

    # Soma móvel via cumsum em O(N) para localizar a janela de 150 ms mais silenciosa
    csum = np.cumsum(np.insert(mono_energy, 0, 0.0))
    win_energy = csum[win:] - csum[:-win]
    best_rel = int(np.argmin(win_energy)) + win // 2
    return min(total_frames, max(1, start + best_rel))


def slice_wav_for_retranscription(
    source_wav_path: Path,
    temp_dir: Path,
    block_duration_seconds: int = 180,
    overlap_seconds: float = BLOCK_OVERLAP_SECONDS,
    apply_enhancement: Optional[bool] = None,
) -> List[Tuple[Path, int, float, float, bool]]:
    """Fatia um WAV salvo em blocos temporários (16 kHz) com pausa inteligente e overlap de 2,5s para re-transcrição.

    Retorna uma lista de tuplas: `(block_wav_path, block_index, start_seconds, end_seconds, is_final)`.
    """
    source_wav_path = Path(source_wav_path)
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    with wave.open(str(source_wav_path), "rb") as wf:
        nchannels = int(wf.getnchannels())
        sampwidth = int(wf.getsampwidth())
        orig_rate = int(wf.getframerate())
        total_frames = int(wf.getnframes())
        if sampwidth != 2:
            raise ValueError(f"Formato PCM não suportado ({sampwidth * 8}-bit); esperado WAV 16-bit.")
        if total_frames == 0 or orig_rate <= 0:
            return []
        raw_bytes = wf.readframes(total_frames)

    samples = np.frombuffer(raw_bytes, dtype=np.int16)
    if nchannels > 1:
        samples = samples.reshape(-1, nchannels)
    out_channels = 1 if nchannels == 1 else 2
    if nchannels > 2:
        # Preserva apenas L/R se houver mais canais
        samples = samples[:, :2]

    should_enhance = (orig_rate != TARGET_SAMPLE_RATE) if apply_enhancement is None else bool(apply_enhancement)
    step_frames = max(orig_rate * 10, int(round(max(30, block_duration_seconds) * orig_rate)))
    ov_frames = int(round(max(0.0, overlap_seconds) * orig_rate))
    min_tail_frames = int(round(5.0 * orig_rate))

    tasks: List[Tuple[Path, int, float, float, bool]] = []
    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    cursor = 0
    block_idx = 0

    while cursor < total_frames:
        block_idx += 1
        start_frame = max(0, cursor - ov_frames) if block_idx > 1 else 0
        nominal_end = cursor + step_frames

        if nominal_end >= total_frames or (total_frames - nominal_end) <= min_tail_frames:
            end_frame = total_frames
        else:
            end_frame = _find_pause_cut_frame(samples, nominal_end, orig_rate)
            if end_frame <= cursor + orig_rate * 5:
                end_frame = min(total_frames, nominal_end)

        is_final = end_frame >= total_frames
        chunk = samples[start_frame:end_frame]

        if out_channels == 1:
            mono = chunk if chunk.ndim == 1 else chunk[:, 0]
            out_16k = resample_to_16k(mono, orig_rate, TARGET_SAMPLE_RATE)
            if should_enhance:
                out_16k = enhance_speech_audio(out_16k, TARGET_SAMPLE_RATE)
            pcm_bytes = out_16k.tobytes()
        else:
            ch_l = resample_to_16k(chunk[:, 0], orig_rate, TARGET_SAMPLE_RATE)
            ch_r = resample_to_16k(chunk[:, 1], orig_rate, TARGET_SAMPLE_RATE)
            if should_enhance:
                ch_l = enhance_speech_audio(ch_l, TARGET_SAMPLE_RATE)
                ch_r = enhance_speech_audio(ch_r, TARGET_SAMPLE_RATE)
            tlen = max(len(ch_l), len(ch_r))
            stereo = np.empty((tlen, 2), dtype=np.int16)
            stereo[:, 0] = ch_l[:tlen] if len(ch_l) >= tlen else np.pad(ch_l, (0, tlen - len(ch_l)))
            stereo[:, 1] = ch_r[:tlen] if len(ch_r) >= tlen else np.pad(ch_r, (0, tlen - len(ch_r)))
            pcm_bytes = stereo.tobytes()

        block_path = temp_dir / f"retrans_{timestamp_str}_{block_idx:03d}.wav"
        with wave.open(str(block_path), "wb") as bwf:
            bwf.setnchannels(out_channels)
            bwf.setsampwidth(2)
            bwf.setframerate(TARGET_SAMPLE_RATE)
            bwf.writeframes(pcm_bytes)

        tasks.append(
            (
                block_path,
                block_idx,
                start_frame / float(orig_rate),
                end_frame / float(orig_rate),
                is_final,
            )
        )
        cursor = end_frame

    return tasks


def record_mic_sample(
    mic_index: int,
    duration_seconds: float = 5.0,
    target_rate: int = TARGET_SAMPLE_RATE,
) -> np.ndarray:
    """Captura uma amostra curta do microfone selecionado em 16 kHz mono int16 (para cadastro de voiceprint)."""
    if pyaudio is None:
        raise RuntimeError("PyAudio/pyaudiowpatch não está instalado no ambiente.")

    p = pyaudio.PyAudio()
    stream = None
    chunks: List[np.ndarray] = []
    try:
        mic_info = p.get_device_info_by_index(int(mic_index))
        mic_channels = max(1, min(int(mic_info.get("maxInputChannels", 1)), 2))
        mic_rate = int(mic_info.get("defaultSampleRate", 44100))

        stream = p.open(
            format=pyaudio.paInt16,
            channels=mic_channels,
            rate=mic_rate,
            input=True,
            input_device_index=int(mic_index),
            frames_per_buffer=DEFAULT_CHUNK_FRAMES,
        )

        total_frames_needed = max(DEFAULT_CHUNK_FRAMES, int(round(max(1.0, duration_seconds) * mic_rate)))
        frames_read = 0
        while frames_read < total_frames_needed:
            data = stream.read(DEFAULT_CHUNK_FRAMES, exception_on_overflow=False)
            if not data:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            if mic_channels > 1:
                arr = arr.reshape(-1, mic_channels).mean(axis=1).astype(np.int16)
            chunks.append(arr)
            frames_read += len(arr)

        if not chunks:
            return np.empty(0, dtype=np.int16)
        raw = np.concatenate(chunks)[:total_frames_needed]
        return resample_to_16k(raw, mic_rate, target_rate)
    finally:
        if stream is not None:
            try:
                if hasattr(stream, "is_active") and stream.is_active():
                    stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        try:
            p.terminate()
        except Exception:
            pass


def calc_rms_normalized(samples_int16: np.ndarray) -> float:
    """Calcula o valor RMS normalizado de 0.0 a 1.0 com escala logarítmica para visualização precisa em VU meters."""
    if len(samples_int16) == 0:
        return 0.0

    float_samples = samples_int16.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(float_samples ** 2)))
    if rms <= 1e-4:
        return 0.0

    # Converte para escala em decibéis (-60dB a 0dB mapeados para 0.0 a 1.0)
    db = 20.0 * math.log10(max(rms, 1e-4))
    norm = (db + 60.0) / 60.0
    return float(np.clip(norm, 0.0, 1.0))


class AudioEngine:
    """Motor de captura e sincronização de áudio para reuniões."""

    def __init__(
        self,
        temp_dir: Path,
        on_rms_update: Optional[Callable[[float, float], None]] = None,
        on_block_ready: Optional[Callable[[Path, int, float, float, bool], None]] = None,
        noise_gate_enabled: bool = True,
        noise_gate_threshold: float = 0.015,
    ):
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self.on_rms_update = on_rms_update
        self.on_block_ready = on_block_ready
        self.noise_gate_enabled = noise_gate_enabled
        self.noise_gate_threshold = noise_gate_threshold

        self.is_recording = False
        self.is_mic_muted = False
        self.block_duration_seconds = 180

        self._pyaudio_instance: Optional[pyaudio.PyAudio] = None
        self._loopback_stream = None
        self._mic_stream = None

        self._loopback_rate = 48000
        self._loopback_channels = 2
        self._mic_rate = 44100
        self._mic_channels = 1

        self._loopback_thread: Optional[threading.Thread] = None
        self._mic_thread: Optional[threading.Thread] = None
        self._slicer_thread: Optional[threading.Thread] = None
        self._vu_thread: Optional[threading.Thread] = None

        self._raw_loop_chunks: List[np.ndarray] = []
        self._raw_mic_chunks: List[np.ndarray] = []
        self._overlap_loop_raw: np.ndarray = np.empty(0, dtype=np.int16)
        self._overlap_mic_raw: np.ndarray = np.empty(0, dtype=np.int16)
        self._buffer_lock = threading.Lock()

        self._current_zoom_rms = 0.0
        self._current_mic_rms = 0.0
        self._last_loop_chunk_time = 0.0
        self._last_mic_chunk_time = 0.0
        self._mic_gate_gain = 1.0
        self._mic_gate_last_open_time = 0.0
        self._block_index = 0
        self._start_time = 0.0

        self.session_wav_path: Optional[Path] = None
        self._session_wave_writer: Optional[wave.Wave_write] = None
        self._session_wav_channels: int = 0
        self._session_frames_written: int = 0

    def set_noise_gate(self, enabled: bool) -> None:
        """Habilita ou desabilita o noise gate dinamicamente."""
        self.noise_gate_enabled = enabled
        logger.info(f"Noise gate ajustado para: {enabled}")

    def set_mic_muted(self, muted: bool) -> None:
        """Silencia ou ativa a captura do microfone em tempo real (Modo Ausente)."""
        self.is_mic_muted = muted
        logger.info(f"Microfone silenciado: {muted}")

    def start_recording(
        self,
        loopback_index: Optional[int],
        mic_index: Optional[int],
        block_duration_seconds: int = 180,
        session_wav_path: Optional[Path] = None,
    ) -> bool:
        """Inicia a gravação sincronizada (suporta Loopback, Microfone ou Ambos)."""
        if self.is_recording:
            logger.warning("Gravação já em andamento.")
            return False

        if loopback_index is None and mic_index is None:
            logger.error("Nenhum dispositivo de áudio (Loopback ou Mic) foi selecionado.")
            return False

        if pyaudio is None:
            logger.error("PyAudio não está disponível no ambiente.")
            return False

        self.block_duration_seconds = max(30, block_duration_seconds)
        self.is_recording = True
        self._block_index = 0
        self._start_time = time.time()
        self._raw_loop_chunks = []
        self._raw_mic_chunks = []
        self._overlap_loop_raw = np.empty(0, dtype=np.int16)
        self._overlap_mic_raw = np.empty(0, dtype=np.int16)
        self._current_zoom_rms = 0.0
        self._current_mic_rms = 0.0
        self._last_loop_chunk_time = 0.0
        self._last_mic_chunk_time = 0.0
        self._mic_gate_gain = 1.0
        self._mic_gate_last_open_time = time.time()
        self.session_wav_path = Path(session_wav_path) if session_wav_path else None
        self._session_wave_writer = None
        self._session_wav_channels = 0
        self._session_frames_written = 0

        try:
            self._pyaudio_instance = pyaudio.PyAudio()

            # Configura dispositivo de loopback (se selecionado)
            if loopback_index is not None:
                loop_info = self._pyaudio_instance.get_device_info_by_index(loopback_index)
                self._loopback_channels = max(1, int(loop_info.get("maxInputChannels", 2)))
                self._loopback_rate = int(loop_info.get("defaultSampleRate", 48000))

                logger.info(
                    f"Abrindo Loopback [{loopback_index}]: {loop_info.get('name')}, "
                    f"taxa={self._loopback_rate}Hz, canais={self._loopback_channels}"
                )
                self._loopback_stream = self._pyaudio_instance.open(
                    format=pyaudio.paInt16,
                    channels=self._loopback_channels,
                    rate=self._loopback_rate,
                    input=True,
                    input_device_index=loopback_index,
                    frames_per_buffer=DEFAULT_CHUNK_FRAMES,
                )
            else:
                logger.info("Loopback desativado pelo usuário (modo apenas microfone/ditado).")
                self._loopback_stream = None
                self._loopback_rate = 48000

            # Configura microfone (se selecionado)
            if mic_index is not None:
                mic_info = self._pyaudio_instance.get_device_info_by_index(mic_index)
                self._mic_channels = max(1, min(int(mic_info.get("maxInputChannels", 1)), 2))
                self._mic_rate = int(mic_info.get("defaultSampleRate", 44100))

                logger.info(
                    f"Abrindo Microfone [{mic_index}]: {mic_info.get('name')}, "
                    f"taxa={self._mic_rate}Hz, canais={self._mic_channels}"
                )
                self._mic_stream = self._pyaudio_instance.open(
                    format=pyaudio.paInt16,
                    channels=self._mic_channels,
                    rate=self._mic_rate,
                    input=True,
                    input_device_index=mic_index,
                    frames_per_buffer=DEFAULT_CHUNK_FRAMES,
                )
            else:
                logger.info("Microfone desativado pelo usuário (captura somente áudio do sistema).")
                self._mic_stream = None
                self._mic_rate = 44100

            # Dispara threads de captura ativas
            if self._loopback_stream is not None:
                self._loopback_thread = threading.Thread(
                    target=self._capture_loopback_loop, daemon=True, name="AudioLoopbackCapture"
                )
                self._loopback_thread.start()
            else:
                self._loopback_thread = None

            if self._mic_stream is not None:
                self._mic_thread = threading.Thread(
                    target=self._capture_mic_loop, daemon=True, name="AudioMicCapture"
                )
                self._mic_thread.start()
            else:
                self._mic_thread = None

            # Dispara thread fatiadora de blocos
            self._slicer_thread = threading.Thread(
                target=self._slicer_loop, daemon=True, name="AudioBlockSlicer"
            )
            self._slicer_thread.start()

            # Dispara thread de atualização visual do VU meter
            self._vu_thread = threading.Thread(
                target=self._vu_meter_loop, daemon=True, name="AudioVUMeter"
            )
            self._vu_thread.start()

            logger.info("Motor de áudio iniciado com sucesso.")
            return True

        except Exception as e:
            logger.exception(f"Falha crítica ao iniciar captura de áudio: {e}")
            self.stop_recording(finalize_block=False)
            return False

    def _capture_loopback_loop(self) -> None:
        """Loop de captura contínua do áudio interno (WASAPI Loopback)."""
        logger.debug("Iniciando loop de captura de áudio do Loopback.")
        stream = self._loopback_stream
        channels = self._loopback_channels

        while self.is_recording and stream is not None:
            try:
                # Evita chamadas bloqueantes em C quando não há áudio reproduzindo
                if hasattr(stream, "get_read_available"):
                    avail = stream.get_read_available()
                    if avail < DEFAULT_CHUNK_FRAMES:
                        time.sleep(0.01)
                        continue

                data = stream.read(DEFAULT_CHUNK_FRAMES, exception_on_overflow=False)
                if not data:
                    time.sleep(0.01)
                    continue

                samples = np.frombuffer(data, dtype=np.int16)
                if channels > 1:
                    # Converte para mono fazendo a média entre os canais
                    samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)

                # Atualiza RMS para o medidor de volume do Zoom
                rms = calc_rms_normalized(samples)
                self._current_zoom_rms = max(rms, self._current_zoom_rms * 0.82)

                with self._buffer_lock:
                    self._raw_loop_chunks.append(samples)
                    self._last_loop_chunk_time = time.time()

            except Exception as e:
                if self.is_recording:
                    logger.debug(f"Exceção temporária na leitura do loopback: {e}")
                time.sleep(0.01)

        logger.debug("Finalizado loop de captura de áudio do Loopback.")

    def _apply_mic_noise_gate(
        self, samples: np.ndarray, rms: float, now: Optional[float] = None
    ) -> Tuple[np.ndarray, float]:
        """Aplica noise gate suave com janela de hold (~150ms) e atenuação 0.15x em vez de corte seco."""
        if not self.noise_gate_enabled or len(samples) == 0:
            self._mic_gate_gain = 1.0
            return samples, rms

        ts = time.time() if now is None else now
        if rms >= self.noise_gate_threshold:
            self._mic_gate_last_open_time = ts
            target_gain = 1.0
        elif (ts - self._mic_gate_last_open_time) <= NOISE_GATE_HOLD_SECONDS:
            target_gain = 1.0
        else:
            target_gain = NOISE_GATE_ATTENUATION_GAIN

        prev_gain = self._mic_gate_gain
        self._mic_gate_gain = target_gain

        if prev_gain == 1.0 and target_gain == 1.0:
            return samples, rms

        n = len(samples)
        if target_gain > prev_gain:
            # Ataque rápido (~128 amostras) preserva consoantes iniciais
            ramp_len = min(128, n)
            env = np.empty(n, dtype=np.float32)
            env[:ramp_len] = np.linspace(prev_gain, target_gain, ramp_len, dtype=np.float32)
            if ramp_len < n:
                env[ramp_len:] = target_gain
        elif target_gain < prev_gain:
            # Fechamento progressivo ao longo do frame evita degraus abruptos
            env = np.linspace(prev_gain, target_gain, n, dtype=np.float32)
        else:
            env = target_gain

        attenuated = np.clip(np.round(samples.astype(np.float32) * env), -32768, 32767).astype(np.int16)
        return attenuated, calc_rms_normalized(attenuated)

    def _capture_mic_loop(self) -> None:
        """Loop de captura contínua do microfone local."""
        logger.debug("Iniciando loop de captura do microfone.")
        stream = self._mic_stream
        channels = self._mic_channels

        while self.is_recording and stream is not None:
            try:
                if hasattr(stream, "get_read_available"):
                    avail = stream.get_read_available()
                    if avail < DEFAULT_CHUNK_FRAMES:
                        time.sleep(0.01)
                        continue

                data = stream.read(DEFAULT_CHUNK_FRAMES, exception_on_overflow=False)
                if not data:
                    time.sleep(0.01)
                    continue

                samples = np.frombuffer(data, dtype=np.int16)
                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)

                # Verifica modo mudo
                if self.is_mic_muted:
                    samples = np.zeros_like(samples)
                    self._current_mic_rms = 0.0
                    self._mic_gate_gain = 0.0
                else:
                    rms = calc_rms_normalized(samples)
                    samples, gated_rms = self._apply_mic_noise_gate(samples, rms)
                    self._current_mic_rms = max(gated_rms, self._current_mic_rms * 0.82)

                with self._buffer_lock:
                    self._raw_mic_chunks.append(samples)
                    self._last_mic_chunk_time = time.time()

            except Exception as e:
                if self.is_recording:
                    logger.debug(f"Exceção temporária na leitura do microfone: {e}")
                time.sleep(0.01)

        logger.debug("Finalizado loop de captura do microfone.")

    def _vu_meter_loop(self) -> None:
        """Envia medições de volume amortecidas para a GUI via callback."""
        while self.is_recording:
            try:
                if self.on_rms_update:
                    self.on_rms_update(self._current_zoom_rms, self._current_mic_rms)

                # Decaimento suave dos medidores quando não há sinal recente
                self._current_zoom_rms = max(0.0, self._current_zoom_rms * 0.88)
                self._current_mic_rms = max(0.0, self._current_mic_rms * 0.88)
                time.sleep(0.05)
            except Exception:
                time.sleep(0.05)

        # Ao parar, zera os medidores
        if self.on_rms_update:
            try:
                self.on_rms_update(0.0, 0.0)
            except Exception:
                pass

    def _tail_rms(self, chunks: List[np.ndarray], sample_rate: int, window_sec: float = 0.20) -> float:
        """Calcula o RMS normalizado dos últimos `window_sec` segundos de um buffer de chunks."""
        if not chunks or sample_rate <= 0:
            return 0.0
        need_samples = max(1, int(round(window_sec * sample_rate)))
        collected: List[np.ndarray] = []
        total = 0
        for chunk in reversed(chunks):
            collected.append(chunk)
            total += len(chunk)
            if total >= need_samples:
                break
        tail = np.concatenate(collected[::-1])[-need_samples:]
        return calc_rms_normalized(tail)

    def _is_natural_pause(self, now: Optional[float] = None) -> bool:
        """Retorna True se os fluxos ativos estiverem em janela de baixa energia (pausa natural)."""
        ts = time.time() if now is None else now
        with self._buffer_lock:
            loop_active_speech = False
            if self._loopback_stream is not None and self._raw_loop_chunks:
                recent_loop = (ts - self._last_loop_chunk_time) <= 0.35 if self._last_loop_chunk_time > 0 else True
                if recent_loop:
                    loop_rms = self._tail_rms(self._raw_loop_chunks, self._loopback_rate)
                    loop_active_speech = loop_rms > PAUSE_RMS_THRESHOLD

            mic_active_speech = False
            if self._mic_stream is not None and not self.is_mic_muted and self._raw_mic_chunks:
                recent_mic = (ts - self._last_mic_chunk_time) <= 0.35 if self._last_mic_chunk_time > 0 else True
                if recent_mic:
                    mic_rms = self._tail_rms(self._raw_mic_chunks, self._mic_rate)
                    mic_active_speech = mic_rms > PAUSE_RMS_THRESHOLD

        return not (loop_active_speech or mic_active_speech)

    def _slicer_loop(self) -> None:
        """Monitora o acúmulo de áudio e fatia em pausas naturais (espera até 4s) após block_duration_seconds."""
        logger.debug("Iniciando fatiador inteligente de blocos de áudio.")
        last_slice_time = time.time()
        pending_slice_since: Optional[float] = None

        while self.is_recording:
            time.sleep(0.2)
            now = time.time()
            elapsed = now - last_slice_time
            if elapsed >= self.block_duration_seconds:
                if pending_slice_since is None:
                    pending_slice_since = now

                wait_elapsed = now - pending_slice_since
                if self._is_natural_pause(now=now) or wait_elapsed >= PAUSE_WAIT_MAX_SECONDS:
                    self._create_block_file(is_final=False)
                    last_slice_time = time.time()
                    pending_slice_since = None

    def _create_block_file(self, is_final: bool = False) -> Optional[Path]:
        """Extrai áudio acumulado com overlap de 2,5s, reamostra a 16 kHz, aplica DSP e salva Mono/Estéreo WAV."""
        with self._buffer_lock:
            if not self._raw_loop_chunks and not self._raw_mic_chunks:
                logger.debug("Nenhum dado de áudio no buffer para fatiar.")
                return None

            if self._raw_loop_chunks:
                new_loop_raw = np.concatenate(self._raw_loop_chunks)
                self._raw_loop_chunks = []
            else:
                new_loop_raw = np.empty(0, dtype=np.int16)

            if self._raw_mic_chunks:
                new_mic_raw = np.concatenate(self._raw_mic_chunks)
                self._raw_mic_chunks = []
            else:
                new_mic_raw = np.empty(0, dtype=np.int16)

        # Duração em segundos do sinal recém-capturado (antes do prefixo de overlap)
        new_dur_loop = len(new_loop_raw) / float(self._loopback_rate) if self._loopback_rate > 0 else 0.0
        new_dur_mic = len(new_mic_raw) / float(self._mic_rate) if self._mic_rate > 0 else 0.0
        new_max_duration = max(new_dur_loop, new_dur_mic)

        if new_max_duration < 1.0 and not is_final:
            return None

        # Injeta buffer circular de 2,5s do bloco anterior para continuidade semântica na fronteira
        ov_dur_loop = (len(self._overlap_loop_raw) / float(self._loopback_rate)) if self._loopback_rate > 0 else 0.0
        ov_dur_mic = (len(self._overlap_mic_raw) / float(self._mic_rate)) if self._mic_rate > 0 else 0.0
        prev_ov_duration = max(ov_dur_loop, ov_dur_mic)
        ov_frames_16k = int(round(prev_ov_duration * TARGET_SAMPLE_RATE))

        if len(self._overlap_loop_raw) > 0:
            loop_raw = np.concatenate([self._overlap_loop_raw, new_loop_raw]) if len(new_loop_raw) > 0 else self._overlap_loop_raw
        elif prev_ov_duration > 0.0 and len(new_loop_raw) > 0 and self._loopback_rate > 0:
            pad_ov = np.zeros(int(round(prev_ov_duration * self._loopback_rate)), dtype=np.int16)
            loop_raw = np.concatenate([pad_ov, new_loop_raw])
        else:
            loop_raw = new_loop_raw

        if len(self._overlap_mic_raw) > 0:
            mic_raw = np.concatenate([self._overlap_mic_raw, new_mic_raw]) if len(new_mic_raw) > 0 else self._overlap_mic_raw
        elif prev_ov_duration > 0.0 and len(new_mic_raw) > 0 and self._mic_rate > 0:
            pad_ov = np.zeros(int(round(prev_ov_duration * self._mic_rate)), dtype=np.int16)
            mic_raw = np.concatenate([pad_ov, new_mic_raw])
        else:
            mic_raw = new_mic_raw

        # Atualiza ou limpa os prefixos de overlap para o próximo bloco
        if is_final:
            self._overlap_loop_raw = np.empty(0, dtype=np.int16)
            self._overlap_mic_raw = np.empty(0, dtype=np.int16)
        else:
            loop_ov_samples = int(round(BLOCK_OVERLAP_SECONDS * self._loopback_rate)) if self._loopback_rate > 0 else 0
            mic_ov_samples = int(round(BLOCK_OVERLAP_SECONDS * self._mic_rate)) if self._mic_rate > 0 else 0
            self._overlap_loop_raw = (
                loop_raw[-loop_ov_samples:].copy()
                if (len(new_loop_raw) > 0 and loop_ov_samples > 0)
                else np.empty(0, dtype=np.int16)
            )
            self._overlap_mic_raw = (
                mic_raw[-mic_ov_samples:].copy()
                if (len(new_mic_raw) > 0 and mic_ov_samples > 0)
                else np.empty(0, dtype=np.int16)
            )

        is_presencial_mono = self._loopback_stream is None and self._mic_stream is not None

        self._block_index += 1
        block_num = self._block_index
        timestamp_str = time.strftime("%Y%m%d_%H%M%S")
        wav_path = self.temp_dir / f"block_{timestamp_str}_{block_num:03d}.wav"

        if is_presencial_mono:
            # Modo Presencial (Mic-Only): gera WAV Mono 16 kHz após HPF 80 Hz + AGC + Limiter
            if len(mic_raw) == 0:
                return None
            mic_16k = resample_to_16k(mic_raw, self._mic_rate, TARGET_SAMPLE_RATE)
            mic_16k = enhance_speech_audio(mic_16k, TARGET_SAMPLE_RATE)
            target_len = len(mic_16k)
            if target_len == 0:
                return None
            nchannels = 1
            session_pcm_array = mic_16k
            pcm_bytes = mic_16k.tobytes()
        else:
            # Modo Reunião Online (Estéreo): L = Loopback, R = Microfone
            dur_loop = len(loop_raw) / float(self._loopback_rate) if self._loopback_rate > 0 else 0.0
            dur_mic = len(mic_raw) / float(self._mic_rate) if self._mic_rate > 0 else 0.0

            if len(loop_raw) > 0:
                loop_16k = enhance_speech_audio(
                    resample_to_16k(loop_raw, self._loopback_rate, TARGET_SAMPLE_RATE),
                    TARGET_SAMPLE_RATE,
                )
            else:
                loop_16k = np.zeros(int(round(dur_mic * TARGET_SAMPLE_RATE)), dtype=np.int16)

            if len(mic_raw) > 0:
                mic_16k = enhance_speech_audio(
                    resample_to_16k(mic_raw, self._mic_rate, TARGET_SAMPLE_RATE),
                    TARGET_SAMPLE_RATE,
                )
            else:
                mic_16k = np.zeros(len(loop_16k), dtype=np.int16)

            target_len = max(len(loop_16k), len(mic_16k))
            if target_len == 0:
                return None

            if len(loop_16k) < target_len:
                loop_16k = np.pad(loop_16k, (0, target_len - len(loop_16k)), mode="constant")
            elif len(loop_16k) > target_len:
                loop_16k = loop_16k[:target_len]

            if len(mic_16k) < target_len:
                mic_16k = np.pad(mic_16k, (0, target_len - len(mic_16k)), mode="constant")
            elif len(mic_16k) > target_len:
                mic_16k = mic_16k[:target_len]

            stereo_array = np.empty((target_len, 2), dtype=np.int16)
            stereo_array[:, 0] = loop_16k
            stereo_array[:, 1] = mic_16k
            nchannels = 2
            session_pcm_array = stereo_array
            pcm_bytes = stereo_array.tobytes()

        # Anexa apenas os frames novos (sem repetir os 2,5s de overlap) na gravação oficial da sessão
        self._append_to_session_wav(session_pcm_array, nchannels, ov_frames_16k)

        try:
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(nchannels)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(TARGET_SAMPLE_RATE)
                wf.writeframes(pcm_bytes)

            block_duration = target_len / float(TARGET_SAMPLE_RATE)
            end_time = (time.time() - self._start_time) if self._start_time > 0.0 else block_duration
            start_time = max(0.0, end_time - block_duration)

            mode_desc = "Mono/Presencial" if nchannels == 1 else "Estéreo/Online"
            logger.info(
                f"Bloco {block_num} ({mode_desc}) gerado com sucesso: {wav_path.name} "
                f"({block_duration:.1f}s, [{start_time:.0f}s -> {end_time:.0f}s])"
            )

            if self.on_block_ready:
                self.on_block_ready(wav_path, block_num, start_time, end_time, is_final)

            return wav_path

        except Exception as e:
            logger.exception(f"Erro ao salvar arquivo WAV do bloco {block_num}: {e}")
            return None

    def _append_to_session_wav(self, pcm_array: np.ndarray, nchannels: int, skip_prefix_frames: int) -> None:
        """Grava continuamente apenas os frames inéditos do bloco no arquivo WAV oficial da reunião."""
        if not self.session_wav_path or len(pcm_array) == 0:
            return

        start_idx = max(0, min(len(pcm_array), int(skip_prefix_frames)))
        delta_frames = pcm_array[start_idx:]
        if len(delta_frames) == 0:
            return

        try:
            if self._session_wave_writer is None:
                self.session_wav_path.parent.mkdir(parents=True, exist_ok=True)
                wf = wave.open(str(self.session_wav_path), "wb")
                wf.setnchannels(nchannels)
                wf.setsampwidth(2)
                wf.setframerate(TARGET_SAMPLE_RATE)
                self._session_wave_writer = wf
                self._session_wav_channels = nchannels

            if self._session_wav_channels == nchannels:
                self._session_wave_writer.writeframes(delta_frames.tobytes())
                self._session_frames_written += len(delta_frames)
        except Exception as e:
            logger.exception(f"Erro ao gravar áudio contínuo da sessão em {self.session_wav_path}: {e}")

    def _close_session_wave_writer(self) -> Optional[Path]:
        """Fecha e finaliza o cabeçalho RIFF do arquivo WAV oficial da reunião."""
        saved_path = self.session_wav_path
        writer = self._session_wave_writer
        self._session_wave_writer = None

        if writer is not None:
            try:
                writer.close()
                logger.info(
                    f"Gravação oficial da reunião salva em: {saved_path} "
                    f"({self._session_frames_written / float(TARGET_SAMPLE_RATE):.1f}s)"
                )
            except Exception as e:
                logger.warning(f"Erro ao fechar WAV da sessão {saved_path}: {e}")

        if saved_path and self._session_frames_written == 0 and saved_path.exists():
            try:
                saved_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        return saved_path if (saved_path and saved_path.exists()) else None

    def stop_recording(self, finalize_block: bool = True) -> None:
        """Interrompe a captura de áudio com encerramento seguro e não bloqueante."""
        if not self.is_recording:
            return

        logger.info("Encerrando captura de áudio...")
        self.is_recording = False

        # Aguarda finalização limpa das threads de captura
        for thread in [self._loopback_thread, self._mic_thread, self._slicer_thread, self._vu_thread]:
            if thread and thread.is_alive():
                try:
                    thread.join(timeout=0.6)
                except Exception:
                    pass

        if finalize_block:
            try:
                self._create_block_file(is_final=True)
            except Exception as e:
                logger.exception(f"Erro ao empacotar bloco final: {e}")

        self._close_session_wave_writer()

        # Fecha streams com verificações de estado seguro
        for attr in ["_loopback_stream", "_mic_stream"]:
            stream = getattr(self, attr, None)
            if stream is not None:
                try:
                    if hasattr(stream, "is_active") and stream.is_active():
                        stream.stop_stream()
                except Exception as e:
                    logger.debug(f"Aviso ao parar stream {attr}: {e}")
                try:
                    stream.close()
                except Exception as e:
                    logger.debug(f"Aviso ao fechar stream {attr}: {e}")
                setattr(self, attr, None)

        if self._pyaudio_instance is not None:
            try:
                self._pyaudio_instance.terminate()
            except Exception as e:
                logger.debug(f"Aviso ao terminar instância PyAudio: {e}")
            self._pyaudio_instance = None

        logger.info("Motor de áudio encerrado com sucesso.")

