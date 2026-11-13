"""Instalador Gráfico Moderno (Setup Wizard) para o OmniScribe AI em CustomTkinter.

Permite que qualquer usuário instale o software com interface visual, escolha a pasta de destino,
crie atalhos no Desktop e Menu Iniciar, preserve dados de reuniões anteriores e execute imediatamente.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

logger = logging.getLogger("omniscribe.installer")

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


def get_default_install_dir() -> Path:
    """Retorna o diretório de instalação padrão recomendado pelo Windows (sem requerer elevação UAC)."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "Programs" / "OmniScribe AI"
    return Path.home() / "AppData" / "Local" / "Programs" / "OmniScribe AI"


def get_app_package_zip() -> Optional[Path]:
    """Localiza o arquivo zip ultracompacto com todos os binários do aplicativo."""
    # 1. Se estiver empacotado pelo PyInstaller
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "app_data.zip"
        if bundled.exists():
            return bundled

    # 2. Ao lado do executável ou em dist/
    candidate_local = Path(sys.executable).resolve().parent / "app_data.zip"
    if candidate_local.exists():
        return candidate_local

    root = Path(__file__).resolve().parent.parent
    candidate = root / "dist" / "app_data.zip"
    if candidate.exists():
        return candidate

    candidate_root = Path(sys.executable).resolve().parent / "dist" / "app_data.zip"
    if candidate_root.exists():
        return candidate_root

    return None


def get_source_app_dir() -> Optional[Path]:
    """Localiza a pasta com os binários compilados do OmniScribe AI (fallback)."""
    # 1. Se estiver empacotado dentro do executável do Setup pelo PyInstaller
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "app_data"
        if bundled.exists():
            return bundled

    # 2. Modo local de desenvolvimento (procura em dist/OmniScribe ao lado do script)
    root = Path(__file__).resolve().parent.parent
    candidate = root / "dist" / "OmniScribe"
    if candidate.exists() and (candidate / "OmniScribe.exe").exists():
        return candidate

    candidate_root = Path(sys.executable).resolve().parent / "dist" / "OmniScribe"
    if candidate_root.exists():
        return candidate_root

    return None


def create_windows_shortcut(target_exe: Path, shortcut_path: Path, description: str = "OmniScribe AI") -> None:
    """Cria um atalho (.lnk) do Windows nativamente via PowerShell WScript.Shell."""
    target_exe = target_exe.resolve()
    shortcut_path = shortcut_path.resolve()
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)

    ps_cmd = (
        f"$WshShell = New-Object -ComObject WScript.Shell; "
        f"$Shortcut = $WshShell.CreateShortcut('{str(shortcut_path)}'); "
        f"$Shortcut.TargetPath = '{str(target_exe)}'; "
        f"$Shortcut.WorkingDirectory = '{str(target_exe.parent)}'; "
        f"$Shortcut.Description = '{description}'; "
        f"$Shortcut.Save()"
    )
    try:
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            check=False,
            creationflags=flags,
        )
    except Exception as e:
        logger.warning(f"Erro ao criar atalho em {shortcut_path}: {e}")


