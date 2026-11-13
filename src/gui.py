"""Interface Gráfica Moderna em CustomTkinter para o Zoom Meeting Transcriber.

Gerencia seleção de dispositivos WASAPI, controles de gravação, VU meters em tempo real,
visualização em abas (Transcrição ao Vivo, Ata Executiva, Logs) e configuração da API Key.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from tkinter import filedialog

import customtkinter as ctk

from audio_engine import AudioEngine, listar_dispositivos_audio
from gemini_service import (
    GeminiService,
    format_seconds,
    get_base_dir,
    load_api_key_from_env,
    save_api_key_to_env,
)

logger = logging.getLogger("omniscribe.gui")

# Configurações globais de aparência
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# Paleta de Cores Profissional (Regra 60-30-10 & Dark Obsidian Theme)
THEME_BG = "#0b0f19"          # Fundo base escuro e sofisticado (60%)
THEME_CARD = "#111827"        # Containers e cartões modulares (30%)
THEME_BORDER = "#1f2937"      # Bordas sutis de contraste elegante
THEME_INPUT = "#070b12"       # Entradas e editores de texto
THEME_TEXT_PRIMARY = "#f8fafc"# Texto primário
THEME_TEXT_MUTED = "#94a3b8"  # Texto secundário suave
THEME_TEXT_DIM = "#64748b"    # Metadados e rótulos discretos

COLOR_PRIMARY = "#3b82f6"     # Azul vibrante (10% acento)
COLOR_SUCCESS = "#10b981"     # Esmeralda para Iniciar Gravação
COLOR_DANGER = "#ef4444"      # Vermelho Carmesim para Parar
COLOR_INDIGO = "#6366f1"      # Índigo para Consumo & Cotas
COLOR_PURPLE = "#8b5cf6"      # Roxo para Histórico


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
        self.configure(fg_color=THEME_BG)
        self.grab_set()  # Modal

        self.grid_columnconfigure(0, weight=1)

        # Título
        self.lbl_title = ctk.CTkLabel(
            self,
            text="Configuração da API Google Gemini",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        )
        self.lbl_title.pack(pady=(20, 5))

        self.lbl_desc = ctk.CTkLabel(
            self,
            text="Insira sua GEMINI_API_KEY para habilitar a transcrição e geração de atas.\nA chave será salva em seu arquivo .env local.",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
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
            fg_color=THEME_INPUT,
            border_color=THEME_BORDER,
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



class UsageModal(ctk.CTkToplevel):
    """Modal visual para exibir métricas detalhadas de consumo, custos e cotas da API Gemini."""

    def __init__(self, parent: "ZoomTranscriberApp"):
        super().__init__(parent)
        self.parent = parent

        self.title("Consumo & Cotas da API - OmniScribe AI")
        self.geometry("580x500")
        self.resizable(False, False)
        self.configure(fg_color=THEME_BG)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)

        summary = self.parent.gemini_service.get_usage_summary()

        # Cabeçalho
        lbl_title = ctk.CTkLabel(
            self,
            text="📊 Monitor de Consumo & Custos da API",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        )
        lbl_title.pack(pady=(20, 4))

        lbl_sub = ctk.CTkLabel(
            self,
            text="Rastreamento em tempo real do Gemini 2.5 Flash para total transparência.",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        )
        lbl_sub.pack(pady=(0, 14))

        # Card de Cota Diária (Free Tier)
        quota_card = ctk.CTkFrame(self, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=10)
        quota_card.pack(fill="x", padx=24, pady=6)
        quota_card.grid_columnconfigure(0, weight=1)

        today_req = summary["today_requests"]
        free_lim = summary["free_limit"]
        percent = summary["daily_percent"]

        lbl_q_title = ctk.CTkLabel(
            quota_card,
            text="Cota Gratuita Diária (Google AI Studio)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        )
        lbl_q_title.grid(row=0, column=0, padx=14, pady=(12, 4), sticky="w")

        q_color = "#38bdf8" if percent < 80 else ("#f59e0b" if percent < 95 else "#ef4444")
        lbl_q_val = ctk.CTkLabel(
            quota_card,
            text=f"{today_req} / {free_lim} requisições hoje ({percent:.1f}%)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=q_color,
        )
        lbl_q_val.grid(row=0, column=1, padx=14, pady=(12, 4), sticky="e")

        bar_quota = ctk.CTkProgressBar(quota_card, height=12, progress_color="#10b981" if percent < 80 else "#ef4444", fg_color="#1f2937", corner_radius=6)
        bar_quota.set(percent / 100.0)
        bar_quota.grid(row=1, column=0, columnspan=2, padx=14, pady=(4, 14), sticky="ew")

        # Card de Estatísticas Gerais
        stats_card = ctk.CTkFrame(self, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=10)
        stats_card.pack(fill="x", padx=24, pady=8)
        stats_card.grid_columnconfigure((0, 1), weight=1)

        # Linhas de métricas
        self._add_metric_row(stats_card, 0, "🎙️ Total de Reuniões Concluídas:", f"{summary['total_meetings']} sessões")
        self._add_metric_row(stats_card, 1, "⏱️ Áudio Processado Acumulado:", summary["audio_time_str"])
        self._add_metric_row(stats_card, 2, "⚡ Requisições Totais Realizadas:", f"{summary['total_requests']} chamadas")
        self._add_metric_row(stats_card, 3, "🔤 Volume de Tokens (Prompt + Output):", f"{summary['total_tokens']:,} tokens".replace(",", "."))
        self._add_metric_row(stats_card, 4, "💵 Custo Estimado Acumulado (USD):", f"${summary['cost_usd']:.4f}")
        self._add_metric_row(stats_card, 5, "🇧🇷 Custo Estimado em Reais (BRL):", f"R$ {summary['cost_brl']:.2f}", highlight=True)

        # Informações de transparência
        lbl_info = ctk.CTkLabel(
            self,
            text="💡 O Gemini 2.5 Flash oferece até 1.500 requisições/dia totalmente gratuitas no AI Studio.\nMesmo em conta paga (Pay-as-you-go), 1 hora de reunião custa apenas ~R$ 0,40.",
            font=ctk.CTkFont(size=11),
            text_color="#71717a",
            justify="center",
        )
        lbl_info.pack(pady=8)

        btn_ok = ctk.CTkButton(
            self,
            text="Entendido",
            fg_color="#3b82f6",
            hover_color="#2563eb",
            width=120,
            command=self.destroy,
        )
        btn_ok.pack(pady=(4, 16))

    def _add_metric_row(self, frame: ctk.CTkFrame, row: int, label: str, value: str, highlight: bool = False):
        lbl_lbl = ctk.CTkLabel(frame, text=label, font=ctk.CTkFont(size=12, weight="bold" if highlight else "normal"))
        lbl_lbl.grid(row=row, column=0, padx=14, pady=5, sticky="w")
        color = "#10b981" if highlight else "#f3f4f6"
        lbl_val = ctk.CTkLabel(frame, text=value, font=ctk.CTkFont(size=12, weight="bold"), text_color=color)
        lbl_val.grid(row=row, column=1, padx=14, pady=5, sticky="e")


class HistoryModal(ctk.CTkToplevel):
    """Modal visual para listar, buscar e carregar atas e transcrições anteriores."""

    def __init__(self, parent: "ZoomTranscriberApp"):
        super().__init__(parent)
        self.parent = parent

        self.title("Histórico de Reuniões & Atas - OmniScribe AI")
        self.geometry("740x540")
        self.minsize(620, 420)
        self.configure(fg_color=THEME_BG)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Cabeçalho
        top_frame = ctk.CTkFrame(self, fg_color="transparent")
        top_frame.grid(row=0, column=0, padx=20, pady=(16, 8), sticky="ew")
        top_frame.grid_columnconfigure(0, weight=1)

        lbl_title = ctk.CTkLabel(
            top_frame,
            text="📜 Histórico de Reuniões e Gravações",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        )
        lbl_title.grid(row=0, column=0, sticky="w")

        lbl_sub = ctk.CTkLabel(
            top_frame,
            text="Selecione qualquer ata executiva ou transcrição salva para visualizar imediatamente no aplicativo.",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        )
        lbl_sub.grid(row=1, column=0, sticky="w")

        # Container rolável com as reuniões
        self.scroll_list = ctk.CTkScrollableFrame(self, corner_radius=10, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1)
        self.scroll_list.grid(row=1, column=0, padx=20, pady=8, sticky="nsew")
        self.scroll_list.grid_columnconfigure(0, weight=1)

        # Rodapé
        bot_frame = ctk.CTkFrame(self, fg_color="transparent")
        bot_frame.grid(row=2, column=0, padx=20, pady=(8, 16), sticky="ew")

        btn_browse = ctk.CTkButton(
            bot_frame,
            text="📂 Procurar outro arquivo (.md)...",
            font=ctk.CTkFont(size=12),
            fg_color="#374151",
            hover_color="#4b5563",
            command=self._browse_custom_file,
        )
        btn_browse.pack(side="left")

        btn_close = ctk.CTkButton(
            bot_frame,
            text="Fechar",
            font=ctk.CTkFont(size=12),
            fg_color="#4b5563",
            hover_color="#374151",
            width=100,
            command=self.destroy,
        )
        btn_close.pack(side="right")

        self._populate_list()

    def _populate_list(self) -> None:
        base_dir = get_base_dir()
        atas_root = base_dir / "atas_reunioes"

        if not atas_root.exists():
            lbl_empty = ctk.CTkLabel(
                self.scroll_list,
                text="Nenhuma ata gravada ainda.\nSuas gravações concluídas aparecerão aqui automaticamente!",
                font=ctk.CTkFont(size=13),
                text_color="#9ca3af",
                justify="center",
            )
            lbl_empty.pack(pady=40)
            return

        files = [f for f in atas_root.glob("**/*.md") if f.is_file() and not f.name.startswith(".")]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        if not files:
            lbl_empty = ctk.CTkLabel(
                self.scroll_list,
                text="Nenhum arquivo Markdown encontrado na pasta de atas.",
                font=ctk.CTkFont(size=13),
                text_color="#9ca3af",
            )
            lbl_empty.pack(pady=40)
            return

        for f in files:
            self._create_item_card(f)

    def _create_item_card(self, file_path: Path) -> None:
        filename = file_path.name
        is_ata = "ata_executiva" in filename
        is_transc = "transcricao" in filename

        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
        date_str = mtime.strftime("%d/%m/%Y às %H:%M")

        card = ctk.CTkFrame(self.scroll_list, fg_color="#182235", border_color=THEME_BORDER, border_width=1, corner_radius=8)
        card.pack(fill="x", padx=4, pady=4)
        card.grid_columnconfigure(1, weight=1)

        # Ícone e Badge
        if is_ata:
            badge_text = "ATA EXECUTIVA"
            badge_color = "#059669"
            icon = "📋"
        elif is_transc:
            badge_text = "TRANSCRIÇÃO"
            badge_color = "#2563eb"
            icon = "🎙️"
        else:
            badge_text = "DOCUMENTO"
            badge_color = "#4b5563"
            icon = "📄"

        badge = ctk.CTkLabel(
            card,
            text=f"{icon} {badge_text}",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#ffffff",
            fg_color=badge_color,
            corner_radius=4,
            width=110,
            height=24,
        )
        badge.grid(row=0, column=0, padx=10, pady=10, sticky="w")

        info_frame = ctk.CTkFrame(card, fg_color="transparent")
        info_frame.grid(row=0, column=1, padx=6, pady=8, sticky="w")

        lbl_fname = ctk.CTkLabel(
            info_frame,
            text=filename,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#f3f4f6",
        )
        lbl_fname.pack(anchor="w")

        lbl_date = ctk.CTkLabel(
            info_frame,
            text=f"Modificado em: {date_str} • Tamanho: {file_path.stat().st_size // 1024 + 1} KB",
            font=ctk.CTkFont(size=11),
            text_color="#9ca3af",
        )
        lbl_date.pack(anchor="w")

        btn_load = ctk.CTkButton(
            card,
            text="Visualizar",
            width=100,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#3b82f6",
            hover_color="#1d4ed8",
            command=lambda p=file_path: self._select_file(p),
        )
        btn_load.grid(row=0, column=2, padx=12, pady=10, sticky="e")

    def _select_file(self, file_path: Path) -> None:
        self.parent.load_document(file_path)
        self.destroy()

    def _browse_custom_file(self) -> None:
        atas_root = get_base_dir() / "atas_reunioes"
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Selecione um arquivo de Ata ou Transcrição",
            initialdir=str(atas_root) if atas_root.exists() else str(get_base_dir()),
            filetypes=[("Documentos Markdown", "*.md"), ("Todos os Arquivos", "*.*")],
        )
        if chosen:
            self._select_file(Path(chosen))


class ZoomTranscriberApp(ctk.CTk):
    """Aplicação Principal de Transcrição e Geração de Atas."""

    def __init__(self):
        super().__init__()

        self.title("OmniScribe AI - Universal Meeting Transcriber & Minute Generator")
        self.geometry("1020x760")
        self.minsize(960, 700)
        self.configure(fg_color=THEME_BG)

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

        # Inicializa motores e serviços com carregamento adiado do SDK para boot instantâneo
        self.gemini_service = GeminiService(
            on_log=self._handle_service_log,
            on_transcription_update=self._handle_transcription_update,
            on_minute_generated=self._handle_minute_generated,
            on_status_change=self._handle_status_change,
            defer_init=True,
        )

        self.audio_engine = AudioEngine(
            temp_dir=self.temp_dir,
            on_rms_update=self._handle_rms_update,
            on_block_ready=self._handle_block_ready,
        )

        # Constrói componentes da interface gráfica
        self._build_ui()

        # Pinta a janela imediatamente antes de carregar áudio/SDK pesado
        self.update()

        # Inicia temporizador de interface
        self._timer_running = False

        # Carrega dispositivos WASAPI, cliente Gemini e validação de API key após renderização
        self.after(60, self._refresh_audio_devices)
        self.after(120, self.gemini_service._init_client)
        self.after(200, self._check_initial_api_key)

    def _build_ui(self) -> None:
        """Constrói o layout responsivo e profissional da janela."""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)  # Abas ocupam todo o espaço vertical restante dinamicamente

        # ---------------- 1. CABEÇALHO SUPERIOR ----------------
        header_frame = ctk.CTkFrame(self, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=10)
        header_frame.grid(row=0, column=0, padx=16, pady=(14, 8), sticky="ew")
        header_frame.grid_columnconfigure(0, weight=1)

        title_container = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_container.grid(row=0, column=0, padx=16, pady=10, sticky="w")

        title_badge_row = ctk.CTkFrame(title_container, fg_color="transparent")
        title_badge_row.pack(anchor="w")

        lbl_app_title = ctk.CTkLabel(
            title_badge_row,
            text="🎙️ OmniScribe AI",
            font=ctk.CTkFont(size=21, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        )
        lbl_app_title.pack(side="left")

        lbl_badge = ctk.CTkLabel(
            title_badge_row,
            text="PRO v1.2",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#1e293b",
            text_color="#38bdf8",
            corner_radius=6,
            padx=8,
            pady=2,
        )
        lbl_badge.pack(side="left", padx=(10, 0))

        lbl_app_subtitle = ctk.CTkLabel(
            title_container,
            text="Captura Dual WASAPI • Diarização Inteligente Multi-Voz • Atas Executivas com Gemini 2.5 Flash",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        )
        lbl_app_subtitle.pack(anchor="w", pady=(2, 0))

        header_actions = ctk.CTkFrame(header_frame, fg_color="transparent")
        header_actions.grid(row=0, column=1, padx=16, pady=10, sticky="e")

        self.btn_header_history = ctk.CTkButton(
            header_actions,
            text="📜 Histórico",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=COLOR_PURPLE,
            hover_color="#7c3aed",
            width=110,
            command=self._open_history_modal,
        )
        self.btn_header_history.pack(side="left", padx=(0, 8))

        self.btn_usage = ctk.CTkButton(
            header_actions,
            text="📊 Consumo & Cotas",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=COLOR_INDIGO,
            hover_color="#4f46e5",
            width=140,
            command=self._open_usage_modal,
        )
        self.btn_usage.pack(side="left", padx=(0, 8))

        self.btn_config_key = ctk.CTkButton(
            header_actions,
            text="🔑 API Key",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=COLOR_PRIMARY,
            hover_color="#2563eb",
            width=110,
            command=self._open_api_key_dialog,
        )
        self.btn_config_key.pack(side="left")

        # ---------------- 2. CONFIGURAÇÕES & DISPOSITIVOS ----------------
        config_frame = ctk.CTkFrame(self, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=10)
        config_frame.grid(row=1, column=0, padx=16, pady=6, sticky="ew")
        config_frame.grid_columnconfigure((1, 3), weight=1)

        # Linha 0: Dispositivos de Áudio
        lbl_loop = ctk.CTkLabel(config_frame, text="Áudio Sistema / Reunião (L):", font=ctk.CTkFont(weight="bold"))
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
            fg_color="#374151",
            hover_color="#4b5563",
            command=self._refresh_audio_devices,
        )
        self.btn_refresh_devices.grid(row=0, column=1, padx=(6, 0))

        lbl_mic = ctk.CTkLabel(config_frame, text="Microfone Local (R):", font=ctk.CTkFont(weight="bold"))
        lbl_mic.grid(row=0, column=2, padx=(14, 8), pady=(12, 6), sticky="w")

        self.opt_mic = ctk.CTkOptionMenu(config_frame, values=["Procurando dispositivos..."])
        self.opt_mic.grid(row=0, column=3, padx=(6, 14), pady=(12, 6), sticky="ew")

        # Linha 1: Título da Reunião e Duração dos Blocos
        lbl_title = ctk.CTkLabel(config_frame, text="Título da Sessão:", font=ctk.CTkFont(weight="bold"))
        lbl_title.grid(row=1, column=0, padx=(14, 8), pady=8, sticky="w")

        self.entry_meeting_title = ctk.CTkEntry(
            config_frame,
            placeholder_text="Ex: Alinhamento Semanal BI / React Podcast Tech",
            fg_color=THEME_INPUT,
            border_color=THEME_BORDER,
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
        control_frame = ctk.CTkFrame(self, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=10)
        control_frame.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        control_frame.grid_columnconfigure(1, weight=1)

        # Lado Esquerdo: Botões e Cronômetro
        left_ctrl = ctk.CTkFrame(control_frame, fg_color="transparent")
        left_ctrl.grid(row=0, column=0, padx=16, pady=12, sticky="w")

        self.btn_start = ctk.CTkButton(
            left_ctrl,
            text="▶ Iniciar Gravação",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLOR_SUCCESS,
            hover_color="#059669",
            width=165,
            height=42,
            corner_radius=8,
            command=self._start_recording,
        )
        self.btn_start.pack(side="left", padx=(0, 10))

        self.btn_stop = ctk.CTkButton(
            left_ctrl,
            text="⏹ Encerrar e Gerar Ata",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLOR_DANGER,
            hover_color="#dc2626",
            width=185,
            height=42,
            corner_radius=8,
            state="disabled",
            command=self._stop_recording,
        )
        self.btn_stop.pack(side="left", padx=(0, 14))

        self.lbl_timer = ctk.CTkLabel(
            left_ctrl,
            text="⏱ 00:00:00",
            font=ctk.CTkFont(family="Consolas", size=22, weight="bold"),
            text_color="#38bdf8",
        )
        self.lbl_timer.pack(side="left", padx=(0, 14))

        self.lbl_status = ctk.CTkLabel(
            left_ctrl,
            text="● Pronto para Iniciar",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=COLOR_SUCCESS,
        )
        self.lbl_status.pack(side="left", padx=4)

        # Lado Direito: VU Meters em Tempo Real
        vu_frame = ctk.CTkFrame(control_frame, fg_color="transparent")
        vu_frame.grid(row=0, column=1, padx=16, pady=10, sticky="e")

        # VU Sistema (L)
        vu_zoom_row = ctk.CTkFrame(vu_frame, fg_color="transparent")
        vu_zoom_row.pack(anchor="e", pady=2)
        self.lbl_vu_zoom = ctk.CTkLabel(vu_zoom_row, text="Sistema (L):", font=ctk.CTkFont(size=11, weight="bold"), width=75, anchor="e")
        self.lbl_vu_zoom.pack(side="left")
        self.bar_vu_zoom = ctk.CTkProgressBar(vu_zoom_row, width=150, height=12, progress_color="#38bdf8", fg_color="#1f2937", corner_radius=6)
        self.bar_vu_zoom.set(0.0)
        self.bar_vu_zoom.pack(side="left", padx=6)
        self.lbl_vu_zoom_val = ctk.CTkLabel(vu_zoom_row, text="0%", font=ctk.CTkFont(size=11), text_color=THEME_TEXT_DIM, width=32)
        self.lbl_vu_zoom_val.pack(side="left")

        # VU Mic (R)
        vu_mic_row = ctk.CTkFrame(vu_frame, fg_color="transparent")
        vu_mic_row.pack(anchor="e", pady=2)
        self.lbl_vu_mic = ctk.CTkLabel(vu_mic_row, text="Mic (R):", font=ctk.CTkFont(size=11, weight="bold"), width=75, anchor="e")
        self.lbl_vu_mic.pack(side="left")
        self.bar_vu_mic = ctk.CTkProgressBar(vu_mic_row, width=150, height=12, progress_color=COLOR_SUCCESS, fg_color="#1f2937", corner_radius=6)
        self.bar_vu_mic.set(0.0)
        self.bar_vu_mic.pack(side="left", padx=6)
        self.lbl_vu_mic_val = ctk.CTkLabel(vu_mic_row, text="0%", font=ctk.CTkFont(size=11), text_color=THEME_TEXT_DIM, width=32)
        self.lbl_vu_mic_val.pack(side="left")

        # ---------------- 4. ÁREA DE VISUALIZAÇÃO EM ABAS ----------------
        self.tabview = ctk.CTkTabview(
            self,
            corner_radius=10,
            fg_color=THEME_CARD,
            segmented_button_fg_color="#070b12",
            segmented_button_selected_color=COLOR_PRIMARY,
            segmented_button_selected_hover_color="#2563eb",
            segmented_button_unselected_color="#1f2937",
            segmented_button_unselected_hover_color="#374151",
            text_color="#f8fafc",
        )
        self.tabview.grid(row=3, column=0, padx=16, pady=(6, 8), sticky="nsew")

        # Aba 1: Transcrição ao Vivo
        tab_transcription = self.tabview.add("Transcrição ao Vivo")
        tab_transcription.grid_columnconfigure(0, weight=1)
        tab_transcription.grid_rowconfigure(1, weight=1)

        tb_trans = ctk.CTkFrame(tab_transcription, fg_color="transparent")
        tb_trans.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 6))
        tb_trans.grid_columnconfigure(0, weight=1)

        lbl_t_info = ctk.CTkLabel(
            tb_trans,
            text="⚡ Diarização acústica ativa • Canal L (Sistema) & Canal R (Você) • Falas sobrepostas preservadas",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        )
        lbl_t_info.grid(row=0, column=0, sticky="w")

        self.btn_copy_trans = ctk.CTkButton(
            tb_trans,
            text="📋 Copiar Transcrição",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#1f2937",
            hover_color="#374151",
            border_color="#374151",
            border_width=1,
            width=160,
            height=28,
            command=self._copy_transcription,
        )
        self.btn_copy_trans.grid(row=0, column=1, sticky="e")

        self.txt_transcription = ctk.CTkTextbox(
            tab_transcription,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=THEME_INPUT,
            text_color=THEME_TEXT_PRIMARY,
            border_color=THEME_BORDER,
            border_width=1,
            corner_radius=8,
            wrap="word",
        )
        self.txt_transcription.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.txt_transcription.insert(
            "end",
            "A transcrição incremental com diarização de vozes aparecerá aqui à medida que os blocos forem gravados...\n"
        )

        # Aba 2: Ata Executiva
        tab_minutes = self.tabview.add("Ata Executiva")
        tab_minutes.grid_columnconfigure(0, weight=1)
        tab_minutes.grid_rowconfigure(1, weight=1)

        tb_min = ctk.CTkFrame(tab_minutes, fg_color="transparent")
        tb_min.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 6))
        tb_min.grid_columnconfigure(0, weight=1)

        lbl_m_info = ctk.CTkLabel(
            tb_min,
            text="📄 Síntese executiva formatada em Markdown com Plano de Ação e Decisões",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        )
        lbl_m_info.grid(row=0, column=0, sticky="w")

        self.btn_copy_min = ctk.CTkButton(
            tb_min,
            text="📋 Copiar Ata Markdown",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#1f2937",
            hover_color="#374151",
            border_color="#374151",
            border_width=1,
            width=170,
            height=28,
            command=self._copy_minutes,
        )
        self.btn_copy_min.grid(row=0, column=1, sticky="e")

        self.txt_minutes = ctk.CTkTextbox(
            tab_minutes,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=THEME_INPUT,
            text_color=THEME_TEXT_PRIMARY,
            border_color=THEME_BORDER,
            border_width=1,
            corner_radius=8,
            wrap="word",
        )
        self.txt_minutes.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.txt_minutes.insert(
            "end",
            "Ao encerrar a reunião, a Ata Executiva consolidada em Markdown será exibida nesta aba.\n"
        )

        # Aba 3: Logs do Sistema
        tab_logs = self.tabview.add("Logs do Sistema")
        tab_logs.grid_columnconfigure(0, weight=1)
        tab_logs.grid_rowconfigure(1, weight=1)

        tb_logs = ctk.CTkFrame(tab_logs, fg_color="transparent")
        tb_logs.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 6))
        tb_logs.grid_columnconfigure(0, weight=1)

        lbl_l_info = ctk.CTkLabel(
            tb_logs,
            text="⚙️ Registro Operacional em Tempo Real",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        )
        lbl_l_info.grid(row=0, column=0, sticky="w")

        btn_box_logs = ctk.CTkFrame(tb_logs, fg_color="transparent")
        btn_box_logs.grid(row=0, column=1, sticky="e")

        self.btn_copy_logs = ctk.CTkButton(
            btn_box_logs,
            text="📋 Copiar Logs",
            font=ctk.CTkFont(size=11),
            fg_color="#1f2937",
            hover_color="#374151",
            border_color="#374151",
            border_width=1,
            width=100,
            height=26,
            command=self._copy_logs,
        )
        self.btn_copy_logs.pack(side="left", padx=(0, 6))

        self.btn_clear_logs = ctk.CTkButton(
            btn_box_logs,
            text="🗑️ Limpar",
            font=ctk.CTkFont(size=11),
            fg_color="#1f2937",
            hover_color="#374151",
            border_color="#374151",
            border_width=1,
            width=80,
            height=26,
            command=lambda: self.txt_logs.delete("1.0", "end"),
        )
        self.btn_clear_logs.pack(side="left")

        self.txt_logs = ctk.CTkTextbox(
            tab_logs,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=THEME_INPUT,
            text_color="#38bdf8",
            border_color=THEME_BORDER,
            border_width=1,
            corner_radius=8,
            wrap="word",
        )
        self.txt_logs.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))

        # ---------------- 5. RODAPÉ ----------------
        footer_frame = ctk.CTkFrame(self, fg_color="transparent")
        footer_frame.grid(row=4, column=0, padx=16, pady=(4, 12), sticky="ew")
        footer_frame.grid_columnconfigure(0, weight=1)

        self.lbl_footer_info = ctk.CTkLabel(
            footer_frame,
            text=f"📂 Diretório Base: {get_base_dir()}",
            font=ctk.CTkFont(size=11),
            text_color=THEME_TEXT_DIM,
        )
        self.lbl_footer_info.grid(row=0, column=0, sticky="w")

        footer_btn_box = ctk.CTkFrame(footer_frame, fg_color="transparent")
        footer_btn_box.grid(row=0, column=1, sticky="e")

        self.btn_open_folder = ctk.CTkButton(
            footer_btn_box,
            text="📁 Abrir Pasta das Atas",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#1f2937",
            hover_color="#374151",
            border_color="#374151",
            border_width=1,
            command=self._open_atas_folder,
        )
        self.btn_open_folder.pack(side="left")

    def _copy_transcription(self) -> None:
        """Copia o conteúdo da aba Transcrição para a Área de Transferência com feedback visual."""
        content = self.txt_transcription.get("1.0", "end-1c").strip()
        if content:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.update()
            self.btn_copy_trans.configure(text="✅ Copiado!", fg_color=COLOR_SUCCESS)
            self.after(1800, lambda: self.btn_copy_trans.configure(text="📋 Copiar Transcrição", fg_color="#1f2937"))

    def _copy_minutes(self) -> None:
        """Copia o conteúdo da Ata Executiva para a Área de Transferência com feedback visual."""
        content = self.txt_minutes.get("1.0", "end-1c").strip()
        if content:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.update()
            self.btn_copy_min.configure(text="✅ Copiado!", fg_color=COLOR_SUCCESS)
            self.after(1800, lambda: self.btn_copy_min.configure(text="📋 Copiar Ata Markdown", fg_color="#1f2937"))

    def _copy_logs(self) -> None:
        """Copia os logs técnicos para a Área de Transferência."""
        content = self.txt_logs.get("1.0", "end-1c").strip()
        if content:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.update()
            self.btn_copy_logs.configure(text="✅ Copiado!", fg_color=COLOR_SUCCESS)
            self.after(1800, lambda: self.btn_copy_logs.configure(text="📋 Copiar Logs", fg_color="#1f2937"))

    # ---------------- MÉTODOS DE HARDWARE E DISPOSITIVOS ----------------
    def _refresh_audio_devices(self) -> None:
        """Escaneia os dispositivos WASAPI e atualiza os seletores da interface."""
        if self.is_recording:
            return

        self._log_gui("Atualizando dispositivos de áudio WASAPI...")
        loop_devices, mic_devices = listar_dispositivos_audio()

        # 1. Atualiza Saídas de Loopback
        self.loopback_map.clear()
        loop_options = ["Nenhum (Apenas Microfone / Ditado)"]
        self.loopback_map["Nenhum (Apenas Microfone / Ditado)"] = None
        default_loop_name = None

        for d in loop_devices:
            # Formato legível e único incluindo o índice
            display_name = f"[{d['index']}] {d['name'][:46]}"
            self.loopback_map[display_name] = d["index"]
            loop_options.append(display_name)
            if d.get("is_default") and default_loop_name is None:
                default_loop_name = display_name

        self.opt_loopback.configure(values=loop_options)
        # Prefere o dispositivo padrão de áudio se detectado, ou o primeiro
        self.opt_loopback.set(default_loop_name or (loop_options[1] if len(loop_options) > 1 else loop_options[0]))

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

    def _open_usage_modal(self) -> None:
        """Abre o modal de monitoramento de cotas e custos da API."""
        UsageModal(self)

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

        selected_mic_str = self.opt_mic.get()
        mic_index = self.mic_map.get(selected_mic_str)

        if loop_index is None and mic_index is None:
            self._log_gui("[ERRO] Selecione ao menos um dispositivo (Loopback ou Microfone).")
            return

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

        self.lbl_status.configure(text="● Gravando...", text_color="#ef4444")
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
        self.lbl_status.configure(text="Status: Encerrando gravação...", text_color="#f59e0b")

        # Roda o encerramento em thread assíncrona para não travar a GUI
        threading.Thread(target=self._async_stop_recording, daemon=True, name="AsyncStopRecording").start()

    def _async_stop_recording(self) -> None:
        """Processamento de encerramento em thread secundária."""
        try:
            self._log_gui("[GRAVAÇÃO] Encerrando motor de áudio e descarregando buffers...")
            self.audio_engine.stop_recording(finalize_block=True)

            self._log_gui("[GRAVAÇÃO] Finalizando fila do Gemini e gerando Ata...")
            self.gemini_service.finish_session()

            def reenable_controls():
                self.btn_start.configure(state="normal")
                self.opt_loopback.configure(state="normal")
                self.opt_mic.configure(state="normal")
                self.btn_refresh_devices.configure(state="normal")
                self.opt_block_duration.configure(state="normal")
                self.entry_meeting_title.configure(state="normal")

            self.after(0, reenable_controls)
            self._log_gui("[GRAVAÇÃO] Gravação finalizada com sucesso.")

        except Exception as e:
            logger.exception(f"Erro ao encerrar gravação: {e}")
            self._log_gui(f"[ERRO] Falha no encerramento da gravação: {e}")
            self.after(0, lambda: self.btn_start.configure(state="normal"))

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
            if hasattr(self, "lbl_vu_zoom_val"):
                self.lbl_vu_zoom_val.configure(text=f"{int(zoom_rms * 100)}%")
            if hasattr(self, "lbl_vu_mic_val"):
                self.lbl_vu_mic_val.configure(text=f"{int(mic_rms * 100)}%")

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

    def _open_history_modal(self) -> None:
        """Abre o modal visual de histórico de reuniões."""
        HistoryModal(self)

    def load_document(self, file_path: Path) -> None:
        """Carrega e exibe um arquivo de Ata ou Transcrição nas abas da interface."""
        file_path = Path(file_path).resolve()
        if not file_path.exists():
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            filename = file_path.name
            if "ata_executiva" in filename:
                self.txt_minutes.delete("1.0", "end")
                self.txt_minutes.insert("end", content)
                self.tabview.set("Ata Executiva")
                self.lbl_status.configure(text=f"Ata carregada: {filename[:28]}...", text_color="#10b981")

                # Tenta localizar a transcrição correspondente na mesma pasta
                matching_name = filename.replace("ata_executiva_", "transcricao_")
                matching_file = file_path.parent / matching_name
                if matching_file.exists():
                    try:
                        with open(matching_file, "r", encoding="utf-8") as mf:
                            self.txt_transcription.delete("1.0", "end")
                            self.txt_transcription.insert("end", mf.read())
                    except Exception:
                        pass

            elif "transcricao" in filename:
                self.txt_transcription.delete("1.0", "end")
                self.txt_transcription.insert("end", content)
                self.tabview.set("Transcrição ao Vivo")
                self.lbl_status.configure(text=f"Transcrição carregada: {filename[:28]}...", text_color="#38bdf8")

                # Tenta localizar a ata correspondente na mesma pasta
                matching_name = filename.replace("transcricao_", "ata_executiva_")
                matching_file = file_path.parent / matching_name
                if matching_file.exists():
                    try:
                        with open(matching_file, "r", encoding="utf-8") as mf:
                            self.txt_minutes.delete("1.0", "end")
                            self.txt_minutes.insert("end", mf.read())
                    except Exception:
                        pass
            else:
                self.txt_transcription.delete("1.0", "end")
                self.txt_transcription.insert("end", content)
                self.tabview.set("Transcrição ao Vivo")

            self._log_gui(f"[HISTÓRICO] Reunião carregada na interface: {filename}")

        except Exception as e:
            logger.exception(f"Erro ao carregar documento: {e}")
            self._log_gui(f"[ERRO] Falha ao ler arquivo: {e}")


def run_gui():
    """Função de inicialização da GUI."""
    app = ZoomTranscriberApp()
    app.mainloop()


if __name__ == "__main__":
    run_gui()

