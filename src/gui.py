"""Interface Gráfica Moderna em CustomTkinter para o Zoom Meeting Transcriber.

Gerencia seleção de dispositivos WASAPI, controles de gravação, VU meters em tempo real,
visualização em abas (Transcrição ao Vivo, Ata Executiva, Logs) e configuração da API Key.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import customtkinter as ctk

from audio_engine import AudioEngine, listar_dispositivos_audio
from gemini_service import (
    GeminiService,
    format_seconds,
    get_base_dir,
    load_api_key_from_env,
    save_api_key_to_env,
)

logger = logging.getLogger("zoom_transcriber.gui")

# Configurações globais de aparência
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


def open_folder_in_explorer(folder_path: Path) -> None:
    """Abre uma pasta no Windows Explorer ou explorador padrão do sistema operacional."""
    folder_path = folder_path.resolve()
    folder_path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(folder_path))
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", str(folder_path)], check=False)
        else:
            import subprocess
            subprocess.run(["xdg-open", str(folder_path)], check=False)
    except Exception as e:
        logger.error(f"Erro ao abrir pasta {folder_path}: {e}")


class ApiKeyModal(ctk.CTkToplevel):
    """Janela modal para visualização e atualização da GEMINI_API_KEY."""

    def __init__(self, parent: ctk.CTk, on_save_callback):
        super().__init__(parent)
        self.on_save_callback = on_save_callback

        self.title("Configurar Chave da API Google Gemini")
        self.geometry("540x260")
        self.resizable(False, False)
        self.grab_set()  # Modal

        self.grid_columnconfigure(0, weight=1)

        # Título
        self.lbl_title = ctk.CTkLabel(
            self,
            text="Configuração da API Google Gemini",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.lbl_title.pack(pady=(20, 5))

        self.lbl_desc = ctk.CTkLabel(
            self,
            text="Insira sua GEMINI_API_KEY para habilitar a transcrição e geração de atas.\nA chave será salva em seu arquivo .env local.",
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
            justify="center",
        )
        self.lbl_desc.pack(pady=(0, 15))

        # Campo de entrada
        current_key = load_api_key_from_env()
        self.entry_key = ctk.CTkEntry(
            self,
            width=460,
            show="*",
            placeholder_text="Cole sua GEMINI_API_KEY aqui...",
        )
        if current_key:
            self.entry_key.insert(0, current_key)
        self.entry_key.pack(pady=5)

        # Checkbox mostrar chave
        self.show_var = ctk.BooleanVar(value=False)
        self.chk_show = ctk.CTkCheckBox(
            self,
            text="Mostrar chave",
            variable=self.show_var,
            command=self._toggle_show,
            font=ctk.CTkFont(size=11),
        )
        self.chk_show.pack(pady=(2, 15))

        # Botões
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=40, pady=(0, 15))

        self.btn_cancel = ctk.CTkButton(
            btn_frame,
            text="Cancelar",
            fg_color="#4b5563",
            hover_color="#374151",
            width=120,
            command=self.destroy,
        )
        self.btn_cancel.pack(side="left")

        self.btn_save = ctk.CTkButton(
            btn_frame,
            text="Salvar Chave",
            fg_color="#10b981",
            hover_color="#059669",
            width=160,
            command=self._save_and_close,
        )
        self.btn_save.pack(side="right")

    def _toggle_show(self):
        if self.show_var.get():
            self.entry_key.configure(show="")
        else:
            self.entry_key.configure(show="*")

    def _save_and_close(self):
        new_key = self.entry_key.get().strip()
        if new_key:
            self.on_save_callback(new_key)
            self.destroy()
        else:
            self.entry_key.configure(border_color="#ef4444")


class ZoomTranscriberApp(ctk.CTk):
    """Aplicação Principal de Transcrição e Geração de Atas."""

    def __init__(self):
        super().__init__()

        self.title("Zoom Meeting Transcriber & Minute Generator")
        self.geometry("960x730")
        self.minsize(920, 680)

        # Estado da gravação
        self.is_recording = False
        self.recording_start_time = 0.0
        self.block_count = 0

        # Mapeamentos de hardware de áudio
        self.loopback_map: Dict[str, int] = {}
        self.mic_map: Dict[str, Optional[int]] = {}

        # Diretório temporário para blocos de áudio
        self.temp_dir = get_base_dir() / "atas_reunioes" / "temp_blocks"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # Inicializa motores e serviços
        self.gemini_service = GeminiService(
            on_log=self._handle_service_log,
            on_transcription_update=self._handle_transcription_update,
            on_minute_generated=self._handle_minute_generated,
            on_status_change=self._handle_status_change,
        )

        self.audio_engine = AudioEngine(
            temp_dir=self.temp_dir,
            on_rms_update=self._handle_rms_update,
            on_block_ready=self._handle_block_ready,
        )

        # Constrói componentes da interface gráfica
        self._build_ui()

        # Carrega lista inicial de dispositivos WASAPI
        self._refresh_audio_devices()

        # Inicia temporizador de interface
        self._timer_running = False

        # Verifica se a chave GEMINI_API_KEY está configurada
        self.after(300, self._check_initial_api_key)

    def _build_ui(self) -> None:
        """Constrói o layout responsivo da janela."""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)  # Abas ocupam o espaço vertical restante

        # ---------------- 1. CABEÇALHO SUPERIOR ----------------
        header_frame = ctk.CTkFrame(self, fg_color="#18181b", corner_radius=8)
        header_frame.grid(row=0, column=0, padx=16, pady=(14, 8), sticky="ew")
        header_frame.grid_columnconfigure(0, weight=1)

        title_container = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_container.grid(row=0, column=0, padx=16, pady=10, sticky="w")

        lbl_app_title = ctk.CTkLabel(
            title_container,
            text="🎙️ Zoom Meeting Transcriber",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        lbl_app_title.pack(anchor="w")

        lbl_app_subtitle = ctk.CTkLabel(
            title_container,
            text="Captura dual WASAPI Loopback + Microfone • Transcrição incremental • Atas Executivas com Gemini 2.5 Flash",
            font=ctk.CTkFont(size=12),
            text_color="#a1a1aa",
        )
        lbl_app_subtitle.pack(anchor="w")

        self.btn_config_key = ctk.CTkButton(
            header_frame,
            text="🔑 Configurar API Key",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#3b82f6",
            hover_color="#2563eb",
            width=160,
            command=self._open_api_key_dialog,
        )
        self.btn_config_key.grid(row=0, column=1, padx=16, pady=10, sticky="e")

        # ---------------- 2. CONFIGURAÇÕES & DISPOSITIVOS ----------------
        config_frame = ctk.CTkFrame(self, fg_color="#27272a", corner_radius=8)
        config_frame.grid(row=1, column=0, padx=16, pady=6, sticky="ew")
        config_frame.grid_columnconfigure((1, 3), weight=1)

        # Linha 0: Dispositivos de Áudio
        lbl_loop = ctk.CTkLabel(config_frame, text="Áudio Zoom (Loopback):", font=ctk.CTkFont(weight="bold"))
        lbl_loop.grid(row=0, column=0, padx=(14, 8), pady=(12, 6), sticky="w")

        loop_box_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        loop_box_frame.grid(row=0, column=1, padx=6, pady=(12, 6), sticky="ew")
        loop_box_frame.grid_columnconfigure(0, weight=1)

        self.opt_loopback = ctk.CTkOptionMenu(loop_box_frame, values=["Procurando dispositivos..."])
        self.opt_loopback.grid(row=0, column=0, sticky="ew")

        self.btn_refresh_devices = ctk.CTkButton(
            loop_box_frame,
            text="↻",
            width=36,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#4b5563",
            hover_color="#374151",
            command=self._refresh_audio_devices,
        )
        self.btn_refresh_devices.grid(row=0, column=1, padx=(6, 0))

        lbl_mic = ctk.CTkLabel(config_frame, text="Microfone Local:", font=ctk.CTkFont(weight="bold"))
        lbl_mic.grid(row=0, column=2, padx=(14, 8), pady=(12, 6), sticky="w")

        self.opt_mic = ctk.CTkOptionMenu(config_frame, values=["Procurando dispositivos..."])
        self.opt_mic.grid(row=0, column=3, padx=(6, 14), pady=(12, 6), sticky="ew")

        # Linha 1: Título da Reunião e Duração dos Blocos
        lbl_title = ctk.CTkLabel(config_frame, text="Título da Reunião:", font=ctk.CTkFont(weight="bold"))
        lbl_title.grid(row=1, column=0, padx=(14, 8), pady=8, sticky="w")

        self.entry_meeting_title = ctk.CTkEntry(
            config_frame,
            placeholder_text="Ex: Alinhamento Semanal BI / Sprint Review",
        )
        self.entry_meeting_title.grid(row=1, column=1, padx=6, pady=8, sticky="ew")

        lbl_block = ctk.CTkLabel(config_frame, text="Tamanho do Bloco:", font=ctk.CTkFont(weight="bold"))
        lbl_block.grid(row=1, column=2, padx=(14, 8), pady=8, sticky="w")

        self.opt_block_duration = ctk.CTkOptionMenu(
            config_frame,
            values=["1 minuto", "3 minutos", "5 minutos"],
        )
        self.opt_block_duration.set("3 minutos")
        self.opt_block_duration.grid(row=1, column=3, padx=(6, 14), pady=8, sticky="ew")

        # Linha 2: Checkboxes de Controle de Áudio
        toggles_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        toggles_frame.grid(row=2, column=0, columnspan=4, padx=14, pady=(4, 12), sticky="ew")

        self.chk_noise_gate = ctk.CTkCheckBox(
            toggles_frame,
            text="Ativar Noise Gate no Microfone (elimina ruído de fundo)",
            command=self._on_toggle_noise_gate,
            font=ctk.CTkFont(size=12),
        )
        self.chk_noise_gate.select()
        self.chk_noise_gate.pack(side="left", padx=(0, 20))

        self.chk_mic_mute = ctk.CTkCheckBox(
            toggles_frame,
            text="Mutar Microfone em tempo real (Modo Ausente / Silencioso)",
            command=self._on_toggle_mic_mute,
            font=ctk.CTkFont(size=12),
        )
        self.chk_mic_mute.pack(side="left", padx=10)

        # ---------------- 3. PAINEL DE CONTROLES & VU METERS ----------------
        control_frame = ctk.CTkFrame(self, fg_color="#18181b", corner_radius=8)
        control_frame.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        control_frame.grid_columnconfigure(1, weight=1)

        # Lado Esquerdo: Botões e Cronômetro
        left_ctrl = ctk.CTkFrame(control_frame, fg_color="transparent")
        left_ctrl.grid(row=0, column=0, padx=16, pady=12, sticky="w")

        self.btn_start = ctk.CTkButton(
            left_ctrl,
            text="▶ Iniciar Gravação",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#10b981",
            hover_color="#059669",
            width=160,
            height=40,
            command=self._start_recording,
        )
        self.btn_start.pack(side="left", padx=(0, 10))

        self.btn_stop = ctk.CTkButton(
            left_ctrl,
            text="⏹ Encerrar e Gerar Ata",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#ef4444",
            hover_color="#dc2626",
            width=180,
            height=40,
            state="disabled",
            command=self._stop_recording,
        )
        self.btn_stop.pack(side="left", padx=10)

        self.lbl_timer = ctk.CTkLabel(
            left_ctrl,
            text="⏱ 00:00:00",
            font=ctk.CTkFont(family="Consolas", size=20, weight="bold"),
            text_color="#38bdf8",
        )
        self.lbl_timer.pack(side="left", padx=16)

        self.lbl_status = ctk.CTkLabel(
            left_ctrl,
            text="Status: Aguardando...",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#9ca3af",
        )
        self.lbl_status.pack(side="left", padx=8)

        # Lado Direito: VU Meters em Tempo Real
        vu_frame = ctk.CTkFrame(control_frame, fg_color="transparent")
        vu_frame.grid(row=0, column=1, padx=16, pady=10, sticky="e")

        # VU Zoom (L)
        vu_zoom_row = ctk.CTkFrame(vu_frame, fg_color="transparent")
        vu_zoom_row.pack(anchor="e", pady=2)
        self.lbl_vu_zoom = ctk.CTkLabel(vu_zoom_row, text="Zoom (L):", font=ctk.CTkFont(size=11, weight="bold"), width=70)
        self.lbl_vu_zoom.pack(side="left")
        self.bar_vu_zoom = ctk.CTkProgressBar(vu_zoom_row, width=170, height=12, progress_color="#38bdf8")
        self.bar_vu_zoom.set(0.0)
        self.bar_vu_zoom.pack(side="left", padx=6)

        # VU Mic (R)
        vu_mic_row = ctk.CTkFrame(vu_frame, fg_color="transparent")
        vu_mic_row.pack(anchor="e", pady=2)
        self.lbl_vu_mic = ctk.CTkLabel(vu_mic_row, text="Mic (R):", font=ctk.CTkFont(size=11, weight="bold"), width=70)
        self.lbl_vu_mic.pack(side="left")
        self.bar_vu_mic = ctk.CTkProgressBar(vu_mic_row, width=170, height=12, progress_color="#10b981")
        self.bar_vu_mic.set(0.0)
        self.bar_vu_mic.pack(side="left", padx=6)

        # ---------------- 4. ÁREA DE VISUALIZAÇÃO EM ABAS ----------------
        self.tabview = ctk.CTkTabview(self, corner_radius=8)
        self.tabview.grid(row=3, column=0, padx=16, pady=(6, 8), sticky="nsew")

        # Aba 1: Transcrição ao Vivo
        tab_transcription = self.tabview.add("Transcrição ao Vivo")
        tab_transcription.grid_columnconfigure(0, weight=1)
        tab_transcription.grid_rowconfigure(0, weight=1)

        self.txt_transcription = ctk.CTkTextbox(
            tab_transcription,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            wrap="word",
        )
        self.txt_transcription.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.txt_transcription.insert(
            "end",
            "A transcrição incremental aparecerá aqui à medida que os blocos de áudio forem concluídos...\n"
        )

        # Aba 2: Ata Executiva
        tab_minutes = self.tabview.add("Ata Executiva")
        tab_minutes.grid_columnconfigure(0, weight=1)
        tab_minutes.grid_rowconfigure(0, weight=1)

        self.txt_minutes = ctk.CTkTextbox(
            tab_minutes,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            wrap="word",
        )
        self.txt_minutes.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.txt_minutes.insert(
            "end",
            "Ao encerrar a reunião, a Ata Executiva consolidada em Markdown será exibida nesta aba.\n"
        )

        # Aba 3: Logs do Sistema
        tab_logs = self.tabview.add("Logs do Sistema")
        tab_logs.grid_columnconfigure(0, weight=1)
        tab_logs.grid_rowconfigure(0, weight=1)

        self.txt_logs = ctk.CTkTextbox(
            tab_logs,
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="word",
        )
        self.txt_logs.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        # ---------------- 5. RODAPÉ ----------------
        footer_frame = ctk.CTkFrame(self, fg_color="transparent")
        footer_frame.grid(row=4, column=0, padx=16, pady=(4, 12), sticky="ew")
        footer_frame.grid_columnconfigure(0, weight=1)

        self.lbl_footer_info = ctk.CTkLabel(
            footer_frame,
            text=f"Base: {get_base_dir()}",
            font=ctk.CTkFont(size=11),
            text_color="#71717a",
        )
        self.lbl_footer_info.grid(row=0, column=0, sticky="w")

        self.btn_open_folder = ctk.CTkButton(
            footer_frame,
            text="📁 Abrir Pasta das Atas",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#374151",
            hover_color="#4b5563",
            command=self._open_atas_folder,
        )
        self.btn_open_folder.grid(row=0, column=1, sticky="e")

    # ---------------- MÉTODOS DE HARDWARE E DISPOSITIVOS ----------------
    def _refresh_audio_devices(self) -> None:
        """Escaneia os dispositivos WASAPI e atualiza os seletores da interface."""
        if self.is_recording:
            return

        self._log_gui("Atualizando dispositivos de áudio WASAPI...")
        loop_devices, mic_devices = listar_dispositivos_audio()

        # 1. Atualiza Saídas de Loopback
        self.loopback_map.clear()
        loop_options = []
        default_loop_name = None

        for d in loop_devices:
            # Formato legível e único incluindo o índice
            display_name = f"[{d['index']}] {d['name'][:46]}"
            self.loopback_map[display_name] = d["index"]
            loop_options.append(display_name)
            if d.get("is_default") and default_loop_name is None:
                default_loop_name = display_name

        if loop_options:
            self.opt_loopback.configure(values=loop_options)
            self.opt_loopback.set(default_loop_name or loop_options[0])
        else:
            self.opt_loopback.configure(values=["Nenhum dispositivo Loopback encontrado"])
            self.opt_loopback.set("Nenhum dispositivo Loopback encontrado")

        # 2. Atualiza Entradas de Microfone
        self.mic_map.clear()
        mic_options = ["Nenhum (Apenas Áudio do Zoom)"]
        self.mic_map["Nenhum (Apenas Áudio do Zoom)"] = None
        default_mic_name = None

        for d in mic_devices:
            display_name = f"[{d['index']}] {d['name'][:46]}"
            self.mic_map[display_name] = d["index"]
            mic_options.append(display_name)
            if d.get("is_default") and default_mic_name is None:
                default_mic_name = display_name

        self.opt_mic.configure(values=mic_options)
        self.opt_mic.set(default_mic_name or mic_options[0])

        self._log_gui(f"Dispositivos detectados: {len(loop_devices)} loopback(s), {len(mic_devices)} microfone(s).")

    # ---------------- GERENCIAMENTO DA CHAVE API ----------------
    def _check_initial_api_key(self) -> None:
        """Verifica na inicialização se há chave configurada no .env ou ambiente."""
        key = load_api_key_from_env()
        if not key:
            self._open_api_key_dialog()

    def _open_api_key_dialog(self) -> None:
        """Abre a janela modal para configurar a GEMINI_API_KEY."""
        ApiKeyModal(self, on_save_callback=self._on_api_key_saved)

    def _on_api_key_saved(self, new_key: str) -> None:
        """Callback acionado quando o usuário salva a chave API."""
        success = self.gemini_service.update_api_key(new_key)
        if success:
            self._log_gui("[OK] GEMINI_API_KEY atualizada com sucesso.")
            self.lbl_status.configure(text="Status: API configurada", text_color="#10b981")
        else:
            self._log_gui("[AVISO] Chave salva no .env, mas validação com Gemini falhou.")

    # ---------------- CONTROLE DE GRAVAÇÃO ----------------
    def _start_recording(self) -> None:
        """Inicia a gravação da reunião e o pipeline de transcrição."""
        # Validação da chave Gemini
        if not self.gemini_service.api_key:
            self._open_api_key_dialog()
            return

        # Obtenção dos índices selecionados
        selected_loop_str = self.opt_loopback.get()
        loop_index = self.loopback_map.get(selected_loop_str)

        if loop_index is None:
            self._log_gui("[ERRO] Dispositivo de áudio Loopback inválido ou não selecionado.")
            return

        selected_mic_str = self.opt_mic.get()
        mic_index = self.mic_map.get(selected_mic_str)

        # Determinação do tamanho do bloco
        block_dur_str = self.opt_block_duration.get()
        block_seconds = 180
        if "1" in block_dur_str:
            block_seconds = 60
        elif "5" in block_dur_str:
            block_seconds = 300

        # Inicia sessão no Gemini Service
        meeting_title = self.entry_meeting_title.get().strip() or "Reunião de Alinhamento"
        transc_path, _ = self.gemini_service.start_session(meeting_title)

        # Inicia captura no motor de áudio
        started = self.audio_engine.start_recording(
            loopback_index=loop_index,
            mic_index=mic_index,
            block_duration_seconds=block_seconds,
        )

        if not started:
            self._log_gui("[ERRO] Não foi possível iniciar a captura de áudio.")
            return

        self.is_recording = True
        self.recording_start_time = time.time()
        self.block_count = 0

        # Atualiza estado visual dos controles
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.opt_loopback.configure(state="disabled")
        self.opt_mic.configure(state="disabled")
        self.btn_refresh_devices.configure(state="disabled")
        self.opt_block_duration.configure(state="disabled")
        self.entry_meeting_title.configure(state="disabled")

        self.lbl_status.configure(text="Status: Gravando...", text_color="#10b981")
        self.txt_transcription.delete("1.0", "end")
        self.txt_transcription.insert("end", f"=== Início da Gravação: {meeting_title} ===\n\n")

        # Inicia temporizador
        self._timer_running = True
        self._update_timer()

        self._log_gui(f"[GRAVAÇÃO] Iniciada: Bloco={block_seconds}s, Loopback={selected_loop_str}, Mic={selected_mic_str}")

    def _stop_recording(self) -> None:
        """Interrompe a gravação e solicita a consolidação da Ata Executiva."""
        if not self.is_recording:
            return

        self.is_recording = False
        self._timer_running = False

        self.btn_stop.configure(state="disabled")
        self.lbl_status.configure(text="Status: Processando últimos blocos...", text_color="#f59e0b")

        # Para captura no motor de áudio
        self.audio_engine.stop_recording(finalize_block=True)

        # Notifica o Gemini Service para processar os blocos restantes e gerar a Ata
        self.gemini_service.finish_session()

        # Reabilita controles
        self.btn_start.configure(state="normal")
        self.opt_loopback.configure(state="normal")
        self.opt_mic.configure(state="normal")
        self.btn_refresh_devices.configure(state="normal")
        self.opt_block_duration.configure(state="normal")
        self.entry_meeting_title.configure(state="normal")

        self._log_gui("[GRAVAÇÃO] Encerrada pelo usuário. Aguardando finalização da Ata...")

    def _update_timer(self) -> None:
        """Atualiza o contador de tempo decorrido da reunião a cada segundo."""
        if self._timer_running:
            elapsed = time.time() - self.recording_start_time
            self.lbl_timer.configure(text=f"⏱ {format_seconds(elapsed)}")
            self.after(1000, self._update_timer)

    # ---------------- CALLBACKS THREAD-SAFE ----------------
    def _handle_rms_update(self, zoom_rms: float, mic_rms: float) -> None:
        """Recebe níveis RMS do motor de áudio e atualiza os medidores VU na thread principal."""
        def update():
            self.bar_vu_zoom.set(zoom_rms)
            self.bar_vu_mic.set(mic_rms)

        self.after(0, update)

    def _handle_block_ready(
        self,
        wav_path: Path,
        block_index: int,
        start_seconds: float,
        end_seconds: float,
        is_final: bool,
    ) -> None:
        """Recebe notificação de bloco WAV concluído e o envia para a fila do Gemini."""
        self.block_count = block_index
        self.gemini_service.enqueue_block(
            wav_path=wav_path,
            block_index=block_index,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            is_final=is_final,
        )

    def _handle_service_log(self, message: str) -> None:
        """Anexa logs técnicos na Aba 3 com timestamp."""
        def append():
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.txt_logs.insert("end", f"[{timestamp}] {message}\n")
            self.txt_logs.see("end")

        self.after(0, append)

    def _handle_transcription_update(
        self,
        block_index: int,
        block_text: str,
        full_transcript: str,
        is_final: bool,
    ) -> None:
        """Atualiza a Aba 1 com o novo trecho transcrito."""
        def update():
            self.txt_transcription.insert("end", block_text)
            self.txt_transcription.see("end")

        self.after(0, update)

    def _handle_minute_generated(self, minute_path: Path, minute_text: str) -> None:
        """Exibe a Ata Executiva consolidada na Aba 2 e alterna para ela."""
        def update():
            self.txt_minutes.delete("1.0", "end")
            self.txt_minutes.insert("end", minute_text)
            self.tabview.set("Ata Executiva")
            self.lbl_status.configure(text="Status: Ata Executiva Concluída!", text_color="#10b981")
            self._log_gui(f"[CONCLUÍDO] Ata Executiva disponível em: {minute_path.name}")

        self.after(0, update)

    def _handle_status_change(self, status: str) -> None:
        """Atualiza o badge de status na barra de controles."""
        def update():
            self.lbl_status.configure(text=f"Status: {status}")

        self.after(0, update)

    def _log_gui(self, message: str) -> None:
        """Registra mensagem local diretamente na caixa de logs."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.txt_logs.insert("end", f"[{timestamp}] {message}\n")
        self.txt_logs.see("end")

    # ---------------- AÇÕES DE INTERFACE ----------------
    def _on_toggle_noise_gate(self) -> None:
        enabled = bool(self.chk_noise_gate.get())
        self.audio_engine.set_noise_gate(enabled)

    def _on_toggle_mic_mute(self) -> None:
        muted = bool(self.chk_mic_mute.get())
        self.audio_engine.set_mic_muted(muted)

    def _open_atas_folder(self) -> None:
        """Abre a pasta das atas no explorador de arquivos."""
        now = datetime.now()
        target_dir = get_base_dir() / "atas_reunioes" / now.strftime("%Y") / now.strftime("%m")
        open_folder_in_explorer(target_dir)


def run_gui():
    """Função de inicialização da GUI."""
    app = ZoomTranscriberApp()
    app.mainloop()


if __name__ == "__main__":
    run_gui()
