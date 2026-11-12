"""Motor de áudio do OmniScribe AI com suporte a WASAPI Loopback (sistema/reuniões) e microfone local.

Realiza captura dual sincronizada, resampling para 16kHz mono via NumPy,
noise gate configurável, intercalação em WAV estéreo e cálculo de VU meter em tempo real.
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
    """Reamostra um vetor mono int16 para target_rate usando np.interp (rápido e sem dependências externas)."""
    if orig_rate == target_rate or len(audio_mono) == 0:
        return audio_mono

    duration = len(audio_mono) / float(orig_rate)
    num_target_samples = int(round(duration * target_rate))
    if num_target_samples == 0:
        return np.empty(0, dtype=np.int16)

    orig_times = np.linspace(0.0, duration, len(audio_mono), endpoint=False)
    target_times = np.linspace(0.0, duration, num_target_samples, endpoint=False)

    resampled = np.interp(target_times, orig_times, audio_mono.astype(np.float32))
    return np.clip(resampled, -32768, 32767).astype(np.int16)


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
        self._buffer_lock = threading.Lock()

        self._current_zoom_rms = 0.0
        self._current_mic_rms = 0.0
        self._block_index = 0
        self._start_time = 0.0

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
        self._current_zoom_rms = 0.0
        self._current_mic_rms = 0.0

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

            except Exception as e:
                if self.is_recording:
                    logger.debug(f"Exceção temporária na leitura do loopback: {e}")
                time.sleep(0.01)

        logger.debug("Finalizado loop de captura de áudio do Loopback.")

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
                else:
                    rms = calc_rms_normalized(samples)
                    # Noise gate: zera o bloco se o nível RMS estiver abaixo do limiar
                    if self.noise_gate_enabled and rms < self.noise_gate_threshold:
                        samples = np.zeros_like(samples)
                        rms = 0.0

                    self._current_mic_rms = max(rms, self._current_mic_rms * 0.82)

                with self._buffer_lock:
                    self._raw_mic_chunks.append(samples)

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

    def _slicer_loop(self) -> None:
        """Monitora o acúmulo de áudio e fatiamento automático a cada N segundos."""
        logger.debug("Iniciando fatiador periódico de blocos de áudio.")
        last_slice_time = time.time()

        while self.is_recording:
            time.sleep(0.5)
            elapsed = time.time() - last_slice_time
            if elapsed >= self.block_duration_seconds:
                self._create_block_file(is_final=False)
                last_slice_time = time.time()

    def _create_block_file(self, is_final: bool = False) -> Optional[Path]:
        """Extrai os dados acumulados, reamostra para 16kHz, intercala em estéreo e salva em WAV."""
        with self._buffer_lock:
            if not self._raw_loop_chunks and not self._raw_mic_chunks:
                logger.debug("Nenhum dado de áudio no buffer para fatiar.")
                return None

            if self._raw_loop_chunks:
                loop_raw = np.concatenate(self._raw_loop_chunks)
                self._raw_loop_chunks = []
            else:
                loop_raw = np.empty(0, dtype=np.int16)

            if self._raw_mic_chunks:
                mic_raw = np.concatenate(self._raw_mic_chunks)
                self._raw_mic_chunks = []
            else:
                mic_raw = np.empty(0, dtype=np.int16)

        # Duração em segundos do sinal original
        dur_loop = len(loop_raw) / float(self._loopback_rate) if self._loopback_rate > 0 else 0.0
        dur_mic = len(mic_raw) / float(self._mic_rate) if self._mic_rate > 0 else 0.0
        max_duration = max(dur_loop, dur_mic)

        if max_duration < 1.0 and not is_final:
            # Trecho muito curto, descarta se não for o encerramento da reunião
            return None

        # Reamostra ambos para 16kHz mono (se um canal estiver vazio, preenche com silêncio)
        if len(loop_raw) > 0:
            loop_16k = resample_to_16k(loop_raw, self._loopback_rate, TARGET_SAMPLE_RATE)
        else:
            mic_resampled_len = int(round(dur_mic * TARGET_SAMPLE_RATE))
            loop_16k = np.zeros(mic_resampled_len, dtype=np.int16)

        if len(mic_raw) > 0:
            mic_16k = resample_to_16k(mic_raw, self._mic_rate, TARGET_SAMPLE_RATE)
        else:
            mic_16k = np.zeros(len(loop_16k), dtype=np.int16)

        # Sincroniza e iguala o comprimento de amostras dos dois canais
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

        # Canal Esquerdo (L): Zoom / Sistema
        # Canal Direito (R): Microfone local
        stereo_array = np.empty((target_len, 2), dtype=np.int16)
        stereo_array[:, 0] = loop_16k
        stereo_array[:, 1] = mic_16k

        self._block_index += 1
        block_num = self._block_index
        timestamp_str = time.strftime("%Y%m%d_%H%M%S")
        wav_path = self.temp_dir / f"block_{timestamp_str}_{block_num:03d}.wav"

        try:
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(TARGET_SAMPLE_RATE)
                wf.writeframes(stereo_array.tobytes())

            block_duration = target_len / float(TARGET_SAMPLE_RATE)
            end_time = time.time() - self._start_time
            start_time = max(0.0, end_time - block_duration)

            logger.info(
                f"Bloco {block_num} gerado com sucesso: {wav_path.name} "
                f"({block_duration:.1f}s, [{start_time:.0f}s -> {end_time:.0f}s])"
            )

            if self.on_block_ready:
                self.on_block_ready(wav_path, block_num, start_time, end_time, is_final)

            return wav_path

        except Exception as e:
            logger.exception(f"Erro ao salvar arquivo WAV do bloco {block_num}: {e}")
            return None

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

