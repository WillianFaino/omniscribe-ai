"""Interface Gráfica Moderna em CustomTkinter para o Zoom Meeting Transcriber.

Gerencia seleção de dispositivos WASAPI, controles de gravação, VU meters em tempo real,
visualização em abas (Transcrição ao Vivo, Ata Executiva, Logs) e configuração da API Key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from tkinter import filedialog

import customtkinter as ctk

from audio_engine import (
    AudioEngine,
    get_wav_info,
    listar_dispositivos_audio,
    record_mic_sample,
    slice_wav_for_retranscription,
)
from archive_service import MeetingArchiveService
from mascot_widget import VozitoWidget, OmniMicWidget
from offline_asr_service import WHISPER_MODELS, list_ollama_models
from gemini_service import (
    GeminiService,
    format_seconds,
    get_base_dir,
    load_api_key_from_env,
    save_api_key_to_env,
)

logger = logging.getLogger("escutai.gui")
_ARCHIVE_READER = MeetingArchiveService()

# Configurações globais de aparência (Escuro como padrão; alternável para Claro em tempo real)
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# Paleta Semântica Dupla (Light, Dark) compatível nativamente com CustomTkinter
THEME_BG = ("#f1f5f9", "#0b0f19")            # Fundo base (60%)
THEME_CARD = ("#ffffff", "#111827")          # Cartões principais (30%)
THEME_CARD_ALT = ("#f8fafc", "#182235")      # Cartões secundários / itens
THEME_BORDER = ("#cbd5e1", "#1f2937")        # Bordas sutis
THEME_INPUT = ("#f8fafc", "#070b12")         # Campos de entrada e texto
THEME_TEXT_PRIMARY = ("#0f172a", "#f8fafc")  # Texto principal (WCAG AAA)
THEME_TEXT_MUTED = ("#475569", "#cbd5e1")    # Texto secundário (>= 4.5:1 Dark/Light)
THEME_TEXT_DIM = ("#64748b", "#94a3b8")      # Metadados discretos (>= 4.5:1 Dark/Light)

BTN_NEUTRAL = ("#e2e8f0", "#1f2937")
BTN_NEUTRAL_HOVER = ("#cbd5e1", "#374151")

COLOR_PRIMARY = ("#2563eb", "#3b82f6")       # Acento principal (10%)
COLOR_PRIMARY_HOVER = ("#1d4ed8", "#2563eb")
COLOR_PRIMARY_SOFT = ("#dbeafe", "#1e293b")
COLOR_SUCCESS = ("#059669", "#10b981")
COLOR_DANGER = ("#dc2626", "#ef4444")
COLOR_AMBER = ("#d97706", "#f59e0b")
COLOR_TEAL = ("#0e7490", "#0891b2")
COLOR_INDIGO = ("#4f46e5", "#6366f1")
COLOR_PURPLE = ("#7c3aed", "#8b5cf6")


def _style_option_menu(menu: ctk.CTkOptionMenu) -> ctk.CTkOptionMenu:
    """Padroniza a aparência de CTkOptionMenu com os tokens do tema Claro/Escuro."""
    menu.configure(
        fg_color=BTN_NEUTRAL,
        button_color=COLOR_PRIMARY,
        button_hover_color=COLOR_PRIMARY_HOVER,
        text_color=THEME_TEXT_PRIMARY,
        dropdown_fg_color=THEME_CARD,
        dropdown_hover_color=BTN_NEUTRAL_HOVER,
        dropdown_text_color=THEME_TEXT_PRIMARY,
    )
    return menu


def _open_path_in_os(target_path: Path) -> None:
    """Abre um arquivo ou pasta no aplicativo/explorador padrão do sistema operacional."""
    resolved = Path(target_path).resolve()
    try:
        if sys.platform == "win32":
            os.startfile(str(resolved))
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", str(resolved)], check=False)
        else:
            import subprocess
            subprocess.run(["xdg-open", str(resolved)], check=False)
    except Exception as e:
        logger.error(f"Erro ao abrir caminho {resolved}: {e}")


def open_folder_in_explorer(folder_path: Path) -> None:
    """Abre uma pasta no explorador padrão do sistema operacional."""
    folder = Path(folder_path).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    _open_path_in_os(folder)


def open_file_in_os(file_path: Path) -> None:
    """Abre um arquivo (ex.: .wav) no aplicativo padrão do sistema operacional."""
    fpath = Path(file_path).resolve()
    if fpath.exists():
        _open_path_in_os(fpath)


def infer_title_from_audio_name(wav_path: Path) -> str:
    """Extrai um título legível a partir do nome `audio_YYYYMMDD_HHMM_Titulo.wav`."""
    stem = wav_path.stem
    cleaned = re.sub(r"^audio_\d{8}_\d{4}_?", "", stem)
    cleaned = re.sub(r"_+", " ", cleaned).strip()
    return cleaned or stem


class RetranscribeModal(ctk.CTkToplevel):
    """Modal para configurar e iniciar a re-transcrição de um áudio WAV salvo."""

    def __init__(self, parent: "ZoomTranscriberApp", wav_path: Path):
        super().__init__(parent)
        self.parent = parent
        self.wav_path = Path(wav_path).resolve()
        self.wav_info = get_wav_info(self.wav_path)

        self.title("Re-transcrever Áudio Salvo - Escutaí")
        self.geometry("580x475")
        self.minsize(560, 460)
        self.resizable(True, False)
        self.configure(fg_color=THEME_BG)
        self.grab_set()

        ch_label = "Mono (Presencial)" if self.wav_info["channels"] == 1 else "Estéreo (Online L/R)"
        dur_label = format_seconds(self.wav_info["duration_seconds"])
        sr_hz = self.wav_info["sample_rate"]

        lbl_head = ctk.CTkLabel(
            self,
            text="🔄 Re-transcrever Reunião Gravada",
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        )
        lbl_head.pack(anchor="w", padx=24, pady=(18, 4))

        lbl_meta = ctk.CTkLabel(
            self,
            text=f"Arquivo: {self.wav_path.name}  •  Duração: {dur_label}  •  {ch_label} ({sr_hz} Hz)",
            font=ctk.CTkFont(size=12),
            text_color="#38bdf8",
        )
        lbl_meta.pack(anchor="w", padx=24, pady=(0, 14))

        form = ctk.CTkFrame(self, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=10)
        form.pack(fill="x", padx=24, pady=4)
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(form, text="Título da Nova Ata:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=14, pady=(14, 8), sticky="w"
        )
        self.entry_title = ctk.CTkEntry(form, fg_color=THEME_INPUT, border_color=THEME_BORDER)
        self.entry_title.insert(0, infer_title_from_audio_name(self.wav_path))
        self.entry_title.grid(row=0, column=1, padx=14, pady=(14, 8), sticky="ew")

        ctk.CTkLabel(form, text="Glossário / Nomes:", font=ctk.CTkFont(weight="bold")).grid(
            row=1, column=0, padx=14, pady=8, sticky="w"
        )
        self.entry_context = ctk.CTkEntry(
            form,
            placeholder_text="Ex: Participantes: Carlos, Juliana; Termos: BI, Deploy, WASAPI",
            fg_color=THEME_INPUT,
            border_color=THEME_BORDER,
        )
        self.entry_context.grid(row=1, column=1, padx=14, pady=8, sticky="ew")

        ctk.CTkLabel(form, text="Tamanho do Bloco:", font=ctk.CTkFont(weight="bold")).grid(
            row=2, column=0, padx=14, pady=8, sticky="w"
        )
        self.opt_block = _style_option_menu(ctk.CTkOptionMenu(form, values=["1 minuto", "3 minutos", "5 minutos"]))
        self.opt_block.set("1 minuto")
        self.opt_block.grid(row=2, column=1, padx=14, pady=8, sticky="w")

        ctk.CTkLabel(form, text="Motor de IA:", font=ctk.CTkFont(weight="bold")).grid(
            row=3, column=0, padx=14, pady=8, sticky="w"
        )
        self.opt_engine = _style_option_menu(
            ctk.CTkOptionMenu(
                form,
                values=["☁️ Online (Gemini 2.5)", "💻 Offline (Whisper ONNX)"],
            )
        )
        default_eng = "💻 Offline (Whisper ONNX)" if getattr(parent, "engine_mode", "online") == "offline" else "☁️ Online (Gemini 2.5)"
        self.opt_engine.set(default_eng)
        self.opt_engine.grid(row=3, column=1, padx=14, pady=8, sticky="w")

        self.chk_enhance = ctk.CTkCheckBox(
            form,
            text="Aplicar filtro passa-alta (~80 Hz) e normalização dinâmica (recomendado para WAV externo)",
            font=ctk.CTkFont(size=12),
        )
        if sr_hz != 16000:
            self.chk_enhance.select()
        self.chk_enhance.grid(row=4, column=0, columnspan=2, padx=14, pady=(6, 14), sticky="w")

        lbl_note = ctk.CTkLabel(
            self,
            text="Serão gerados novos arquivos transcricao_*.md e ata_executiva_*.md sem sobrescrever os anteriores.",
            font=ctk.CTkFont(size=11),
            text_color=THEME_TEXT_DIM,
        )
        lbl_note.pack(anchor="w", padx=24, pady=(8, 10))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=24, pady=(4, 16))

        ctk.CTkButton(
            btn_row,
            text="Cancelar",
            fg_color="#4b5563",
            hover_color="#374151",
            width=110,
            command=self.destroy,
        ).pack(side="left")

        ctk.CTkButton(
            btn_row,
            text="🔄 Iniciar Re-transcrição e Nova Ata",
            fg_color=COLOR_SUCCESS,
            hover_color="#059669",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._confirm,
        ).pack(side="right")

    def _confirm(self) -> None:
        title = self.entry_title.get().strip() or infer_title_from_audio_name(self.wav_path)
        context_notes = self.entry_context.get().strip()
        block_str = self.opt_block.get()
        block_seconds = 60 if "1" in block_str else (300 if "5" in block_str else 180)
        eng_mode = "offline" if "Offline" in self.opt_engine.get() else "online"
        apply_enh = bool(self.chk_enhance.get())
        self.destroy()
        self.parent.start_retranscription(
            wav_path=self.wav_path,
            title=title,
            context_notes=context_notes,
            block_seconds=block_seconds,
            apply_enhancement=apply_enh,
            engine_mode=eng_mode,
        )


def parse_time_to_seconds(raw_value: str) -> float:
    """Converte string `SS`, `MM:SS` ou `HH:MM:SS` em segundos."""
    txt = raw_value.strip()
    if not txt:
        return 0.0
    if ":" not in txt:
        return max(0.0, float(txt.replace(",", ".")))
    parts = [float(p.replace(",", ".")) for p in txt.split(":")]
    if len(parts) == 2:
        return max(0.0, parts[0] * 60.0 + parts[1])
    if len(parts) == 3:
        return max(0.0, parts[0] * 3600.0 + parts[1] * 60.0 + parts[2])
    raise ValueError("Formato de tempo inválido (use SS ou MM:SS).")


class VoiceProfilesModal(ctk.CTkToplevel):
    """Modal para gerenciar modelos ONNX locais e cadastrar voiceprints (Microfone ou WAV salvo)."""

    CALIBRATION_PHRASES = [
        "Olá, aqui é {name} cadastrando minha voz no Escutaí para reuniões.",
        "Hoje vamos alinhar os indicadores de BI, entregas do sprint e próximos passos.",
        "Mesmo quando várias pessoas falam na sala, o sistema diferencia cada timbre vocal.",
    ]

    def __init__(self, parent: "ZoomTranscriberApp"):
        super().__init__(parent)
        self.parent = parent
        self.vp = parent.gemini_service.voiceprint_service
        self.captured_samples: List = []
        self.is_busy = False
        self.selected_wav_path: Optional[Path] = None

        self.title("Perfis Biométricos de Voz (Voiceprints) - Escutaí")
        self.geometry("760x660")
        self.minsize(680, 580)
        self.configure(fg_color=THEME_BG)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # 1. Cabeçalho
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, padx=20, pady=(14, 6), sticky="ew")
        ctk.CTkLabel(
            head,
            text="🗣️ Perfis Biométricos de Voz (Voiceprints Locais)",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        ).pack(anchor="w")
        ctk.CTkLabel(
            head,
            text="Identifica você e colegas automaticamente por timbre vocal sem depender de apresentações na reunião.",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        ).pack(anchor="w")

        # 2. Banner de Status dos Modelos ONNX
        model_card = ctk.CTkFrame(self, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=10)
        model_card.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
        model_card.grid_columnconfigure(0, weight=1)

        self.lbl_model_status = ctk.CTkLabel(
            model_card,
            text="Verificando motor biométrico...",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
        )
        self.lbl_model_status.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="w")

        self.btn_download_models = ctk.CTkButton(
            model_card,
            text="⬇ Baixar Modelos (~26 MB)",
            width=195,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#0891b2",
            hover_color="#0e7490",
            command=self._start_model_download,
        )
        self.btn_download_models.grid(row=0, column=1, padx=14, pady=(10, 4), sticky="e")

        self.prog_model = ctk.CTkProgressBar(model_card, height=8)
        self.prog_model.set(0.0)
        self.prog_model.grid(row=1, column=0, columnspan=2, padx=14, pady=(2, 10), sticky="ew")

        # 3. Abas de Cadastro (Microfone estilo Ok Google vs Trecho de WAV)
        self.enroll_tabs = ctk.CTkTabview(
            self,
            fg_color=THEME_CARD,
            segmented_button_fg_color=THEME_INPUT,
            segmented_button_selected_color=COLOR_PRIMARY,
            segmented_button_unselected_color=BTN_NEUTRAL,
            text_color=THEME_TEXT_PRIMARY,
            height=245,
        )
        self.enroll_tabs.grid(row=2, column=0, padx=20, pady=6, sticky="ew")

        tab_mic = self.enroll_tabs.add("🎙️ Cadastrar pelo Microfone")
        tab_mic.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(tab_mic, text="Nome do Locutor:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=(10, 6), sticky="w"
        )
        self.entry_mic_name = ctk.CTkEntry(tab_mic, fg_color=THEME_INPUT, border_color=THEME_BORDER)
        default_name = "Willian" if not self.vp.has_profiles() else ""
        if default_name:
            self.entry_mic_name.insert(0, default_name)
        else:
            self.entry_mic_name.configure(placeholder_text="Ex: Carlos, Ana, Marcos...")
        self.entry_mic_name.grid(row=0, column=1, columnspan=2, padx=12, pady=(10, 6), sticky="ew")

        self.lbl_phrase_box = ctk.CTkLabel(
            tab_mic,
            text="",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=THEME_CARD_ALT,
            text_color=THEME_TEXT_PRIMARY,
            corner_radius=8,
            wraplength=640,
            justify="center",
            height=58,
        )
        self.lbl_phrase_box.grid(row=1, column=0, columnspan=3, padx=12, pady=6, sticky="ew")

        mic_btn_row = ctk.CTkFrame(tab_mic, fg_color="transparent")
        mic_btn_row.grid(row=2, column=0, columnspan=3, padx=12, pady=(6, 4), sticky="ew")

        self.btn_rec_phrase = ctk.CTkButton(
            mic_btn_row,
            text="🎙️ Gravar Frase 1/3 (5s)",
            fg_color=COLOR_PRIMARY,
            hover_color="#2563eb",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._record_next_phrase,
        )
        self.btn_rec_phrase.pack(side="left", padx=(0, 8))

        self.btn_clear_phrases = ctk.CTkButton(
            mic_btn_row,
            text="Limpar",
            width=75,
            fg_color="#4b5563",
            hover_color="#374151",
            command=self._clear_captured_phrases,
        )
        self.btn_clear_phrases.pack(side="left", padx=(0, 12))

        self.lbl_mic_progress = ctk.CTkLabel(
            mic_btn_row,
            text="0/3 amostras capturadas",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        )
        self.lbl_mic_progress.pack(side="left")

        self.btn_save_mic_profile = ctk.CTkButton(
            mic_btn_row,
            text="💾 Salvar Voiceprint",
            fg_color=COLOR_SUCCESS,
            hover_color="#059669",
            font=ctk.CTkFont(size=12, weight="bold"),
            state="disabled",
            command=self._save_mic_profile,
        )
        self.btn_save_mic_profile.pack(side="right")

        # Tab 2: Importar de trecho WAV salvo
        tab_wav = self.enroll_tabs.add("📁 Cadastrar de Gravação (.wav)")
        tab_wav.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(tab_wav, text="Arquivo WAV:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=(10, 6), sticky="w"
        )
        wav_pick_row = ctk.CTkFrame(tab_wav, fg_color="transparent")
        wav_pick_row.grid(row=0, column=1, columnspan=3, padx=12, pady=(10, 6), sticky="ew")
        wav_pick_row.grid_columnconfigure(0, weight=1)

        self.lbl_wav_file = ctk.CTkLabel(
            wav_pick_row,
            text="Nenhum arquivo selecionado",
            fg_color=THEME_INPUT,
            corner_radius=6,
            anchor="w",
            padx=10,
            height=28,
        )
        self.lbl_wav_file.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            wav_pick_row,
            text="📂 Selecionar .wav",
            width=130,
            fg_color="#374151",
            hover_color="#4b5563",
            command=self._pick_wav_file,
        ).grid(row=0, column=1)

        ctk.CTkLabel(tab_wav, text="Nome do Locutor:", font=ctk.CTkFont(weight="bold")).grid(
            row=1, column=0, padx=12, pady=6, sticky="w"
        )
        self.entry_wav_name = ctk.CTkEntry(
            tab_wav,
            placeholder_text="Ex: Carlos (Colega BI)",
            fg_color=THEME_INPUT,
            border_color=THEME_BORDER,
        )
        self.entry_wav_name.grid(row=1, column=1, padx=12, pady=6, sticky="ew")

        ctk.CTkLabel(tab_wav, text="Canal:", font=ctk.CTkFont(weight="bold")).grid(
            row=1, column=2, padx=(8, 4), pady=6, sticky="w"
        )
        self.opt_wav_channel = ctk.CTkOptionMenu(
            tab_wav,
            values=["Auto (Maior Energia)", "Canal L (Remoto)", "Canal R (Mic Local)"],
            width=175,
        )
        self.opt_wav_channel.grid(row=1, column=3, padx=12, pady=6, sticky="e")

        ctk.CTkLabel(tab_wav, text="Trecho (MM:SS):", font=ctk.CTkFont(weight="bold")).grid(
            row=2, column=0, padx=12, pady=6, sticky="w"
        )
        time_row = ctk.CTkFrame(tab_wav, fg_color="transparent")
        time_row.grid(row=2, column=1, columnspan=3, padx=12, pady=6, sticky="ew")
        self.entry_wav_start = ctk.CTkEntry(time_row, width=90, fg_color=THEME_INPUT, border_color=THEME_BORDER)
        self.entry_wav_start.insert(0, "00:00")
        self.entry_wav_start.pack(side="left")
        ctk.CTkLabel(time_row, text=" até ").pack(side="left", padx=4)
        self.entry_wav_end = ctk.CTkEntry(time_row, width=90, fg_color=THEME_INPUT, border_color=THEME_BORDER)
        self.entry_wav_end.insert(0, "00:20")
        self.entry_wav_end.pack(side="left", padx=(0, 12))

        self.btn_enroll_wav = ctk.CTkButton(
            time_row,
            text="✨ Extrair e Salvar Voiceprint",
            fg_color=COLOR_SUCCESS,
            hover_color="#059669",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._save_wav_profile,
        )
        self.btn_enroll_wav.pack(side="right")

        # 4. Lista de perfis salvos + Exportar/Importar
        list_card = ctk.CTkFrame(self, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=10)
        list_card.grid(row=3, column=0, padx=20, pady=6, sticky="nsew")
        list_card.grid_columnconfigure(0, weight=1)
        list_card.grid_rowconfigure(1, weight=1)

        list_head = ctk.CTkFrame(list_card, fg_color="transparent")
        list_head.grid(row=0, column=0, padx=14, pady=(8, 4), sticky="ew")
        list_head.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            list_head,
            text="Perfis Cadastrados (voice_profiles.json)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        ).grid(row=0, column=0, sticky="w")

        share_box = ctk.CTkFrame(list_head, fg_color="transparent")
        share_box.grid(row=0, column=1, sticky="e")

        self.opt_import_conflict = ctk.CTkOptionMenu(
            share_box,
            values=["Se existir: Mesclar", "Se existir: Manter Local", "Se existir: Substituir"],
            width=165,
            height=26,
            font=ctk.CTkFont(size=11),
        )
        self.opt_import_conflict.set("Se existir: Mesclar")
        self.opt_import_conflict.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            share_box,
            text="📥 Importar (.json)",
            width=120,
            height=26,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._import_profiles,
        ).pack(side="left", padx=(0, 6))

        self.btn_export_all = ctk.CTkButton(
            share_box,
            text="📤 Exportar Tudo",
            width=116,
            height=26,
            fg_color="#0891b2",
            hover_color="#0e7490",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self._export_profiles(None),
        )
        self.btn_export_all.pack(side="left")

        self.scroll_profiles = ctk.CTkScrollableFrame(list_card, fg_color=THEME_INPUT, corner_radius=8)
        self.scroll_profiles.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="nsew")
        self.scroll_profiles.grid_columnconfigure(0, weight=1)

        # Rodapé
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=4, column=0, padx=20, pady=(4, 14), sticky="ew")
        self.lbl_feedback = ctk.CTkLabel(foot, text="", font=ctk.CTkFont(size=12), text_color="#38bdf8")
        self.lbl_feedback.pack(side="left")
        ctk.CTkButton(foot, text="Fechar", width=100, fg_color="#4b5563", hover_color="#374151", command=self.destroy).pack(side="right")

        self._refresh_model_status()
        self._update_phrase_preview()
        self._refresh_profiles_list()

    def _refresh_model_status(self) -> None:
        if not self.vp.is_runtime_available():
            self.lbl_model_status.configure(
                text="❌ Biblioteca 'sherpa-onnx' não instalada (execute: pip install sherpa-onnx)",
                text_color="#ef4444",
            )
            self.btn_download_models.configure(state="disabled")
            self.prog_model.set(0.0)
        elif self.vp.are_models_ready():
            self.lbl_model_status.configure(
                text="✅ Modelos biométricos prontos offline (WeSpeaker ResNet34-LM + Silero VAD)",
                text_color="#10b981",
            )
            self.btn_download_models.configure(text="✅ Modelos Prontos", state="disabled", fg_color="#059669")
            self.prog_model.set(1.0)
        else:
            self.lbl_model_status.configure(
                text="⚠️ Modelos ONNX locais ainda não baixados (~26 MB únicos para uso offline)",
                text_color="#f59e0b",
            )
            self.btn_download_models.configure(text="⬇ Baixar Modelos (~26 MB)", state="normal")
            self.prog_model.set(0.0)

    def _update_phrase_preview(self) -> None:
        step = min(len(self.captured_samples), len(self.CALIBRATION_PHRASES) - 1)
        name_preview = self.entry_mic_name.get().strip() or "seu nome"
        phrase = self.CALIBRATION_PHRASES[step].format(name=name_preview)
        self.lbl_phrase_box.configure(text=f"Leia em voz natural ({step + 1}/3):\n\"{phrase}\"")

        count = len(self.captured_samples)
        self.lbl_mic_progress.configure(text=f"{count}/3 amostras capturadas")
        if count >= 3:
            self.btn_rec_phrase.configure(text="🎙️ Gravar Extra (+5s)")
        else:
            self.btn_rec_phrase.configure(text=f"🎙️ Gravar Frase {count + 1}/3 (5s)")
        self.btn_save_mic_profile.configure(state="normal" if count >= 1 and not self.is_busy else "disabled")

    def _refresh_profiles_list(self) -> None:
        for w in self.scroll_profiles.winfo_children():
            w.destroy()
        profiles = self.vp.list_profiles()
        if not profiles:
            ctk.CTkLabel(
                self.scroll_profiles,
                text="Nenhum perfil de voz cadastrado ainda.",
                font=ctk.CTkFont(size=12),
                text_color="#9ca3af",
            ).pack(pady=18)
            return

        for prof in profiles:
            row = ctk.CTkFrame(self.scroll_profiles, fg_color=THEME_CARD_ALT, border_color=THEME_BORDER, border_width=1, corner_radius=6)
            row.pack(fill="x", padx=4, pady=3)
            row.grid_columnconfigure(0, weight=1)
            meta = (
                f"👤 {prof['name']}   •   {prof['samples_count']} amostra(s)   •   "
                f"{prof['total_speech_seconds']:.1f}s de fala útil"
            )
            ctk.CTkLabel(row, text=meta, font=ctk.CTkFont(size=12, weight="bold"), text_color=THEME_TEXT_PRIMARY, anchor="w").grid(
                row=0, column=0, padx=10, pady=8, sticky="w"
            )
            row_btns = ctk.CTkFrame(row, fg_color="transparent")
            row_btns.grid(row=0, column=1, padx=8, pady=6, sticky="e")

            ctk.CTkButton(
                row_btns,
                text="📤 Exportar",
                width=82,
                height=26,
                fg_color="#0891b2",
                hover_color="#0e7490",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda n=prof["name"]: self._export_profiles([n]),
            ).pack(side="left", padx=(0, 6))

            ctk.CTkButton(
                row_btns,
                text="🗑️ Remover",
                width=86,
                height=26,
                fg_color="#991b1b",
                hover_color="#dc2626",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda n=prof["name"]: self._delete_profile(n),
            ).pack(side="left")

    def _delete_profile(self, name: str) -> None:
        if self.vp.delete_profile(name):
            self.lbl_feedback.configure(text=f"Perfil '{name}' removido.", text_color="#f59e0b")
            self._refresh_profiles_list()

    def _export_profiles(self, profile_names: Optional[List[str]]) -> None:
        if self.is_busy:
            return
        if not self.vp.has_profiles():
            self.lbl_feedback.configure(text="Nenhum perfil cadastrado para exportar.", text_color="#f59e0b")
            return

        if profile_names and len(profile_names) == 1:
            safe = re.sub(r"[^a-zA-Z0-9_\-]+", "_", profile_names[0]).strip("_") or "perfil"
            default_name = f"voiceprint_{safe}.json"
        else:
            default_name = "voiceprints_equipe.json"

        chosen = filedialog.asksaveasfilename(
            parent=self,
            title="Exportar Perfis de Voz (.json)",
            initialdir=str(get_base_dir() / "atas_reunioes"),
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("Perfis Voiceprint JSON", "*.json"), ("Todos os Arquivos", "*.*")],
        )
        if not chosen:
            return

        try:
            count = self.vp.export_profiles(Path(chosen), profile_names=profile_names)
            self.lbl_feedback.configure(
                text=f"✅ {count} perfil(is) exportado(s) para {Path(chosen).name}!",
                text_color="#10b981",
            )
        except Exception as e:
            self.lbl_feedback.configure(text=f"Erro ao exportar perfis: {e}", text_color="#ef4444")

    def _import_profiles(self) -> None:
        if self.is_busy:
            return
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Importar Perfis de Voz (.json)",
            initialdir=str(get_base_dir() / "atas_reunioes"),
            filetypes=[("Perfis Voiceprint JSON", "*.json"), ("Todos os Arquivos", "*.*")],
        )
        if not chosen:
            return

        choice = self.opt_import_conflict.get()
        if "Manter" in choice:
            strategy = "skip"
        elif "Substituir" in choice:
            strategy = "overwrite"
        else:
            strategy = "merge"

        try:
            res = self.vp.import_profiles(Path(chosen), conflict_strategy=strategy)
            self._refresh_profiles_list()
            self.lbl_feedback.configure(
                text=(
                    f"✅ Importação concluída: +{res['added']} novo(s), "
                    f"{res['updated']} atualizado(s), {res['skipped']} mantido(s)."
                ),
                text_color="#10b981",
            )
        except Exception as e:
            self.lbl_feedback.configure(text=f"Erro ao importar perfis: {e}", text_color="#ef4444")

    def _start_model_download(self) -> None:
        if self.is_busy:
            return
        self.is_busy = True
        self.btn_download_models.configure(state="disabled")

        def worker():
            def on_prog(msg: str, frac: float):
                self.after(0, lambda: (self.lbl_feedback.configure(text=msg, text_color="#38bdf8"), self.prog_model.set(frac)))

            ok, msg = self.vp.ensure_models_downloaded(on_progress=on_prog)

            def done():
                self.is_busy = False
                self.lbl_feedback.configure(text=msg, text_color="#10b981" if ok else "#ef4444")
                self._refresh_model_status()

            self.after(0, done)

        threading.Thread(target=worker, daemon=True, name="VoiceprintDownload").start()

    def _record_next_phrase(self) -> None:
        if self.is_busy:
            return
        selected_mic_str = self.parent.opt_mic.get()
        mic_index = self.parent.mic_map.get(selected_mic_str)
        if mic_index is None:
            self.lbl_feedback.configure(text="Selecione um Microfone na janela principal primeiro.", text_color="#ef4444")
            return

        self.is_busy = True
        self.btn_rec_phrase.configure(state="disabled", text="🔴 Gravando (5s)...")
        self.btn_save_mic_profile.configure(state="disabled")
        self.lbl_feedback.configure(text="Fale naturalmente por 5 segundos...", text_color="#ef4444")
        self._update_phrase_preview_label_only()

        def worker():
            try:
                samples = record_mic_sample(mic_index=mic_index, duration_seconds=5.0)
                if len(samples) > 0:
                    self.captured_samples.append(samples)
                    ok_msg = f"Amostra {len(self.captured_samples)} capturada com sucesso!"
                    color = "#10b981"
                else:
                    ok_msg = "Nenhum áudio capturado pelo microfone."
                    color = "#f59e0b"
            except Exception as e:
                ok_msg = f"Erro ao gravar microfone: {e}"
                color = "#ef4444"

            def done():
                self.is_busy = False
                self.btn_rec_phrase.configure(state="normal")
                self.lbl_feedback.configure(text=ok_msg, text_color=color)
                self._update_phrase_preview()

            self.after(0, done)

        threading.Thread(target=worker, daemon=True, name="VoiceprintMicRecord").start()

    def _update_phrase_preview_label_only(self) -> None:
        step = min(len(self.captured_samples), len(self.CALIBRATION_PHRASES) - 1)
        name_preview = self.entry_mic_name.get().strip() or "seu nome"
        phrase = self.CALIBRATION_PHRASES[step].format(name=name_preview)
        self.lbl_phrase_box.configure(text=f"Gravando frase {step + 1}/3:\n\"{phrase}\"")

    def _clear_captured_phrases(self) -> None:
        self.captured_samples = []
        self.lbl_feedback.configure(text="Amostras limpas.", text_color="#94a3b8")
        self._update_phrase_preview()

    def _save_mic_profile(self) -> None:
        if self.is_busy or not self.captured_samples:
            return
        name = self.entry_mic_name.get().strip()
        if not name:
            self.lbl_feedback.configure(text="Informe o Nome do Locutor antes de salvar.", text_color="#ef4444")
            return

        self.is_busy = True
        self.btn_save_mic_profile.configure(state="disabled")
        self.lbl_feedback.configure(text=f"Processando biometria de '{name}'...", text_color="#38bdf8")

        samples_copy = list(self.captured_samples)

        def worker():
            try:
                res = self.vp.enroll_profile_from_arrays(name=name, samples_list=samples_copy, sample_rate=16000)
                msg = f"✅ Voiceprint salvo para '{res['name']}' ({res['total_speech_seconds']:.1f}s úteis)!"
                ok = True
            except Exception as e:
                msg = f"Erro ao cadastrar voiceprint: {e}"
                ok = False

            def done():
                self.is_busy = False
                if ok:
                    self.captured_samples = []
                self._refresh_model_status()
                self._update_phrase_preview()
                self._refresh_profiles_list()
                self.lbl_feedback.configure(text=msg, text_color="#10b981" if ok else "#ef4444")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True, name="VoiceprintEnrollMic").start()

    def _pick_wav_file(self) -> None:
        atas_root = get_base_dir() / "atas_reunioes"
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Selecione uma gravação WAV da reunião",
            initialdir=str(atas_root) if atas_root.exists() else str(get_base_dir()),
            filetypes=[("Áudios WAV", "*.wav"), ("Todos os Arquivos", "*.*")],
        )
        if chosen:
            self.selected_wav_path = Path(chosen)
            self.lbl_wav_file.configure(text=self.selected_wav_path.name)

    def _save_wav_profile(self) -> None:
        if self.is_busy:
            return
        if not self.selected_wav_path or not self.selected_wav_path.exists():
            self.lbl_feedback.configure(text="Selecione um arquivo .wav válido.", text_color="#ef4444")
            return
        name = self.entry_wav_name.get().strip()
        if not name:
            self.lbl_feedback.configure(text="Informe o Nome do Locutor.", text_color="#ef4444")
            return
        try:
            s_sec = parse_time_to_seconds(self.entry_wav_start.get())
            e_sec = parse_time_to_seconds(self.entry_wav_end.get())
        except Exception:
            self.lbl_feedback.configure(text="Formato de tempo inválido (use MM:SS ou segundos).", text_color="#ef4444")
            return

        ch_choice = self.opt_wav_channel.get()
        ch_idx = 0 if "Canal L" in ch_choice else (1 if "Canal R" in ch_choice else -1)

        self.is_busy = True
        self.btn_enroll_wav.configure(state="disabled")
        self.lbl_feedback.configure(text=f"Extraindo voiceprint de '{name}'...", text_color="#38bdf8")

        wav_path = self.selected_wav_path

        def worker():
            try:
                res = self.vp.enroll_profile_from_wav_segment(
                    name=name,
                    wav_path=wav_path,
                    start_seconds=s_sec,
                    end_seconds=e_sec,
                    channel=ch_idx,
                )
                msg = f"✅ Voiceprint extraído para '{res['name']}' ({res['total_speech_seconds']:.1f}s úteis)!"
                ok = True
            except Exception as e:
                msg = f"Erro ao extrair de WAV: {e}"
                ok = False

            def done():
                self.is_busy = False
                self.btn_enroll_wav.configure(state="normal")
                self._refresh_model_status()
                self._refresh_profiles_list()
                self.lbl_feedback.configure(text=msg, text_color="#10b981" if ok else "#ef4444")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True, name="VoiceprintEnrollWav").start()


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

        self.title("Consumo & Cotas da API - Escutaí")
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


def find_matching_audio(md_path: Path) -> Optional[Path]:
    """Localiza o arquivo `audio_*.wav` correspondente a uma ata/transcrição na mesma pasta."""
    name = md_path.name
    if name.startswith("ata_executiva_"):
        cand = md_path.parent / (name.replace("ata_executiva_", "audio_", 1).rsplit(".", 1)[0] + ".wav")
        if cand.exists():
            return cand
    elif name.startswith("transcricao_"):
        cand = md_path.parent / (name.replace("transcricao_", "audio_", 1).rsplit(".", 1)[0] + ".wav")
        if cand.exists():
            return cand
    return None


def render_history_cards_into(
    scroll_container: ctk.CTkScrollableFrame,
    filter_mode: str,
    on_view_md,
    on_retranscribe_wav,
    on_open_archive=None,
    on_play_archive=None,
    on_retranscribe_archive=None,
    search_query: str = "",
    on_empty_action=None,
) -> None:
    """Popula um container rolável com cartões de pacotes .escutai/.omniscribe, .wav, atas e transcrições."""
    for child in scroll_container.winfo_children():
        child.destroy()

    atas_root = get_base_dir() / "atas_reunioes"
    if not atas_root.exists():
        empty_box = ctk.CTkFrame(scroll_container, fg_color="transparent")
        empty_box.pack(pady=44)
        ctk.CTkLabel(
            empty_box,
            text="Nenhuma ata ou gravação salva ainda.\nInicie uma gravação ou importe um pacote .escutai.",
            font=ctk.CTkFont(size=13),
            text_color=THEME_TEXT_MUTED,
            justify="center",
        ).pack(pady=(0, 12))
        if on_empty_action is not None:
            ctk.CTkButton(
                empty_box,
                text="🎙️ Ir para Gravação",
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color=COLOR_PRIMARY,
                hover_color=COLOR_PRIMARY_HOVER,
                height=34,
                command=on_empty_action,
            ).pack()
        return

    patterns = ("**/*.escutai", "**/*.omniscribe", "**/*.md", "**/*.wav")
    if ".escutai" in filter_mode or ".omniscribe" in filter_mode or "Pacote" in filter_mode:
        patterns = ("**/*.escutai", "**/*.omniscribe")
    elif "Áudios" in filter_mode:
        patterns = ("**/*.wav",)
    elif "Atas" in filter_mode or "Transcrições" in filter_mode:
        patterns = ("**/*.md",)

    files = [
        f
        for pat in patterns
        for f in atas_root.glob(pat)
        if f.is_file() and not f.name.startswith(".") and "temp_blocks" not in f.parts
    ]
    if "Atas" in filter_mode:
        files = [f for f in files if "ata_executiva" in f.name]
    elif "Transcrições" in filter_mode:
        files = [f for f in files if "transcricao" in f.name]

    q = (search_query or "").strip().lower()
    if q:
        files = [f for f in files if q in f.name.lower() or q in str(f.parent.name).lower()]

    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    if not files:
        ctk.CTkLabel(
            scroll_container,
            text="Nenhum item encontrado para este filtro/busca." if q else "Nenhum item encontrado para este filtro.",
            font=ctk.CTkFont(size=13),
            text_color=THEME_TEXT_MUTED,
        ).pack(pady=40)
        return

    for file_path in files:
        filename = file_path.name
        ext = file_path.suffix.lower()
        is_archive = ext in (".escutai", ".omniscribe")
        is_wav = ext == ".wav"
        is_ata = "ata_executiva" in filename
        is_transc = "transcricao" in filename

        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
        date_str = mtime.strftime("%d/%m/%Y às %H:%M")
        size_bytes = file_path.stat().st_size
        size_label = f"{size_bytes / (1024 * 1024):.1f} MB" if size_bytes >= 1024 * 1024 else f"{size_bytes // 1024 + 1} KB"

        card = ctk.CTkFrame(
            scroll_container,
            fg_color=THEME_CARD_ALT,
            border_color=THEME_BORDER,
            border_width=1,
            corner_radius=8,
        )
        card.pack(fill="x", padx=4, pady=4)
        card.grid_columnconfigure(1, weight=1)

        if is_archive:
            badge_text, badge_color, icon = "PACOTE SQLITE", "#7c3aed", "📦"
        elif is_wav:
            badge_text, badge_color, icon = "ÁUDIO WAV", COLOR_TEAL, "🔊"
        elif is_ata:
            badge_text, badge_color, icon = "ATA EXECUTIVA", COLOR_SUCCESS, "📋"
        elif is_transc:
            badge_text, badge_color, icon = "TRANSCRIÇÃO", COLOR_PRIMARY, "🎙️"
        else:
            badge_text, badge_color, icon = "DOCUMENTO", ("#64748b", "#4b5563"), "📄"

        ctk.CTkLabel(
            card,
            text=f"{icon} {badge_text}",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#ffffff",
            fg_color=badge_color,
            corner_radius=5,
            width=116,
            height=24,
        ).grid(row=0, column=0, padx=10, pady=10, sticky="w")

        info_frame = ctk.CTkFrame(card, fg_color="transparent")
        info_frame.grid(row=0, column=1, padx=6, pady=8, sticky="w")

        display_title = filename
        if is_archive:
            try:
                arch_info = _ARCHIVE_READER.read_archive_summary(file_path)
                display_title = f"{arch_info['title']} ({filename})"
                eng_tag = "Offline" if arch_info["engine_mode"] == "offline" else "Online"
                meta_txt = (
                    f"Duração: {format_seconds(arch_info['duration_seconds'])} • {eng_tag} • "
                    f"{arch_info['segment_count']} falas • {arch_info['compression_ratio']}x comp. • "
                    f"{size_label} • {date_str}"
                )
            except Exception:
                meta_txt = f"Pacote SQLite • {size_label} • {date_str}"
        elif is_wav:
            w_info = get_wav_info(file_path)
            ch_txt = "Mono" if w_info["channels"] == 1 else "Estéreo L/R"
            meta_txt = (
                f"Duração: {format_seconds(w_info['duration_seconds'])} • {ch_txt} "
                f"({w_info['sample_rate']} Hz) • {size_label} • {date_str}"
            )
        else:
            meta_txt = f"Modificado em: {date_str} • Tamanho: {size_label}"

        ctk.CTkLabel(
            info_frame,
            text=display_title,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        ).pack(anchor="w")

        ctk.CTkLabel(
            info_frame,
            text=meta_txt,
            font=ctk.CTkFont(size=11),
            text_color=THEME_TEXT_MUTED,
        ).pack(anchor="w")

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.grid(row=0, column=2, padx=10, pady=10, sticky="e")

        if is_archive:
            if on_play_archive is not None:
                ctk.CTkButton(
                    actions,
                    text="▶ Ouvir",
                    width=74,
                    height=30,
                    font=ctk.CTkFont(size=12, weight="bold"),
                    fg_color=BTN_NEUTRAL,
                    hover_color=BTN_NEUTRAL_HOVER,
                    text_color=THEME_TEXT_PRIMARY,
                    command=lambda p=file_path: on_play_archive(p),
                ).pack(side="left", padx=(0, 6))

            if on_retranscribe_archive is not None:
                ctk.CTkButton(
                    actions,
                    text="🔄 Áudio",
                    width=82,
                    height=30,
                    font=ctk.CTkFont(size=11, weight="bold"),
                    fg_color=COLOR_TEAL,
                    hover_color="#0e7490",
                    command=lambda p=file_path: on_retranscribe_archive(p),
                ).pack(side="left", padx=(0, 6))

            ctk.CTkButton(
                actions,
                text="Abrir",
                width=78,
                height=30,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="#7c3aed",
                hover_color="#6d28d9",
                command=(lambda p=file_path: on_open_archive(p)) if on_open_archive else (lambda p=file_path: on_view_md(p)),
            ).pack(side="left")
        elif is_wav:
            ctk.CTkButton(
                actions,
                text="▶ Ouvir",
                width=74,
                height=30,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color=BTN_NEUTRAL,
                hover_color=BTN_NEUTRAL_HOVER,
                text_color=THEME_TEXT_PRIMARY,
                command=lambda p=file_path: open_file_in_os(p),
            ).pack(side="left", padx=(0, 6))

            ctk.CTkButton(
                actions,
                text="🔄 Re-transcrever",
                width=126,
                height=30,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color=COLOR_TEAL,
                hover_color="#0e7490",
                command=lambda p=file_path: on_retranscribe_wav(p),
            ).pack(side="left")
        else:
            paired_wav = find_matching_audio(file_path)
            if paired_wav is not None:
                ctk.CTkButton(
                    actions,
                    text="🔄 Áudio",
                    width=80,
                    height=30,
                    font=ctk.CTkFont(size=11, weight="bold"),
                    fg_color=COLOR_TEAL,
                    hover_color="#0e7490",
                    command=lambda p=paired_wav: on_retranscribe_wav(p),
                ).pack(side="left", padx=(0, 6))

            ctk.CTkButton(
                actions,
                text="Visualizar",
                width=96,
                height=30,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color=COLOR_PRIMARY,
                hover_color=COLOR_PRIMARY_HOVER,
                command=lambda p=file_path: on_view_md(p),
            ).pack(side="left")


class HistoryModal(ctk.CTkToplevel):
    """Modal visual para listar, buscar e carregar atas e transcrições anteriores."""

    def __init__(self, parent: "ZoomTranscriberApp"):
        super().__init__(parent)
        self.parent = parent

        self.title("Histórico de Reuniões & Atas - Escutaí")
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
            text="Visualize atas/transcrições salvas, ouça gravações oficiais em áudio (.wav) ou re-transcreva para gerar uma nova Ata.",
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
            text="📂 Procurar arquivo (.md / .wav)...",
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
        render_history_cards_into(
            self.scroll_list,
            filter_mode="Tudo",
            on_view_md=self._select_file,
            on_retranscribe_wav=self._open_retranscribe,
            on_open_archive=lambda p: (self.parent.load_archive(p), self.destroy()),
            on_play_archive=self.parent.play_archive_audio,
            on_retranscribe_archive=lambda p: (self.destroy(), self.parent.retranscribe_archive(p)),
        )

    def _open_retranscribe(self, wav_path: Path) -> None:
        self.destroy()
        RetranscribeModal(self.parent, wav_path)

    def _select_file(self, file_path: Path) -> None:
        self.parent.load_document(file_path)
        self.destroy()

    def _browse_custom_file(self) -> None:
        atas_root = get_base_dir() / "atas_reunioes"
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Selecione um Pacote (.escutai / .omniscribe), Ata (.md) ou Gravação (.wav)",
            initialdir=str(atas_root) if atas_root.exists() else str(get_base_dir()),
            filetypes=[
                ("Reuniões Escutaí", "*.escutai *.omniscribe *.md *.wav"),
                ("Pacotes SQLite", "*.escutai *.omniscribe"),
                ("Gravações WAV", "*.wav"),
                ("Documentos Markdown", "*.md"),
                ("Todos os Arquivos", "*.*"),
            ],
        )
        if chosen:
            chosen_path = Path(chosen)
            if chosen_path.suffix.lower() in (".escutai", ".omniscribe"):
                self.parent.import_archive_file(chosen_path)
                self.destroy()
            elif chosen_path.suffix.lower() == ".wav":
                self._open_retranscribe(chosen_path)
            else:
                self._select_file(chosen_path)


class ZoomTranscriberApp(ctk.CTk):
    """Aplicação Principal v2.0 (Escutaí) com abas limpas, tema Escuro/Claro e mascote Vozito."""

    TAB_REC = "🎙️ Gravação"
    TAB_ATA = "📋 Ata"
    TAB_HIST = "🗂️ Histórico"
    TAB_CFG = "⚙️ Config & Voz"

    MODE_ONLINE = "🌐 Online (Sistema+Mic)"
    MODE_PRESENCIAL = "🏢 Presencial (Só Mic)"
    ENGINE_ONLINE = "☁️ Gemini"
    ENGINE_OFFLINE = "💻 Offline"

    def __init__(self):
        super().__init__()

        ui_cfg = self._load_ui_settings()
        self.theme_mode = "Light" if str(ui_cfg.get("theme", "")).lower() == "light" else "Dark"
        self.engine_mode = "offline" if str(ui_cfg.get("engine_mode", "")).lower() == "offline" else "online"
        self.offline_whisper_variant = "base" if ui_cfg.get("whisper_variant") == "base" else "small"
        self.offline_ollama_model = str(ui_cfg.get("ollama_model", "") or "")
        self.auto_export_sqlite = bool(ui_cfg.get("auto_export_sqlite", True))
        ctk.set_appearance_mode(self.theme_mode)

        self.title("Escutaí - Transcrição e Atas Inteligentes de Reuniões")
        self.geometry("1040x760")
        self.minsize(960, 680)
        self.configure(fg_color=THEME_BG)

        # Estado da gravação / re-transcrição
        self.is_recording = False
        self.is_retranscribing = False
        self.recording_start_time = 0.0
        self.block_count = 0
        self._logs_expanded = False
        self._api_key_visible = False

        # Mapeamentos de hardware de áudio
        self.loopback_map: Dict[str, Optional[int]] = {}
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
            on_archive_generated=self._handle_archive_generated,
            defer_init=True,
        )
        self.gemini_service.engine_mode = self.engine_mode
        self.gemini_service.offline_whisper_variant = self.offline_whisper_variant
        self.gemini_service.offline_ollama_model = self.offline_ollama_model
        self.gemini_service.auto_export_sqlite = self.auto_export_sqlite

        self.audio_engine = AudioEngine(
            temp_dir=self.temp_dir,
            on_rms_update=self._handle_rms_update,
            on_block_ready=self._handle_block_ready,
        )

        # Constrói componentes da interface gráfica limpa v2.0
        self._build_ui()

        # Pinta a janela imediatamente antes de carregar áudio/SDK pesado
        self.update()

        self._timer_running = False
        self.mascot: Optional[VozitoWidget] = None

        self.after(60, self._refresh_audio_devices)
        self.after(120, self._deferred_init_services)
        self.after(200, self._check_initial_api_key)
        self.after(280, self._init_mascot)

    @staticmethod
    def _ui_settings_path() -> Path:
        return get_base_dir() / "atas_reunioes" / "ui_settings.json"

    def _load_ui_settings(self) -> Dict:
        try:
            cfg_path = self._ui_settings_path()
            if cfg_path.exists():
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_ui_settings(self) -> None:
        try:
            cfg_path = self._ui_settings_path()
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                json.dumps(
                    {
                        "theme": self.theme_mode,
                        "engine_mode": self.engine_mode,
                        "whisper_variant": self.offline_whisper_variant,
                        "ollama_model": self.offline_ollama_model,
                        "auto_export_sqlite": self.auto_export_sqlite,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_theme_preference(self) -> str:
        return "Light" if str(self._load_ui_settings().get("theme", "")).lower() == "light" else "Dark"

    def _save_theme_preference(self) -> None:
        self._save_ui_settings()

    def _deferred_init_services(self) -> None:
        self.gemini_service._init_client()
        self._refresh_usage_panel()
        self._refresh_voiceprint_summary()
        self._refresh_offline_status()
        self._update_hw_summary_label()

    def _build_ui(self) -> None:
        """Constrói o layout minimalista com barra superior de 4 abas e alternador Escuro/Claro."""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ---------------- 1. BARRA SUPERIOR MINIMALISTA ----------------
        topbar = ctk.CTkFrame(
            self,
            fg_color=THEME_CARD,
            border_color=THEME_BORDER,
            border_width=1,
            corner_radius=12,
            height=60,
        )
        topbar.grid(row=0, column=0, padx=16, pady=(12, 8), sticky="ew")
        topbar.grid_columnconfigure(1, weight=1)

        brand_box = ctk.CTkFrame(topbar, fg_color="transparent")
        brand_box.grid(row=0, column=0, padx=16, pady=10, sticky="w")

        ctk.CTkLabel(
            brand_box,
            text="🎙️ Escutaí",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        ).pack(side="left")

        ctk.CTkLabel(
            brand_box,
            text="v2.0",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=COLOR_PRIMARY_SOFT,
            text_color=COLOR_PRIMARY,
            corner_radius=8,
            padx=8,
            pady=2,
        ).pack(side="left", padx=(8, 0))

        self.nav_tabs = ctk.CTkSegmentedButton(
            topbar,
            values=[self.TAB_REC, self.TAB_ATA, self.TAB_HIST, self.TAB_CFG],
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=THEME_INPUT,
            selected_color=COLOR_PRIMARY,
            selected_hover_color=COLOR_PRIMARY_HOVER,
            unselected_color=THEME_INPUT,
            unselected_hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            height=34,
            command=self._on_nav_change,
        )
        self.nav_tabs.set(self.TAB_REC)
        self.nav_tabs.grid(row=0, column=1, padx=12, pady=10)

        top_utils = ctk.CTkFrame(topbar, fg_color="transparent")
        top_utils.grid(row=0, column=2, padx=16, pady=10, sticky="e")

        self.btn_theme_toggle = ctk.CTkButton(
            top_utils,
            text="☀️ Claro" if self.theme_mode == "Dark" else "🌙 Escuro",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            width=92,
            height=32,
            corner_radius=8,
            command=self._toggle_theme,
        )
        self.btn_theme_toggle.pack(side="left", padx=(0, 8))

        self.btn_toggle_mascot = ctk.CTkButton(
            top_utils,
            text="🤖 Vozito",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            width=102,
            height=32,
            corner_radius=8,
            command=self._toggle_mascot,
        )
        self.btn_toggle_mascot.pack(side="left")

        # ---------------- 2. CONTAINER DAS 4 PÁGINAS ----------------
        self.page_container = ctk.CTkFrame(self, fg_color="transparent")
        self.page_container.grid(row=1, column=0, padx=16, pady=(0, 14), sticky="nsew")
        self.page_container.grid_columnconfigure(0, weight=1)
        self.page_container.grid_rowconfigure(0, weight=1)

        self.pages: Dict[str, ctk.CTkFrame] = {}
        self._build_recording_page()
        self._build_minutes_page()
        self._build_history_page()
        self._build_settings_page()

        self._switch_tab(self.TAB_REC)

    def _build_recording_page(self) -> None:
        """Aba 1: Gravação limpa, focada apenas na reunião e transcrição ao vivo."""
        page = ctk.CTkFrame(self.page_container, fg_color="transparent")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        self.pages[self.TAB_REC] = page

        # Cartão de controle principal
        sess_card = ctk.CTkFrame(
            page, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=12
        )
        sess_card.grid(row=0, column=0, sticky="ew")
        sess_card.grid_columnconfigure((0, 1), weight=1)

        # Linha 0: Título + Glossário (50%/50%) e Linha 1: Captação + Motor de IA
        inputs_row = ctk.CTkFrame(sess_card, fg_color="transparent")
        inputs_row.grid(row=0, column=0, columnspan=2, padx=16, pady=(14, 10), sticky="ew")
        inputs_row.grid_columnconfigure((0, 1), weight=1)

        title_col = ctk.CTkFrame(inputs_row, fg_color="transparent")
        title_col.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        ctk.CTkLabel(
            title_col, text="TÍTULO DA REUNIÃO", font=ctk.CTkFont(size=10, weight="bold"), text_color=THEME_TEXT_MUTED
        ).pack(anchor="w", pady=(0, 4))
        self.entry_meeting_title = ctk.CTkEntry(
            title_col,
            placeholder_text="Ex: Alinhamento Semanal BI / Reunião Presencial",
            fg_color=THEME_INPUT,
            border_color=THEME_BORDER,
            text_color=THEME_TEXT_PRIMARY,
            height=36,
        )
        self.entry_meeting_title.pack(fill="x")

        ctx_col = ctk.CTkFrame(inputs_row, fg_color="transparent")
        ctx_col.grid(row=0, column=1, padx=(8, 0), sticky="ew")
        ctk.CTkLabel(
            ctx_col, text="GLOSSÁRIO / NOMES (OPCIONAL)", font=ctk.CTkFont(size=10, weight="bold"), text_color=THEME_TEXT_MUTED
        ).pack(anchor="w", pady=(0, 4))
        self.entry_context_notes = ctk.CTkEntry(
            ctx_col,
            placeholder_text="Ex: Carlos, Juliana; BI, Deploy, WASAPI",
            fg_color=THEME_INPUT,
            border_color=THEME_BORDER,
            text_color=THEME_TEXT_PRIMARY,
            height=36,
        )
        self.entry_context_notes.pack(fill="x")

        modes_subrow = ctk.CTkFrame(inputs_row, fg_color="transparent")
        modes_subrow.grid(row=1, column=0, columnspan=2, pady=(10, 0), sticky="ew")
        modes_subrow.grid_columnconfigure((0, 1), weight=1)

        mode_col = ctk.CTkFrame(modes_subrow, fg_color="transparent")
        mode_col.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            mode_col, text="CAPTAÇÃO", font=ctk.CTkFont(size=10, weight="bold"), text_color=THEME_TEXT_MUTED
        ).pack(side="left", padx=(0, 8))
        self.seg_capture_mode = ctk.CTkSegmentedButton(
            mode_col,
            values=[self.MODE_ONLINE, self.MODE_PRESENCIAL],
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=THEME_INPUT,
            selected_color=COLOR_PRIMARY,
            selected_hover_color=COLOR_PRIMARY_HOVER,
            unselected_color=THEME_INPUT,
            unselected_hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            height=32,
            command=self._on_capture_mode_change,
        )
        self.seg_capture_mode.set(self.MODE_ONLINE)
        self.seg_capture_mode.pack(side="left")

        eng_col = ctk.CTkFrame(modes_subrow, fg_color="transparent")
        eng_col.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            eng_col, text="MOTOR DE IA", font=ctk.CTkFont(size=10, weight="bold"), text_color=THEME_TEXT_MUTED
        ).pack(side="left", padx=(0, 8))
        self.seg_engine_mode = ctk.CTkSegmentedButton(
            eng_col,
            values=[self.ENGINE_ONLINE, self.ENGINE_OFFLINE],
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=THEME_INPUT,
            selected_color="#7c3aed",
            selected_hover_color="#6d28d9",
            unselected_color=THEME_INPUT,
            unselected_hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            height=32,
            command=self._on_engine_mode_change,
        )
        self.seg_engine_mode.set(self.ENGINE_OFFLINE if self.engine_mode == "offline" else self.ENGINE_ONLINE)
        self.seg_engine_mode.pack(side="left")

        # Linha 1: Botão Único Principal + Timer + Status + VU compactos
        sep = ctk.CTkFrame(sess_card, height=1, fg_color=THEME_BORDER)
        sep.grid(row=1, column=0, columnspan=2, padx=16, sticky="ew")

        action_row = ctk.CTkFrame(sess_card, fg_color="transparent")
        action_row.grid(row=2, column=0, columnspan=2, padx=16, pady=12, sticky="ew")
        action_row.grid_columnconfigure(1, weight=1)

        left_act = ctk.CTkFrame(action_row, fg_color="transparent")
        left_act.grid(row=0, column=0, sticky="w")

        self.btn_rec_action = ctk.CTkButton(
            left_act,
            text="▶ Iniciar Gravação",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLOR_SUCCESS,
            hover_color="#059669",
            width=195,
            height=40,
            corner_radius=9,
            command=self._toggle_recording_action,
        )
        self.btn_rec_action.pack(side="left", padx=(0, 14))

        self.lbl_timer = ctk.CTkLabel(
            left_act,
            text="⏱ 00:00:00",
            font=ctk.CTkFont(family="Consolas", size=22, weight="bold"),
            text_color=COLOR_PRIMARY,
        )
        self.lbl_timer.pack(side="left", padx=(0, 14))

        self.lbl_status = ctk.CTkLabel(
            left_act,
            text="● Pronto para iniciar",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=COLOR_SUCCESS,
        )
        self.lbl_status.pack(side="left")

        vu_box = ctk.CTkFrame(action_row, fg_color="transparent")
        vu_box.grid(row=0, column=1, sticky="e")

        self.vu_zoom_row = ctk.CTkFrame(vu_box, fg_color="transparent")
        self.vu_zoom_row.pack(anchor="e", pady=2)
        ctk.CTkLabel(
            self.vu_zoom_row, text="Sistema", font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME_TEXT_MUTED, width=54, anchor="e"
        ).pack(side="left")
        self.bar_vu_zoom = ctk.CTkProgressBar(
            self.vu_zoom_row, width=130, height=9, progress_color=COLOR_PRIMARY, fg_color=THEME_INPUT, corner_radius=6
        )
        self.bar_vu_zoom.set(0.0)
        self.bar_vu_zoom.pack(side="left", padx=6)
        self.lbl_vu_zoom_val = ctk.CTkLabel(
            self.vu_zoom_row, text="0%", font=ctk.CTkFont(size=11), text_color=THEME_TEXT_DIM, width=30
        )
        self.lbl_vu_zoom_val.pack(side="left")

        vu_mic_row = ctk.CTkFrame(vu_box, fg_color="transparent")
        vu_mic_row.pack(anchor="e", pady=2)
        ctk.CTkLabel(
            vu_mic_row, text="Mic", font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME_TEXT_MUTED, width=54, anchor="e"
        ).pack(side="left")
        self.bar_vu_mic = ctk.CTkProgressBar(
            vu_mic_row, width=130, height=9, progress_color=COLOR_SUCCESS, fg_color=THEME_INPUT, corner_radius=6
        )
        self.bar_vu_mic.set(0.0)
        self.bar_vu_mic.pack(side="left", padx=6)
        self.lbl_vu_mic_val = ctk.CTkLabel(
            vu_mic_row, text="0%", font=ctk.CTkFont(size=11), text_color=THEME_TEXT_DIM, width=30
        )
        self.lbl_vu_mic_val.pack(side="left")

        # Barra de progresso discreta exibida apenas durante processamento assíncrono/downloads
        self.bar_async_progress = ctk.CTkProgressBar(
            sess_card, height=4, progress_color=COLOR_PRIMARY, fg_color=THEME_INPUT, corner_radius=2, mode="indeterminate"
        )

        # Linha discreta de resumo do hardware + atalho para Config & Voz
        meta_bar = ctk.CTkFrame(page, fg_color="transparent")
        meta_bar.grid(row=1, column=0, pady=(8, 6), sticky="ew")
        meta_bar.grid_columnconfigure(0, weight=1)

        self.lbl_hw_summary = ctk.CTkLabel(
            meta_bar,
            text="Hardware: detectando... • Blocos de 3 min",
            font=ctk.CTkFont(size=11),
            text_color=THEME_TEXT_MUTED,
        )
        self.lbl_hw_summary.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            meta_bar,
            text="Ajustar hardware ou voiceprints →",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="transparent",
            hover_color=THEME_CARD_ALT,
            text_color=COLOR_PRIMARY,
            height=22,
            width=210,
            command=lambda: self._switch_tab(self.TAB_CFG),
        ).grid(row=0, column=1, sticky="e")

        # Transcrição ao Vivo (ampla e limpa)
        trans_card = ctk.CTkFrame(
            page, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=12
        )
        trans_card.grid(row=2, column=0, sticky="nsew")
        trans_card.grid_columnconfigure(0, weight=1)
        trans_card.grid_rowconfigure(1, weight=1)

        trans_head = ctk.CTkFrame(trans_card, fg_color="transparent")
        trans_head.grid(row=0, column=0, padx=16, pady=(12, 6), sticky="ew")
        trans_head.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            trans_head,
            text="Transcrição ao Vivo",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        ).grid(row=0, column=0, sticky="w")

        self.btn_copy_trans = ctk.CTkButton(
            trans_head,
            text="📋 Copiar Texto",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            width=136,
            height=28,
            command=self._copy_transcription,
        )
        self.btn_copy_trans.grid(row=0, column=1, sticky="e")

        self.txt_transcription = ctk.CTkTextbox(
            trans_card,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=THEME_INPUT,
            text_color=THEME_TEXT_PRIMARY,
            border_color=THEME_BORDER,
            border_width=1,
            corner_radius=9,
            wrap="word",
        )
        self.txt_transcription.grid(row=1, column=0, padx=14, pady=(0, 14), sticky="nsew")
        self.txt_transcription.insert(
            "end",
            "A transcrição incremental com diarização e voiceprints aparecerá aqui conforme os blocos forem gravados...\n",
        )

    def _build_minutes_page(self) -> None:
        """Aba 2: Leitura limpa da Ata Executiva gerada."""
        page = ctk.CTkFrame(self.page_container, fg_color="transparent")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)
        self.pages[self.TAB_ATA] = page

        card = ctk.CTkFrame(page, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=12)
        card.grid(row=0, column=0, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, padx=16, pady=(14, 8), sticky="ew")
        head.grid_columnconfigure(0, weight=1)

        title_box = ctk.CTkFrame(head, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_box,
            text="📋 Ata Executiva da Reunião",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        ).pack(anchor="w")
        self.lbl_ata_meta = ctk.CTkLabel(
            title_box,
            text="Síntese em 5 seções revisada automaticamente em segunda passada fono-semântica",
            font=ctk.CTkFont(size=11),
            text_color=THEME_TEXT_MUTED,
        )
        self.lbl_ata_meta.pack(anchor="w")

        btn_row = ctk.CTkFrame(head, fg_color="transparent")
        btn_row.grid(row=0, column=1, sticky="e")

        self.btn_open_folder = ctk.CTkButton(
            btn_row,
            text="📂 Abrir Pasta",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            width=116,
            height=32,
            command=self._open_atas_folder,
        )
        self.btn_open_folder.pack(side="left", padx=(0, 8))

        self.btn_export_sqlite = ctk.CTkButton(
            btn_row,
            text="📦 Exportar .escutai",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#7c3aed",
            hover_color="#6d28d9",
            width=166,
            height=32,
            command=self._export_current_archive,
        )
        self.btn_export_sqlite.pack(side="left", padx=(0, 8))

        self.btn_copy_min = ctk.CTkButton(
            btn_row,
            text="📋 Copiar Ata Markdown",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=COLOR_PRIMARY,
            hover_color=COLOR_PRIMARY_HOVER,
            width=168,
            height=32,
            command=self._copy_minutes,
        )
        self.btn_copy_min.pack(side="left")

        self.txt_minutes = ctk.CTkTextbox(
            card,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=THEME_INPUT,
            text_color=THEME_TEXT_PRIMARY,
            border_color=THEME_BORDER,
            border_width=1,
            corner_radius=9,
            wrap="word",
        )
        self.txt_minutes.grid(row=1, column=0, padx=14, pady=(0, 14), sticky="nsew")
        self.txt_minutes.insert(
            "end",
            "Ao encerrar uma gravação ou re-transcrição, a Ata Executiva consolidada será exibida aqui.\n",
        )

    def _build_history_page(self) -> None:
        """Aba 3: Histórico nativo unificado de gravações .wav, Atas e Transcrições."""
        page = ctk.CTkFrame(self.page_container, fg_color="transparent")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self.pages[self.TAB_HIST] = page

        top = ctk.CTkFrame(page, fg_color="transparent")
        top.grid(row=0, column=0, pady=(2, 10), sticky="ew")
        top.grid_columnconfigure(0, weight=1)

        search_row = ctk.CTkFrame(top, fg_color="transparent")
        search_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        search_row.grid_columnconfigure(0, weight=1)

        self.entry_hist_search = ctk.CTkEntry(
            search_row,
            placeholder_text="🔍 Buscar por título, data ou nome do arquivo...",
            fg_color=THEME_CARD,
            border_color=THEME_BORDER,
            text_color=THEME_TEXT_PRIMARY,
            height=34,
        )
        self.entry_hist_search.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        self.entry_hist_search.bind("<KeyRelease>", lambda _e: self._refresh_history_tab())

        hist_btns = ctk.CTkFrame(search_row, fg_color="transparent")
        hist_btns.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(
            hist_btns,
            text="↻ Atualizar",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            width=92,
            height=34,
            command=self._refresh_history_tab,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            hist_btns,
            text="📥 Importar Pacote",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#7c3aed",
            hover_color="#6d28d9",
            height=34,
            command=self._import_external_archive_dialog,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            hist_btns,
            text="📂 Abrir arquivo...",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            height=34,
            command=self._browse_external_history_file,
        ).pack(side="left")

        self.seg_hist_filter = ctk.CTkSegmentedButton(
            top,
            values=["Tudo", "📦 Pacotes", "🔊 Áudios (.wav)", "📋 Atas", "🎙️ Transcrições"],
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=THEME_CARD,
            selected_color=COLOR_PRIMARY,
            selected_hover_color=COLOR_PRIMARY_HOVER,
            unselected_color=THEME_CARD,
            unselected_hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            height=32,
            command=lambda _v: self._refresh_history_tab(),
        )
        self.seg_hist_filter.set("Tudo")
        self.seg_hist_filter.grid(row=1, column=0, sticky="w")

        self.hist_scroll = ctk.CTkScrollableFrame(
            page,
            fg_color=THEME_CARD,
            border_color=THEME_BORDER,
            border_width=1,
            corner_radius=12,
        )
        self.hist_scroll.grid(row=1, column=0, sticky="nsew")
        self.hist_scroll.grid_columnconfigure(0, weight=1)

    def _build_settings_page(self) -> None:
        """Aba 4: Configurações de Hardware, API/Cotas, Voiceprints e Logs recolhíveis."""
        page = ctk.CTkFrame(self.page_container, fg_color="transparent")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)
        self.pages[self.TAB_CFG] = page

        scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure((0, 1), weight=1)

        # Card 1: Áudio & Captura
        audio_card = ctk.CTkFrame(
            scroll, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=12
        )
        audio_card.grid(row=0, column=0, padx=(0, 7), pady=(0, 12), sticky="nsew")
        audio_card.grid_columnconfigure(0, weight=1)

        a_head = ctk.CTkFrame(audio_card, fg_color="transparent")
        a_head.grid(row=0, column=0, padx=16, pady=(14, 8), sticky="ew")
        a_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            a_head, text="🎧 Áudio & Captura (WASAPI)", font=ctk.CTkFont(size=14, weight="bold"), text_color=THEME_TEXT_PRIMARY
        ).grid(row=0, column=0, sticky="w")
        self.btn_refresh_devices = ctk.CTkButton(
            a_head,
            text="↻ Detectar",
            width=86,
            height=26,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            command=self._refresh_audio_devices,
        )
        self.btn_refresh_devices.grid(row=0, column=1, sticky="e")

        a_body = ctk.CTkFrame(audio_card, fg_color="transparent")
        a_body.grid(row=1, column=0, padx=16, pady=(0, 14), sticky="ew")
        a_body.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(a_body, text="Áudio do Sistema (Loopback L):", font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME_TEXT_MUTED).pack(anchor="w")
        self.opt_loopback = _style_option_menu(
            ctk.CTkOptionMenu(
                a_body, values=["Procurando dispositivos..."], command=lambda _v: self._on_loopback_selected()
            )
        )
        self.opt_loopback.pack(fill="x", pady=(3, 10))

        ctk.CTkLabel(a_body, text="Microfone Local (R / Presencial):", font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME_TEXT_MUTED).pack(anchor="w")
        self.opt_mic = _style_option_menu(
            ctk.CTkOptionMenu(
                a_body, values=["Procurando dispositivos..."], command=lambda _v: self._update_hw_summary_label()
            )
        )
        self.opt_mic.pack(fill="x", pady=(3, 10))

        blk_row = ctk.CTkFrame(a_body, fg_color="transparent")
        blk_row.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(blk_row, text="Tamanho do Bloco:", font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME_TEXT_MUTED).pack(side="left")
        self.opt_block_duration = _style_option_menu(
            ctk.CTkOptionMenu(
                blk_row, values=["1 minuto", "3 minutos", "5 minutos"], width=130, command=lambda _v: self._update_hw_summary_label()
            )
        )
        self.opt_block_duration.set("1 minuto")
        self.opt_block_duration.pack(side="right")

        self.chk_noise_gate = ctk.CTkCheckBox(
            a_body,
            text="Ativar Noise Gate suave no microfone (hold 150 ms)",
            command=self._on_toggle_noise_gate,
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_PRIMARY,
        )
        self.chk_noise_gate.select()
        self.chk_noise_gate.pack(anchor="w", pady=(4, 4))

        self.chk_mic_mute = ctk.CTkCheckBox(
            a_body,
            text="Mutar microfone em tempo real (Modo Ausente)",
            command=self._on_toggle_mic_mute,
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_PRIMARY,
        )
        self.chk_mic_mute.pack(anchor="w")

        # Card 2: Gemini API & Free Tier
        api_card = ctk.CTkFrame(
            scroll, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=12
        )
        api_card.grid(row=0, column=1, padx=(7, 0), pady=(0, 12), sticky="nsew")
        api_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            api_card, text="🔑 Gemini API & Cota Free Tier", font=ctk.CTkFont(size=14, weight="bold"), text_color=THEME_TEXT_PRIMARY
        ).grid(row=0, column=0, padx=16, pady=(14, 10), sticky="w")

        k_body = ctk.CTkFrame(api_card, fg_color="transparent")
        k_body.grid(row=1, column=0, padx=16, pady=(0, 14), sticky="ew")
        k_body.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(k_body, text="GEMINI_API_KEY:", font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME_TEXT_MUTED).pack(anchor="w")
        key_row = ctk.CTkFrame(k_body, fg_color="transparent")
        key_row.pack(fill="x", pady=(3, 12))

        self.entry_cfg_api_key = ctk.CTkEntry(
            key_row,
            placeholder_text="Cole sua chave AIza...",
            show="•",
            fg_color=THEME_INPUT,
            border_color=THEME_BORDER,
            text_color=THEME_TEXT_PRIMARY,
        )
        if self.gemini_service.api_key:
            self.entry_cfg_api_key.insert(0, self.gemini_service.api_key)
        self.entry_cfg_api_key.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_toggle_key_vis = ctk.CTkButton(
            key_row,
            text="👁",
            width=34,
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            command=self._toggle_cfg_key_visibility,
        )
        self.btn_toggle_key_vis.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            key_row,
            text="Salvar",
            width=72,
            fg_color=COLOR_PRIMARY,
            hover_color=COLOR_PRIMARY_HOVER,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._save_cfg_api_key,
        ).pack(side="left")

        self.lbl_usage_req = ctk.CTkLabel(
            k_body, text="Requisições hoje: 0 / 1.500 (0.0%)", font=ctk.CTkFont(size=12, weight="bold"), text_color=THEME_TEXT_PRIMARY
        )
        self.lbl_usage_req.pack(anchor="w")

        self.bar_usage_quota = ctk.CTkProgressBar(
            k_body, height=10, progress_color=COLOR_PRIMARY, fg_color=THEME_INPUT, corner_radius=6
        )
        self.bar_usage_quota.set(0.0)
        self.bar_usage_quota.pack(fill="x", pady=(6, 8))

        self.lbl_usage_cost = ctk.CTkLabel(
            k_body,
            text="Reuniões: 0 • Áudio: 0 min • Custo ref.: US$ 0.0000 (R$ 0.00)",
            font=ctk.CTkFont(size=11),
            text_color=THEME_TEXT_MUTED,
        )
        self.lbl_usage_cost.pack(anchor="w")

        # Card 3: Biometria Vocal (Voiceprints)
        vp_card = ctk.CTkFrame(
            scroll, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=12
        )
        vp_card.grid(row=1, column=0, columnspan=2, pady=(0, 12), sticky="ew")
        vp_card.grid_columnconfigure(0, weight=1)

        vp_left = ctk.CTkFrame(vp_card, fg_color="transparent")
        vp_left.grid(row=0, column=0, padx=16, pady=14, sticky="w")
        ctk.CTkLabel(
            vp_left, text="🗣️ Biometria Vocal (Voiceprints Locais ONNX)", font=ctk.CTkFont(size=14, weight="bold"), text_color=THEME_TEXT_PRIMARY
        ).pack(anchor="w")
        self.lbl_vp_summary = ctk.CTkLabel(
            vp_left,
            text="Verificando perfis cadastrados...",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        )
        self.lbl_vp_summary.pack(anchor="w", pady=(2, 0))

        vp_btns = ctk.CTkFrame(vp_card, fg_color="transparent")
        vp_btns.grid(row=0, column=1, padx=16, pady=14, sticky="e")

        ctk.CTkButton(
            vp_btns,
            text="🎙️ Gerenciar / Cadastrar Perfis",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=COLOR_TEAL,
            hover_color="#0e7490",
            height=34,
            command=self._open_voiceprints_modal,
        ).pack(side="left")

        # Card 4: Modo Offline (Whisper ONNX + Ollama) & Pacote Portátil SQLite
        off_card = ctk.CTkFrame(
            scroll, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=12
        )
        off_card.grid(row=2, column=0, columnspan=2, pady=(0, 12), sticky="ew")
        off_card.grid_columnconfigure(0, weight=1)

        off_left = ctk.CTkFrame(off_card, fg_color="transparent")
        off_left.grid(row=0, column=0, padx=16, pady=(14, 6), sticky="w")
        ctk.CTkLabel(
            off_left,
            text="💻 Transcrição Offline (Whisper ONNX + Ollama) & Pacote .escutai",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=THEME_TEXT_PRIMARY,
        ).pack(anchor="w")
        self.lbl_offline_status = ctk.CTkLabel(
            off_left,
            text="Verificando modelos Whisper ONNX e Ollama local...",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_MUTED,
        )
        self.lbl_offline_status.pack(anchor="w", pady=(2, 0))

        off_btns = ctk.CTkFrame(off_card, fg_color="transparent")
        off_btns.grid(row=0, column=1, padx=16, pady=(14, 6), sticky="e")

        self.opt_whisper_variant = _style_option_menu(
            ctk.CTkOptionMenu(
                off_btns,
                values=["small", "base"],
                width=96,
                command=self._on_whisper_variant_change,
            )
        )
        self.opt_whisper_variant.set(self.offline_whisper_variant)
        self.opt_whisper_variant.pack(side="left", padx=(0, 8))

        self.btn_download_whisper = ctk.CTkButton(
            off_btns,
            text="⬇️ Baixar / Verificar Offline",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#7c3aed",
            hover_color="#6d28d9",
            height=32,
            command=self._download_offline_models_async,
        )
        self.btn_download_whisper.pack(side="left")

        off_bottom = ctk.CTkFrame(off_card, fg_color="transparent")
        off_bottom.grid(row=1, column=0, columnspan=2, padx=16, pady=(0, 14), sticky="ew")
        self.bar_offline_progress = ctk.CTkProgressBar(
            off_bottom, height=6, progress_color="#7c3aed", fg_color=THEME_INPUT, corner_radius=4
        )
        self.bar_offline_progress.set(0.0)
        self.chk_auto_sqlite = ctk.CTkCheckBox(
            off_bottom,
            text="Gerar pacote portátil SQLite (.escutai com áudio compactado) automaticamente ao encerrar",
            font=ctk.CTkFont(size=12),
            text_color=THEME_TEXT_PRIMARY,
            command=self._on_toggle_auto_sqlite,
        )
        if self.auto_export_sqlite:
            self.chk_auto_sqlite.select()
        self.chk_auto_sqlite.pack(anchor="w")

        # Card 5: Logs Técnicos (Recolhível)
        log_card = ctk.CTkFrame(
            scroll, fg_color=THEME_CARD, border_color=THEME_BORDER, border_width=1, corner_radius=12
        )
        log_card.grid(row=3, column=0, columnspan=2, sticky="ew")
        log_card.grid_columnconfigure(0, weight=1)

        log_head = ctk.CTkFrame(log_card, fg_color="transparent")
        log_head.grid(row=0, column=0, padx=16, pady=12, sticky="ew")
        log_head.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_head, text="🧾 Logs Técnicos do Sistema", font=ctk.CTkFont(size=13, weight="bold"), text_color=THEME_TEXT_PRIMARY
        ).grid(row=0, column=0, sticky="w")

        log_btns = ctk.CTkFrame(log_head, fg_color="transparent")
        log_btns.grid(row=0, column=1, sticky="e")

        self.btn_copy_logs = ctk.CTkButton(
            log_btns,
            text="📋 Copiar",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            width=82,
            height=26,
            command=self._copy_logs,
        )
        self.btn_copy_logs.pack(side="left", padx=(0, 6))

        self.btn_clear_logs = ctk.CTkButton(
            log_btns,
            text="🗑️ Limpar",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            width=76,
            height=26,
            command=lambda: self.txt_logs.delete("1.0", "end"),
        )
        self.btn_clear_logs.pack(side="left", padx=(0, 6))

        self.btn_toggle_logs = ctk.CTkButton(
            log_btns,
            text="Expandir Logs ▾",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=BTN_NEUTRAL,
            hover_color=BTN_NEUTRAL_HOVER,
            text_color=THEME_TEXT_PRIMARY,
            width=120,
            height=26,
            command=self._toggle_logs_drawer,
        )
        self.btn_toggle_logs.pack(side="left")

        self.txt_logs = ctk.CTkTextbox(
            log_card,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=THEME_INPUT,
            text_color=COLOR_PRIMARY,
            border_color=THEME_BORDER,
            border_width=1,
            corner_radius=8,
            height=170,
            wrap="word",
        )
        # Mantido oculto inicialmente para preservar interface limpa

    # ---------------- NAVEGAÇÃO, TEMA E ATUALIZAÇÕES ----------------
    def _on_nav_change(self, tab_name: str) -> None:
        self._switch_tab(tab_name)

    def _switch_tab(self, tab_name: str) -> None:
        if tab_name in self.pages:
            self.nav_tabs.set(tab_name)
            self.pages[tab_name].tkraise()
            if tab_name == self.TAB_HIST:
                self._refresh_history_tab()
            elif tab_name == self.TAB_CFG:
                self._refresh_usage_panel()
                self._refresh_voiceprint_summary()
                self._refresh_offline_status()

    def _toggle_theme(self) -> None:
        self.theme_mode = "Light" if self.theme_mode == "Dark" else "Dark"
        ctk.set_appearance_mode(self.theme_mode)
        self.btn_theme_toggle.configure(text="☀️ Claro" if self.theme_mode == "Dark" else "🌙 Escuro")
        if self.mascot:
            self.mascot.set_theme(self.theme_mode)
        self._save_theme_preference()

    def _toggle_logs_drawer(self) -> None:
        self._logs_expanded = not self._logs_expanded
        if self._logs_expanded:
            self.txt_logs.grid(row=1, column=0, padx=14, pady=(0, 14), sticky="ew")
            self.btn_toggle_logs.configure(text="Recolher Logs ▴")
        else:
            self.txt_logs.grid_forget()
            self.btn_toggle_logs.configure(text="Expandir Logs ▾")

    def _on_capture_mode_change(self, mode_val: str) -> None:
        is_presencial = mode_val == self.MODE_PRESENCIAL
        if is_presencial:
            self.vu_zoom_row.pack_forget()
        else:
            if not self.vu_zoom_row.winfo_ismapped():
                self.vu_zoom_row.pack(anchor="e", pady=2)
        self._update_hw_summary_label()

    def _on_loopback_selected(self) -> None:
        sel = self.opt_loopback.get()
        if self.loopback_map.get(sel) is None:
            self.seg_capture_mode.set(self.MODE_PRESENCIAL)
            self._on_capture_mode_change(self.MODE_PRESENCIAL)
        else:
            self._update_hw_summary_label()

    def _update_hw_summary_label(self) -> None:
        mode = self.seg_capture_mode.get()
        mic_name = self.opt_mic.get()
        loop_name = self.opt_loopback.get()
        block_txt = self.opt_block_duration.get()
        eng_txt = "Offline Whisper" if self.engine_mode == "offline" else "Gemini 2.5"
        profs = self.gemini_service.voiceprint_service.list_profiles() if self.gemini_service.voiceprint_service else []
        vp_txt = f"{len(profs)} voiceprint(s)" if profs else "sem voiceprints"

        if mode == self.MODE_PRESENCIAL:
            dev_txt = f"Microfone ({mic_name[:28]})"
        else:
            dev_txt = f"Sistema + Mic ({mic_name[:22]})"

        self.lbl_hw_summary.configure(text=f"Captação: {dev_txt} • {eng_txt} • Blocos de {block_txt} • {vp_txt}")

    def _on_engine_mode_change(self, val: str) -> None:
        self.engine_mode = "offline" if "Offline" in val else "online"
        self.gemini_service.engine_mode = self.engine_mode
        self._save_ui_settings()
        self._update_hw_summary_label()
        self._log_gui(f"[MOTOR] Alterado para modo {'100% Offline (Whisper ONNX)' if self.engine_mode == 'offline' else 'Online (Gemini 2.5)'}.")

    def _on_whisper_variant_change(self, val: str) -> None:
        self.offline_whisper_variant = "base" if val == "base" else "small"
        self.gemini_service.offline_whisper_variant = self.offline_whisper_variant
        self._save_ui_settings()
        self._refresh_offline_status()

    def _on_toggle_auto_sqlite(self) -> None:
        self.auto_export_sqlite = bool(self.chk_auto_sqlite.get())
        self.gemini_service.auto_export_sqlite = self.auto_export_sqlite
        self._save_ui_settings()

    def _refresh_offline_status(self) -> None:
        if not hasattr(self, "lbl_offline_status"):
            return
        ready, _ = self.gemini_service.offline_asr_service.is_ready(self.offline_whisper_variant)
        w_txt = f"✅ Whisper {self.offline_whisper_variant} pronto" if ready else f"⚠️ Whisper {self.offline_whisper_variant} não baixado"
        self.lbl_offline_status.configure(text=f"{w_txt} • Verificando Ollama...")

        def _probe_ollama():
            ok_ol, models = list_ollama_models()
            if ok_ol and models:
                ol_txt = f"🟢 Ollama ({models[0]})"
            elif ok_ol:
                ol_txt = "🟡 Ollama sem modelos"
            else:
                ol_txt = "⚪ Ata via Síntese Local"
            self.after(0, lambda: self.lbl_offline_status.configure(text=f"{w_txt} • {ol_txt}"))

        threading.Thread(target=_probe_ollama, daemon=True, name="OllamaProbe").start()

    def _download_offline_models_async(self) -> None:
        self.btn_download_whisper.configure(state="disabled", text="⏳ Baixando...")
        self.bar_offline_progress.set(0.02)
        self.bar_offline_progress.pack(fill="x", pady=(0, 8))

        def _on_prog(msg_txt: str, pct: float) -> None:
            def _ui():
                self.lbl_offline_status.configure(text=f"{msg_txt} ({int(pct * 100)}%)")
                self.bar_offline_progress.set(max(0.0, min(1.0, float(pct))))
            self.after(0, _ui)

        def _worker():
            ok, msg = self.gemini_service.offline_asr_service.ensure_models_ready(
                variant=self.offline_whisper_variant,
                progress_cb=_on_prog,
            )
            def _done():
                self.btn_download_whisper.configure(state="normal", text="⬇️ Baixar / Verificar Offline")
                self.bar_offline_progress.pack_forget()
                self._log_gui(f"[OFFLINE] {msg}")
                self._refresh_offline_status()
            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True, name="OfflineModelDownloader").start()

    def _refresh_usage_panel(self) -> None:
        s = self.gemini_service.get_usage_summary()
        self.lbl_usage_req.configure(
            text=f"Requisições hoje: {s['today_requests']} / {s['free_limit']} ({s['daily_percent']:.1f}%)"
        )
        self.bar_usage_quota.set(min(1.0, max(0.0, s["daily_percent"] / 100.0)))
        self.lbl_usage_cost.configure(
            text=(
                f"Reuniões: {s['total_meetings']} • Áudio: {s['audio_time_str']} • "
                f"Custo ref.: US$ {s['cost_usd']:.4f} (R$ {s['cost_brl']:.2f})"
            )
        )

    def _refresh_voiceprint_summary(self) -> None:
        vp = self.gemini_service.voiceprint_service
        profs = vp.list_profiles()
        names = ", ".join(p["name"] for p in profs[:4])
        if len(profs) > 4:
            names += f" (+{len(profs) - 4})"
        model_ok = "✅ Modelos ONNX prontos" if vp.are_models_ready() else "⚠️ Modelos ONNX pendentes (~26 MB)"
        prof_txt = f"{len(profs)} perfil(is) ativo(s): {names}" if profs else "Nenhum perfil cadastrado"
        self.lbl_vp_summary.configure(text=f"{prof_txt} • {model_ok}")
        self._update_hw_summary_label()

    def _refresh_history_tab(self) -> None:
        query = self.entry_hist_search.get() if hasattr(self, "entry_hist_search") else ""
        render_history_cards_into(
            self.hist_scroll,
            filter_mode=self.seg_hist_filter.get(),
            on_view_md=self.load_document,
            on_retranscribe_wav=lambda p: RetranscribeModal(self, p),
            on_open_archive=self.load_archive,
            on_play_archive=self.play_archive_audio,
            on_retranscribe_archive=self.retranscribe_archive,
            search_query=query,
            on_empty_action=lambda: self._switch_tab(self.TAB_REC),
        )

    def _browse_external_history_file(self) -> None:
        atas_root = get_base_dir() / "atas_reunioes"
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Selecione um Pacote (.escutai / .omniscribe), Ata (.md) ou Gravação (.wav)",
            initialdir=str(atas_root) if atas_root.exists() else str(get_base_dir()),
            filetypes=[
                ("Reuniões Escutaí", "*.escutai *.omniscribe *.md *.wav"),
                ("Pacotes SQLite", "*.escutai *.omniscribe"),
                ("Gravações WAV", "*.wav"),
                ("Documentos Markdown", "*.md"),
                ("Todos os Arquivos", "*.*"),
            ],
        )
        if chosen:
            chosen_path = Path(chosen)
            ext = chosen_path.suffix.lower()
            if ext in (".escutai", ".omniscribe"):
                self.import_archive_file(chosen_path)
            elif ext == ".wav":
                RetranscribeModal(self, chosen_path)
            else:
                self.load_document(chosen_path)

    def _import_external_archive_dialog(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Importar pacote portátil .escutai / .omniscribe",
            filetypes=[("Pacotes Escutaí SQLite", "*.escutai *.omniscribe"), ("Todos os Arquivos", "*.*")],
        )
        if chosen:
            self.import_archive_file(Path(chosen))

    def import_archive_file(self, archive_path: Path) -> None:
        try:
            now = datetime.now()
            dest_dir = get_base_dir() / "atas_reunioes" / now.strftime("%Y") / now.strftime("%m")
            data = self.gemini_service.archive_service.import_archive_to_workspace(archive_path, dest_dir)
            imported = data["imported_paths"]["archive"]
            self.load_archive(imported)
            self._refresh_history_tab()
            self._log_gui(f"[SQLITE] Pacote importado para o histórico: {imported.name}")
        except Exception as e:
            self._log_gui(f"[ERRO] Falha ao importar pacote .escutai: {e}")

    def load_archive(self, archive_path: Path) -> None:
        try:
            data = self.gemini_service.archive_service.load_archive_contents(archive_path)
            self.txt_transcription.delete("1.0", "end")
            self.txt_transcription.insert("end", data.get("transcript_md", "") or "*[Transcrição vazia no pacote]*")
            self.txt_minutes.delete("1.0", "end")
            self.txt_minutes.insert("end", data.get("minutes_md", "") or "*[Ata não incluída no pacote]*")
            self.lbl_ata_meta.configure(
                text=f"Pacote SQLite: {Path(archive_path).name} ({data.get('segment_count', 0)} turnos)"
            )
            self.lbl_status.configure(text=f"● Pacote: {data.get('title', '')[:22]}", text_color="#7c3aed")
            self._switch_tab(self.TAB_ATA if data.get("minutes_md") else self.TAB_REC)
            self._log_gui(f"[SQLITE] Pacote carregado: {Path(archive_path).name}")
        except Exception as e:
            self._log_gui(f"[ERRO] Falha ao abrir pacote .escutai: {e}")

    def play_archive_audio(self, archive_path: Path) -> None:
        try:
            out_wav = self.temp_dir / f"play_{Path(archive_path).stem}.wav"
            wav_p = self.gemini_service.archive_service.extract_wav(archive_path, out_wav)
            if wav_p and wav_p.exists():
                open_file_in_os(wav_p)
            else:
                self._log_gui(f"[AVISO] O pacote {Path(archive_path).name} não contém áudio embutido.")
        except Exception as e:
            self._log_gui(f"[ERRO] Falha ao reproduzir áudio do pacote: {e}")

    def retranscribe_archive(self, archive_path: Path) -> None:
        try:
            out_wav = self.temp_dir / f"retrans_{Path(archive_path).stem}.wav"
            wav_p = self.gemini_service.archive_service.extract_wav(archive_path, out_wav)
            if wav_p and wav_p.exists():
                RetranscribeModal(self, wav_p)
            else:
                self._log_gui(f"[AVISO] O pacote {Path(archive_path).name} não contém faixa de áudio para re-transcrever.")
        except Exception as e:
            self._log_gui(f"[ERRO] Falha ao extrair áudio do pacote: {e}")

    def _export_current_archive(self) -> None:
        trans_txt = self.txt_transcription.get("1.0", "end-1c").strip()
        min_txt = self.txt_minutes.get("1.0", "end-1c").strip()
        if not trans_txt and not min_txt:
            self._log_gui("[AVISO] Nenhuma transcrição ou ata carregada para exportar em .escutai.")
            return
        title = self.entry_meeting_title.get().strip() or self.gemini_service.meeting_title or "Reuniao"
        default_name = f"reuniao_{datetime.now().strftime('%Y%m%d_%H%M')}.escutai"
        dest = filedialog.asksaveasfilename(
            parent=self,
            title="Exportar Pacote Portátil .escutai",
            defaultextension=".escutai",
            initialfile=default_name,
            filetypes=[("Pacote Escutaí SQLite", "*.escutai")],
        )
        if not dest:
            return
        try:
            wav_src = self.gemini_service.audio_file_path
            summary = self.gemini_service.archive_service.export_meeting_package(
                output_path=Path(dest),
                title=title,
                engine_mode=self.engine_mode,
                context_notes=self.entry_context_notes.get().strip(),
                transcript_md=trans_txt,
                minutes_md=min_txt,
                wav_path=wav_src if (wav_src and Path(wav_src).exists()) else None,
            )
            mb = summary["size_bytes"] / (1024 * 1024)
            self._log_gui(f"[SQLITE] Pacote exportado com sucesso: {Path(dest).name} ({mb:.2f} MB)")
        except Exception as e:
            self._log_gui(f"[ERRO] Falha ao exportar .escutai: {e}")

    def _handle_archive_generated(self, archive_path: Path, summary: Dict) -> None:
        def _notify():
            mb = summary.get("size_bytes", 0) / (1024 * 1024)
            self._log_gui(f"[HISTÓRICO] Pacote portátil disponível: {archive_path.name} ({mb:.2f} MB)")
        self.after(0, _notify)

    def _toggle_cfg_key_visibility(self) -> None:
        self._api_key_visible = not self._api_key_visible
        self.entry_cfg_api_key.configure(show="" if self._api_key_visible else "•")

    def _save_cfg_api_key(self) -> None:
        key = self.entry_cfg_api_key.get().strip()
        if not key:
            self._log_gui("[AVISO] Informe uma GEMINI_API_KEY válida antes de salvar.")
            return
        self._on_api_key_saved(key)

    def _copy_transcription(self) -> None:
        content = self.txt_transcription.get("1.0", "end-1c").strip()
        if content:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.update()
            self.btn_copy_trans.configure(text="✅ Copiado!", fg_color=COLOR_SUCCESS)
            self.after(1800, lambda: self.btn_copy_trans.configure(text="📋 Copiar Texto", fg_color=BTN_NEUTRAL))

    def _copy_minutes(self) -> None:
        content = self.txt_minutes.get("1.0", "end-1c").strip()
        if content:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.update()
            self.btn_copy_min.configure(text="✅ Copiado!", fg_color=COLOR_SUCCESS)
            self.after(1800, lambda: self.btn_copy_min.configure(text="📋 Copiar Ata Markdown", fg_color=COLOR_PRIMARY))

    def _copy_logs(self) -> None:
        content = self.txt_logs.get("1.0", "end-1c").strip()
        if content:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.update()
            self.btn_copy_logs.configure(text="✅ Copiado!", fg_color=COLOR_SUCCESS)
            self.after(1800, lambda: self.btn_copy_logs.configure(text="📋 Copiar", fg_color=BTN_NEUTRAL))

    # ---------------- MÉTODOS DE HARDWARE E DISPOSITIVOS ----------------
    def _refresh_audio_devices(self) -> None:
        """Escaneia os dispositivos WASAPI e atualiza os seletores da interface."""
        if self.is_recording:
            return

        self._log_gui("Atualizando dispositivos de áudio WASAPI...")
        loop_devices, mic_devices = listar_dispositivos_audio()

        self.loopback_map.clear()
        loop_options = ["Nenhum (Apenas Microfone / Ditado)"]
        self.loopback_map["Nenhum (Apenas Microfone / Ditado)"] = None
        default_loop_name = None

        for d in loop_devices:
            display_name = f"[{d['index']}] {d['name'][:46]}"
            self.loopback_map[display_name] = d["index"]
            loop_options.append(display_name)
            if d.get("is_default") and default_loop_name is None:
                default_loop_name = display_name

        self.opt_loopback.configure(values=loop_options)
        self.opt_loopback.set(default_loop_name or (loop_options[1] if len(loop_options) > 1 else loop_options[0]))

        self.mic_map.clear()
        mic_options = ["Nenhum (Apenas Áudio do Sistema)"]
        self.mic_map["Nenhum (Apenas Áudio do Sistema)"] = None
        default_mic_name = None

        for d in mic_devices:
            display_name = f"[{d['index']}] {d['name'][:46]}"
            self.mic_map[display_name] = d["index"]
            mic_options.append(display_name)
            if d.get("is_default") and default_mic_name is None:
                default_mic_name = display_name

        self.opt_mic.configure(values=mic_options)
        self.opt_mic.set(default_mic_name or mic_options[0])

        self._update_hw_summary_label()
        self._log_gui(f"Dispositivos detectados: {len(loop_devices)} loopback(s), {len(mic_devices)} microfone(s).")

    def _init_mascot(self) -> None:
        """Inicializa o mascote 3D Vozito atualizado após a abertura da janela principal."""
        if self.mascot is not None:
            return
        self.mascot = VozitoWidget(self, theme_mode=self.theme_mode)
        self.mascot.place_default()

    def _toggle_mascot(self) -> None:
        if self.mascot is None:
            self._init_mascot()
        else:
            self.mascot.toggle_mascot()

    # ---------------- GERENCIAMENTO DA CHAVE API & MODAIS ----------------
    def _check_initial_api_key(self) -> None:
        if self.engine_mode == "offline":
            return
        key = load_api_key_from_env()
        if not key:
            if self.mascot:
                self.mascot.trigger_warning("Chave Gemini não configurada! Cole sua API Key ou use o modo Offline.")
            self._open_api_key_dialog()

    def _open_api_key_dialog(self) -> None:
        ApiKeyModal(self, on_save_callback=self._on_api_key_saved)

    def _open_usage_modal(self) -> None:
        UsageModal(self)

    def _open_voiceprints_modal(self) -> None:
        modal = VoiceProfilesModal(self)
        modal.bind("<Destroy>", lambda _e: self.after(50, self._refresh_voiceprint_summary))

    def _open_history_modal(self) -> None:
        HistoryModal(self)

    def _on_api_key_saved(self, new_key: str) -> None:
        success = self.gemini_service.update_api_key(new_key)
        if hasattr(self, "entry_cfg_api_key"):
            self.entry_cfg_api_key.delete(0, "end")
            self.entry_cfg_api_key.insert(0, new_key.strip())
        if success:
            self._log_gui("[OK] GEMINI_API_KEY atualizada com sucesso.")
            self.lbl_status.configure(text="● API configurada", text_color=COLOR_SUCCESS)
        else:
            self._log_gui("[AVISO] Chave salva no .env, mas validação com Gemini falhou.")

    # ---------------- CONTROLE DE GRAVAÇÃO ----------------
    def _set_session_controls_state(self, idle: bool, recording: bool = False) -> None:
        """Alterna controles durante gravação/re-transcrição mantendo botão único sincronizado."""
        field_state = "normal" if idle else "disabled"
        self.opt_loopback.configure(state=field_state)
        self.opt_mic.configure(state=field_state)
        self.btn_refresh_devices.configure(state=field_state)
        self.opt_block_duration.configure(state=field_state)
        self.entry_meeting_title.configure(state=field_state)
        self.entry_context_notes.configure(state=field_state)
        self.seg_capture_mode.configure(state=field_state)
        self.seg_engine_mode.configure(state=field_state)

        if recording:
            self.btn_rec_action.configure(
                text="⏹ Encerrar e Gerar Ata",
                fg_color=COLOR_DANGER,
                hover_color="#dc2626",
                state="normal",
            )
            self.bar_async_progress.stop()
            self.bar_async_progress.grid_forget()
        elif not idle:
            self.btn_rec_action.configure(
                text="🔄 Processando...",
                fg_color=COLOR_PRIMARY,
                state="disabled",
            )
            self.bar_async_progress.grid(row=3, column=0, columnspan=2, padx=16, pady=(0, 10), sticky="ew")
            self.bar_async_progress.start()
        else:
            self.btn_rec_action.configure(
                text="▶ Iniciar Gravação",
                fg_color=COLOR_SUCCESS,
                hover_color="#059669",
                state="normal",
            )
            self.bar_async_progress.stop()
            self.bar_async_progress.grid_forget()

    def _toggle_recording_action(self) -> None:
        if self.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        """Inicia a gravação da reunião e o pipeline de transcrição."""
        if self.is_recording or self.is_retranscribing:
            self._log_gui("[AVISO] Já existe uma gravação ou re-transcrição em andamento.")
            return

        active_engine = "offline" if "Offline" in self.seg_engine_mode.get() else "online"
        self.engine_mode = active_engine
        if active_engine == "online" and not self.gemini_service.api_key:
            self._open_api_key_dialog()
            return

        selected_mic_str = self.opt_mic.get()
        mic_index = self.mic_map.get(selected_mic_str)

        if self.seg_capture_mode.get() == self.MODE_PRESENCIAL:
            selected_loop_str = "Desativado (Modo Presencial)"
            loop_index = None
        else:
            selected_loop_str = self.opt_loopback.get()
            loop_index = self.loopback_map.get(selected_loop_str)

        if loop_index is None and mic_index is None:
            self._log_gui("[ERRO] Selecione ao menos um dispositivo válido em Config & Voz.")
            self._switch_tab(self.TAB_CFG)
            return

        block_dur_str = self.opt_block_duration.get()
        block_seconds = 60 if "1" in block_dur_str else (300 if "5" in block_dur_str else 180)

        meeting_title = self.entry_meeting_title.get().strip() or "Reunião de Alinhamento"
        context_notes = self.entry_context_notes.get().strip()
        self.gemini_service.start_session(meeting_title, context_notes=context_notes, engine_mode=active_engine)
        session_audio_path = self.gemini_service.get_session_audio_path()

        started = self.audio_engine.start_recording(
            loopback_index=loop_index,
            mic_index=mic_index,
            block_duration_seconds=block_seconds,
            session_wav_path=session_audio_path,
        )

        if not started:
            self._log_gui("[ERRO] Não foi possível iniciar a captura de áudio.")
            return

        self.is_recording = True
        self.recording_start_time = time.time()
        self.block_count = 0
        self._set_session_controls_state(idle=False, recording=True)

        self.lbl_status.configure(text="● Gravando...", text_color=COLOR_DANGER)
        self.txt_transcription.delete("1.0", "end")
        self.txt_transcription.insert("end", f"=== Início da Gravação: {meeting_title} ===\n\n")

        self._timer_running = True
        self._update_timer()

        if self.mascot:
            self.mascot.start_listening()

        self._log_gui(f"[GRAVAÇÃO] Iniciada: Bloco={block_seconds}s, Loopback={selected_loop_str}, Mic={selected_mic_str}")
        if session_audio_path:
            self._log_gui(f"[ÁUDIO] Gravação oficial será salva em: {session_audio_path.name}")

    def _stop_recording(self) -> None:
        if not self.is_recording:
            return

        self.is_recording = False
        self._timer_running = False
        self._set_session_controls_state(idle=False, recording=False)
        self.lbl_status.configure(text="● Encerrando e consolidando...", text_color="#f59e0b")

        if self.mascot:
            self.mascot.stop_listening()
            self.mascot.trigger_transcribing(4.0, "Consolidando transcrição e gerando Ata...")

        threading.Thread(target=self._async_stop_recording, daemon=True, name="AsyncStopRecording").start()

    def _async_stop_recording(self) -> None:
        try:
            self._log_gui("[GRAVAÇÃO] Encerrando motor de áudio e descarregando buffers...")
            self.audio_engine.stop_recording(finalize_block=True)

            saved_audio = self.audio_engine.session_wav_path
            if saved_audio and saved_audio.exists():
                self._log_gui(f"[ÁUDIO] Gravação oficial preservada em: {saved_audio.name}")

            self._log_gui("[GRAVAÇÃO] Finalizando fila do Gemini e gerando Ata...")
            self.gemini_service.finish_session()

            self.after(0, lambda: self._set_session_controls_state(idle=True))
            self._log_gui("[GRAVAÇÃO] Gravação finalizada com sucesso.")
        except Exception as e:
            logger.exception(f"Erro ao encerrar gravação: {e}")
            self._log_gui(f"[ERRO] Falha no encerramento da gravação: {e}")
            self.after(0, lambda: self._set_session_controls_state(idle=True))

    # ---------------- RE-TRANSCRIÇÃO DE ÁUDIO SALVO ----------------
    def start_retranscription(
        self,
        wav_path: Path,
        title: str,
        context_notes: str = "",
        block_seconds: int = 60,
        apply_enhancement: Optional[bool] = None,
        engine_mode: Optional[str] = None,
    ) -> None:
        if self.is_recording or self.is_retranscribing:
            self._log_gui("[AVISO] Aguarde a conclusão da gravação ou re-transcrição atual.")
            return

        target_engine = engine_mode if engine_mode in ("online", "offline") else self.engine_mode
        if target_engine == "online" and not self.gemini_service.api_key:
            self._open_api_key_dialog()
            return

        wav_path = Path(wav_path).resolve()
        if not wav_path.exists():
            self._log_gui(f"[ERRO] Arquivo de áudio não encontrado: {wav_path}")
            return

        self.is_retranscribing = True
        self.block_count = 0
        self._set_session_controls_state(idle=False, recording=False)

        eng_badge = "Offline Whisper" if target_engine == "offline" else "Online Gemini"
        self.lbl_status.configure(text=f"🔄 Re-transcrevendo ({eng_badge})...", text_color=COLOR_PRIMARY)
        self.txt_transcription.delete("1.0", "end")
        self.txt_transcription.insert(
            "end",
            f"=== Re-transcrição de Áudio Salvo ({eng_badge}): {title} ===\n"
            f"Arquivo fonte: {wav_path.name}\n\n",
        )
        self._switch_tab(self.TAB_REC)

        if self.mascot:
            self.mascot.trigger_transcribing(4.0, "Re-transcrevendo gravação salva...")

        threading.Thread(
            target=self._async_retranscribe_worker,
            args=(wav_path, title, context_notes, block_seconds, apply_enhancement, target_engine),
            daemon=True,
            name="AsyncRetranscribeWorker",
        ).start()

    def _async_retranscribe_worker(
        self,
        wav_path: Path,
        title: str,
        context_notes: str,
        block_seconds: int,
        apply_enhancement: Optional[bool],
        engine_mode: str = "online",
    ) -> None:
        try:
            self._log_gui(f"[RE-TRANSCRIÇÃO] Iniciando reprocessamento ({engine_mode}): {wav_path.name}")
            transc_path, minute_path = self.gemini_service.start_session(
                title=title,
                context_notes=context_notes,
                source_audio_path=wav_path,
                engine_mode=engine_mode,
            )
            self._log_gui(f"[RE-TRANSCRIÇÃO] Novos arquivos: {transc_path.name} / {minute_path.name}")

            blocks = slice_wav_for_retranscription(
                source_wav_path=wav_path,
                temp_dir=self.temp_dir,
                block_duration_seconds=block_seconds,
                apply_enhancement=apply_enhancement,
            )

            if not blocks:
                self._log_gui("[AVISO] Arquivo WAV vazio ou sem frames válidos para re-transcrever.")
                self.gemini_service.finish_session()
            else:
                self._log_gui(f"[RE-TRANSCRIÇÃO] Áudio dividido em {len(blocks)} bloco(s) com overlap de 2,5s.")
                for b_path, b_idx, b_start, b_end, b_final in blocks:
                    self.gemini_service.enqueue_block(
                        wav_path=b_path,
                        block_index=b_idx,
                        start_seconds=b_start,
                        end_seconds=b_end,
                        is_final=b_final,
                    )
                self.gemini_service.finish_session()

            worker = getattr(self.gemini_service, "_worker_thread", None)
            if worker and worker.is_alive():
                worker.join()

            self._log_gui("[RE-TRANSCRIÇÃO] Re-transcrição e nova Ata Executiva concluídas!")
        except Exception as e:
            logger.exception(f"Erro na re-transcrição de {wav_path}: {e}")
            self._log_gui(f"[ERRO] Falha ao re-transcrever {wav_path.name}: {e}")
        finally:
            self.is_retranscribing = False
            self.after(0, lambda: self._set_session_controls_state(idle=True))

    def _update_timer(self) -> None:
        if self._timer_running:
            elapsed = time.time() - self.recording_start_time
            self.lbl_timer.configure(text=f"⏱ {format_seconds(elapsed)}")
            self.after(1000, self._update_timer)

    # ---------------- CALLBACKS THREAD-SAFE ----------------
    def _handle_rms_update(self, zoom_rms: float, mic_rms: float) -> None:
        def update():
            self.bar_vu_zoom.set(zoom_rms)
            self.bar_vu_mic.set(mic_rms)
            self.lbl_vu_zoom_val.configure(text=f"{int(zoom_rms * 100)}%")
            self.lbl_vu_mic_val.configure(text=f"{int(mic_rms * 100)}%")
            if self.mascot:
                self.mascot.set_audio_rms(max(zoom_rms, mic_rms))

        self.after(0, update)

    def _handle_block_ready(
        self,
        wav_path: Path,
        block_index: int,
        start_seconds: float,
        end_seconds: float,
        is_final: bool,
    ) -> None:
        self.block_count = block_index
        self.gemini_service.enqueue_block(
            wav_path=wav_path,
            block_index=block_index,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            is_final=is_final,
        )

    def _handle_service_log(self, message: str) -> None:
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
        def update():
            self.txt_transcription.insert("end", block_text)
            self.txt_transcription.see("end")
            if self.mascot and not is_final:
                self.mascot.trigger_transcribing(2.2)

        self.after(0, update)

    def _handle_minute_generated(self, minute_path: Path, minute_text: str) -> None:
        def update():
            self.txt_minutes.delete("1.0", "end")
            self.txt_minutes.insert("end", minute_text)
            self.lbl_ata_meta.configure(text=f"Arquivo salvo: {minute_path.name}")
            self._switch_tab(self.TAB_ATA)
            self.lbl_status.configure(text="● Ata Concluída!", text_color=COLOR_SUCCESS)
            self._refresh_usage_panel()
            self._log_gui(f"[CONCLUÍDO] Ata Executiva disponível em: {minute_path.name}")
            if self.mascot:
                self.mascot.trigger_celebrating("Ata executiva pronta e salva! 🏆")

        self.after(0, update)

    def _handle_status_change(self, status: str) -> None:
        def _apply():
            low = status.lower()
            if any(k in low for k in ("erro", "falha")):
                color = COLOR_DANGER
            elif any(k in low for k in ("concluíd", "pronto", "salva")):
                color = COLOR_SUCCESS
            elif any(k in low for k in ("baixando", "compactando", "refinando")):
                color = COLOR_AMBER
            else:
                color = COLOR_PRIMARY
            self.lbl_status.configure(text=f"● {status}", text_color=color)

        self.after(0, _apply)

    def _log_gui(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.txt_logs.insert("end", f"[{timestamp}] {message}\n")
        self.txt_logs.see("end")

    # ---------------- AÇÕES DE INTERFACE ----------------
    def _on_toggle_noise_gate(self) -> None:
        self.audio_engine.set_noise_gate(bool(self.chk_noise_gate.get()))

    def _on_toggle_mic_mute(self) -> None:
        self.audio_engine.set_mic_muted(bool(self.chk_mic_mute.get()))

    def _open_atas_folder(self) -> None:
        now = datetime.now()
        target_dir = get_base_dir() / "atas_reunioes" / now.strftime("%Y") / now.strftime("%m")
        open_folder_in_explorer(target_dir)

    def load_document(self, file_path: Path) -> None:
        """Carrega e exibe um arquivo de Ata ou Transcrição nas abas da interface."""
        file_path = Path(file_path).resolve()
        if not file_path.exists():
            return

        try:
            content = file_path.read_text(encoding="utf-8")
            filename = file_path.name

            if "ata_executiva" in filename:
                self.txt_minutes.delete("1.0", "end")
                self.txt_minutes.insert("end", content)
                self.lbl_ata_meta.configure(text=f"Visualizando: {filename}")
                self._switch_tab(self.TAB_ATA)
                self.lbl_status.configure(text=f"● Ata: {filename[:24]}...", text_color=COLOR_SUCCESS)

                matching_file = file_path.parent / filename.replace("ata_executiva_", "transcricao_", 1)
                if matching_file.exists():
                    try:
                        self.txt_transcription.delete("1.0", "end")
                        self.txt_transcription.insert("end", matching_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass

            elif "transcricao" in filename:
                self.txt_transcription.delete("1.0", "end")
                self.txt_transcription.insert("end", content)
                self._switch_tab(self.TAB_REC)
                self.lbl_status.configure(text=f"● Transcrição: {filename[:22]}...", text_color=COLOR_PRIMARY)

                matching_file = file_path.parent / filename.replace("transcricao_", "ata_executiva_", 1)
                if matching_file.exists():
                    try:
                        self.txt_minutes.delete("1.0", "end")
                        self.txt_minutes.insert("end", matching_file.read_text(encoding="utf-8"))
                        self.lbl_ata_meta.configure(text=f"Visualizando: {matching_file.name}")
                    except Exception:
                        pass
            else:
                self.txt_transcription.delete("1.0", "end")
                self.txt_transcription.insert("end", content)
                self._switch_tab(self.TAB_REC)

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