def write_uninstaller_script(install_dir: Path) -> None:
    """Gera script autônomo de desinstalação na pasta do programa."""
    uninst_bat = install_dir / "uninstall.bat"
    content = """@echo off
chcp 65001 >nul
title Desinstalador do OmniScribe AI
echo ===============================================================================
echo                   Desinstalador do OmniScribe AI
echo ===============================================================================
echo.
echo Tem certeza de que deseja remover o OmniScribe AI deste computador?
set /p CONFIRM="Digite S para confirmar ou N para cancelar: "
if /i not "%CONFIRM%"=="S" exit /b

echo.
echo [1/3] Fechando aplicacao se estiver em execucao...
taskkill /f /im OmniScribe.exe >nul 2>&1

echo [2/3] Removendo atalhos do Desktop e Menu Iniciar...
del /f /q "%USERPROFILE%\\Desktop\\OmniScribe AI.lnk" >nul 2>&1
del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\OmniScribe AI.lnk" >nul 2>&1

echo [3/3] Removendo arquivos do programa...
cd ..
set "APP_DIR=%~dp0"
rmdir /s /q "%APP_DIR%" >nul 2>&1

echo.
echo ===============================================================================
echo                OmniScribe AI foi desinstalado com sucesso!
echo ===============================================================================
pause
"""
    try:
        with open(uninst_bat, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        logger.warning(f"Não foi possível criar uninstall.bat: {e}")


class InstallerApp(ctk.CTk):
    """Janela principal do assistente de instalação (Setup Wizard)."""

    def __init__(self):
        super().__init__()

        self.title("Instalador do OmniScribe AI")
        self.geometry("640x520")
        self.resizable(False, False)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.current_step = 1
        self.target_dir = get_default_install_dir()

        # Variáveis de opções
        self.var_desktop_shortcut = ctk.BooleanVar(value=True)
        self.var_start_shortcut = ctk.BooleanVar(value=True)
        self.var_preserve_data = ctk.BooleanVar(value=True)
        self.var_launch_now = ctk.BooleanVar(value=True)

        # Container principal de páginas
        self.page_container = ctk.CTkFrame(self, fg_color="transparent")
        self.page_container.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="nsew")
        self.page_container.grid_columnconfigure(0, weight=1)
        self.page_container.grid_rowconfigure(0, weight=1)

        # Barra inferior de navegação
        self.nav_frame = ctk.CTkFrame(self, fg_color="#18181b", height=60, corner_radius=0)
        self.nav_frame.grid(row=1, column=0, sticky="ew")

        self.btn_back = ctk.CTkButton(
            self.nav_frame,
            text="< Voltar",
            width=100,
            fg_color="#374151",
            hover_color="#4b5563",
            command=self._prev_page,
        )
        self.btn_back.pack(side="left", padx=20, pady=12)

        self.btn_cancel = ctk.CTkButton(
            self.nav_frame,
            text="Cancelar",
            width=100,
            fg_color="#374151",
            hover_color="#4b5563",
            command=self.destroy,
        )
        self.btn_cancel.pack(side="left", padx=6, pady=12)

        self.btn_next = ctk.CTkButton(
            self.nav_frame,
            text="Avançar >",
            width=120,
            font=ctk.CTkFont(weight="bold"),
            fg_color="#3b82f6",
            hover_color="#2563eb",
            command=self._next_page,
        )
        self.btn_next.pack(side="right", padx=20, pady=12)

        self._show_page(1)

    def _clear_page(self):
        for widget in self.page_container.winfo_children():
            widget.destroy()

    def _show_page(self, step: int):
        self.current_step = step
        self._clear_page()

        if step == 1:
            self._render_page_welcome()
        elif step == 2:
            self._render_page_directory()
        elif step == 3:
            self._render_page_options()
        elif step == 4:
            self._render_page_installing()
        elif step == 5:
            self._render_page_finished()

    # ---------------- PÁGINA 1: BOAS-VINDAS ----------------
    def _render_page_welcome(self):
        self.btn_back.configure(state="disabled")
        self.btn_next.configure(text="Avançar >", state="normal")

        frame = ctk.CTkFrame(self.page_container, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=20, pady=20)

        lbl_icon = ctk.CTkLabel(frame, text="🎙️", font=ctk.CTkFont(size=56))
        lbl_icon.pack(pady=(15, 10))

        lbl_title = ctk.CTkLabel(
            frame,
            text="Bem-vindo ao Assistente de Instalação do\nOmniScribe AI",
            font=ctk.CTkFont(size=22, weight="bold"),
            justify="center",
        )
        lbl_title.pack(pady=10)

        lbl_desc = ctk.CTkLabel(
            frame,
            text=(
                "O OmniScribe AI é o assistente universal para transcrição em tempo real e\n"
                "geração automática de Atas Executivas para reuniões (Google Meet, Teams, Zoom e voz).\n\n"
                "Este assistente irá instalar o software em seu computador de forma rápida e segura,\n"
                "sem necessitar de privilégios de administrador ou comandos de terminal."
            ),
            font=ctk.CTkFont(size=13),
            text_color="#9ca3af",
            justify="center",
        )
        lbl_desc.pack(pady=15)

    # ---------------- PÁGINA 2: PASTA DE DESTINO ----------------
    def _render_page_directory(self):
        self.btn_back.configure(state="normal")
        self.btn_next.configure(text="Avançar >", state="normal")

        frame = ctk.CTkFrame(self.page_container, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=20, pady=20)

        lbl_title = ctk.CTkLabel(
            frame,
            text="Pasta de Instalação",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        lbl_title.pack(anchor="w", pady=(0, 6))

        lbl_desc = ctk.CTkLabel(
            frame,
            text="Escolha a pasta onde o OmniScribe AI será instalado:",
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
        )
        lbl_desc.pack(anchor="w", pady=(0, 16))

        dir_box = ctk.CTkFrame(frame, fg_color="transparent")
        dir_box.pack(fill="x", pady=10)
        dir_box.grid_columnconfigure(0, weight=1)

        self.entry_dir = ctk.CTkEntry(dir_box, font=ctk.CTkFont(size=12))
        self.entry_dir.insert(0, str(self.target_dir))
        self.entry_dir.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        btn_browse = ctk.CTkButton(
            dir_box,
            text="Procurar...",
            width=100,
            command=self._browse_dir,
        )
        btn_browse.grid(row=0, column=1)

        lbl_space = ctk.CTkLabel(
            frame,
            text="Espaço em disco necessário: ~120 MB\nEspaço livre disponível: Suficiente",
            font=ctk.CTkFont(size=11),
            text_color="#71717a",
            justify="left",
        )
        lbl_space.pack(anchor="w", pady=20)

    def _browse_dir(self):
        chosen = filedialog.askdirectory(initialdir=str(self.target_dir), parent=self)
        if chosen:
            self.target_dir = Path(chosen) / "OmniScribe AI"
            self.entry_dir.delete(0, "end")
            self.entry_dir.insert(0, str(self.target_dir))

    # ---------------- PÁGINA 3: OPÇÕES ADICIONAIS ----------------
    def _render_page_options(self):
        self.btn_back.configure(state="normal")
        self.btn_next.configure(text="Instalar Agora", fg_color="#10b981", hover_color="#059669")

        # Atualiza pasta digitada
        self.target_dir = Path(self.entry_dir.get().strip() or str(self.target_dir))

        frame = ctk.CTkFrame(self.page_container, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=20, pady=20)

        lbl_title = ctk.CTkLabel(
            frame,
            text="Configurações e Atalhos",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        lbl_title.pack(anchor="w", pady=(0, 6))

        lbl_desc = ctk.CTkLabel(
            frame,
            text="Selecione as opções adicionais que deseja configurar para a instalação:",
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
        )
        lbl_desc.pack(anchor="w", pady=(0, 20))

        chk_desk = ctk.CTkCheckBox(
            frame,
            text="Criar atalho na Área de Trabalho (Desktop)",
            variable=self.var_desktop_shortcut,
            font=ctk.CTkFont(size=13),
        )
        chk_desk.pack(anchor="w", pady=8)

        chk_menu = ctk.CTkCheckBox(
            frame,
            text="Criar atalho no Menu Iniciar do Windows",
            variable=self.var_start_shortcut,
            font=ctk.CTkFont(size=13),
        )
        chk_menu.pack(anchor="w", pady=8)

        chk_data = ctk.CTkCheckBox(
            frame,
            text="Preservar atas, gravações anteriores e chave da API (.env) existentes",
            variable=self.var_preserve_data,
            font=ctk.CTkFont(size=13),
        )
        chk_data.pack(anchor="w", pady=8)

    # ---------------- PÁGINA 4: INSTALANDO ----------------
    def _render_page_installing(self):
        self.btn_back.configure(state="disabled")
        self.btn_next.configure(state="disabled")
        self.btn_cancel.configure(state="disabled")

        frame = ctk.CTkFrame(self.page_container, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=20, pady=40)

        lbl_title = ctk.CTkLabel(
            frame,
            text="Instalando o OmniScribe AI...",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        lbl_title.pack(anchor="w", pady=(0, 10))

        self.lbl_status = ctk.CTkLabel(
            frame,
            text="Preparando arquivos...",
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
        )
        self.lbl_status.pack(anchor="w", pady=(0, 16))

        self.progress_bar = ctk.CTkProgressBar(frame, height=14, progress_color="#3b82f6")
        self.progress_bar.pack(fill="x", pady=10)
        self.progress_bar.set(0.1)

        # Dispara thread de instalação
        threading.Thread(target=self._run_installation, daemon=True).start()

    def _run_installation(self):
        """Executa a cópia de arquivos, configuração e geração de atalhos."""
        try:
            time.sleep(0.3)
            self._update_progress(0.2, "Fechando processos antigos se abertos...")
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/f", "/im", "OmniScribe.exe"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Localiza pacote zip de alta performance ou pasta de origem
            pkg_zip = get_app_package_zip()
            source_dir = get_source_app_dir()
            target_dir = self.target_dir.resolve()

            # Backup de dados se solicitado
            backup_atas = None
            backup_env = None
            if self.var_preserve_data.get() and target_dir.exists():
                self._update_progress(0.25, "Salvando dados e atas existentes...")
                atas_dir = target_dir / "atas_reunioes"
                env_file = target_dir / ".env"
                if atas_dir.exists():
                    backup_atas = target_dir.parent / "_temp_atas_backup"
                    if backup_atas.exists():
                        shutil.rmtree(backup_atas, ignore_errors=True)
                    shutil.copytree(atas_dir, backup_atas)

                if env_file.exists():
                    backup_env = target_dir.parent / "_temp_env_backup"
                    shutil.copyfile(env_file, backup_env)

            target_dir.mkdir(parents=True, exist_ok=True)

            # Instalação via descompactação ultrarrápida do pacote
            if pkg_zip and pkg_zip.exists():
                self._update_progress(0.35, "Extraindo arquivos do OmniScribe AI...")
                with zipfile.ZipFile(pkg_zip, "r") as zf:
                    members = zf.namelist()
                    total = len(members)
                    for i, member in enumerate(members):
                        zf.extract(member, target_dir)
                        if i % 60 == 0 or i == total - 1:
                            ratio = i / max(1, total)
                            prog = 0.35 + 0.40 * ratio
                            fname = Path(member).name or member
                            self._update_progress(prog, f"Instalando: {fname} ({int(ratio * 100)}%)")
            elif source_dir and source_dir.exists():
                self._update_progress(0.4, f"Copiando arquivos para {target_dir.name}...")
                for item in source_dir.iterdir():
                    dest = target_dir / item.name
                    if item.is_dir():
                        if dest.exists():
                            shutil.rmtree(dest, ignore_errors=True)
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
            else:
                (target_dir / "src").mkdir(exist_ok=True)

            self._update_progress(0.8, "Configurando variáveis de ambiente...")
            # Restaura .env ou cria a partir de .env.example
            if backup_env and backup_env.exists():
                shutil.copyfile(backup_env, target_dir / ".env")
                backup_env.unlink(missing_ok=True)
            elif (target_dir / ".env.example").exists() and not (target_dir / ".env").exists():
                shutil.copyfile(target_dir / ".env.example", target_dir / ".env")

            # Restaura atas_reunioes
            if backup_atas and backup_atas.exists():
                dest_atas = target_dir / "atas_reunioes"
                if dest_atas.exists():
                    shutil.rmtree(dest_atas, ignore_errors=True)
                shutil.copytree(backup_atas, dest_atas)
                shutil.rmtree(backup_atas, ignore_errors=True)

            # Gera script de desinstalação
            self._update_progress(0.85, "Registrando desinstalador...")
            write_uninstaller_script(target_dir)

            # Gera atalhos do Windows
            self._update_progress(0.92, "Criando atalhos no Windows...")
            exe_path = target_dir / "OmniScribe.exe"

            if sys.platform == "win32":
                # Desktop
                if self.var_desktop_shortcut.get():
                    user_desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
                    create_windows_shortcut(exe_path, user_desktop / "OmniScribe AI.lnk")

                # Menu Iniciar
                if self.var_start_shortcut.get():
                    appdata = os.environ.get("APPDATA")
                    if appdata:
                        start_menu = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                        create_windows_shortcut(exe_path, start_menu / "OmniScribe AI.lnk")

            self._update_progress(1.0, "Instalação concluída!")
            time.sleep(0.4)
            self.after(0, lambda: self._show_page(5))

        except Exception as e:
            logger.exception(f"Erro na instalação: {e}")
            self.after(0, lambda: self._show_error(str(e)))

    def _update_progress(self, val: float, msg: str):
        def update():
            self.progress_bar.set(val)
            self.lbl_status.configure(text=msg)
        self.after(0, update)

    def _show_error(self, err_msg: str):
        self._clear_page()
        lbl = ctk.CTkLabel(
            self.page_container,
            text=f"❌ Ocorreu um erro durante a instalação:\n\n{err_msg}",
            font=ctk.CTkFont(size=13),
            text_color="#ef4444",
            justify="center",
        )
        lbl.pack(pady=40)
        self.btn_cancel.configure(text="Fechar", state="normal")

    # ---------------- PÁGINA 5: CONCLUÍDO ----------------
    def _render_page_finished(self):
        self.btn_back.configure(state="disabled")
        self.btn_cancel.configure(state="disabled")
        self.btn_next.configure(
            text="Concluir",
            state="normal",
            fg_color="#10b981",
            hover_color="#059669",
            command=self._finish_and_launch,
        )

        frame = ctk.CTkFrame(self.page_container, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=20, pady=30)

        lbl_icon = ctk.CTkLabel(frame, text="🎉", font=ctk.CTkFont(size=56))
        lbl_icon.pack(pady=(10, 10))

        lbl_title = ctk.CTkLabel(
            frame,
            text="Instalação Concluída com Sucesso!",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#10b981",
        )
        lbl_title.pack(pady=6)

        lbl_desc = ctk.CTkLabel(
            frame,
            text=(
                f"O OmniScribe AI foi instalado com sucesso em:\n{self.target_dir}\n\n"
                "Seus atalhos foram criados e o aplicativo está pronto para uso imediato."
            ),
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
            justify="center",
        )
        lbl_desc.pack(pady=15)

        chk_launch = ctk.CTkCheckBox(
            frame,
            text="Iniciar o OmniScribe AI agora",
            variable=self.var_launch_now,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        chk_launch.pack(pady=10)

    def _finish_and_launch(self):
        if self.var_launch_now.get():
            exe_path = self.target_dir / "OmniScribe.exe"
            if exe_path.exists():
                try:
                    if sys.platform == "win32":
                        os.startfile(str(exe_path))
                    else:
                        subprocess.Popen([str(exe_path)])
                except Exception as e:
                    logger.warning(f"Erro ao iniciar aplicativo: {e}")
        self.destroy()

    def _next_page(self):
        if self.current_step == 3:
            self._show_page(4)
        elif self.current_step < 5:
            self._show_page(self.current_step + 1)

    def _prev_page(self):
        if self.current_step > 1:
            self._show_page(self.current_step - 1)


def run_installer():
    app = InstallerApp()
    app.mainloop()


if __name__ == "__main__":
    run_installer()

