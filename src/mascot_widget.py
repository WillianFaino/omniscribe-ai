"""Widget interativo do mascote 3D OmniMic no estilo assistente Windows 95/XP."""

from __future__ import annotations

import random
import time
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk
from PIL import ImageTk

from mascot_3d import DISPLAY_H, DISPLAY_W, OmniMic3DRenderer

XP_BALLOON_BG = "#FFFFE1"
XP_BALLOON_BORDER = "#374151"
XP_BALLOON_TEXT = "#111827"
XP_CARD_BG = "#0F172A"
XP_CARD_BORDER = "#334155"


class OmniMicWidget(ctk.CTkFrame):
    """Overlay flutuante e arrastavel com o mascote 3D OmniMic + balao estilo Win95/XP."""

    def __init__(
        self,
        master: ctk.CTk,
        on_visibility_change: Optional[Callable[[bool], None]] = None,
    ) -> None:
        super().__init__(
            master,
            fg_color="transparent",
            border_width=0,
        )
        self._master = master
        self._on_visibility_change = on_visibility_change
        self._renderer = OmniMic3DRenderer()

        self._state = "greeting"
        self._state_until: float = time.monotonic() + 3.2
        self._return_state = "idle"
        self._phase = 0.0
        self._rms = 0.0
        self._look_x = 0.0
        self._look_y = 0.0
        self._last_interaction = time.monotonic()
        self._visible = True
        self._running = True
        self._photo_ref: Optional[ImageTk.PhotoImage] = None

        # Controle de arraste
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._did_drag = False
        self._pos_x: Optional[int] = None
        self._pos_y: Optional[int] = None

        self._build_ui()
        self._build_context_menu()
        self._bind_events()

        self.say(
            "Olá! Eu sou o OmniMic.\nPronto para anotar sua próxima reunião?",
            duration=6.5,
        )
        self._tick()

    def _build_ui(self) -> None:
        # 1. Balao de fala retrô estilo Windows 95/XP (acima do mascote)
        self._balloon = tk.Frame(
            self,
            bg=XP_BALLOON_BG,
            highlightbackground=XP_BALLOON_BORDER,
            highlightthickness=2,
            bd=0,
        )
        balloon_top = tk.Frame(self._balloon, bg=XP_BALLOON_BG)
        balloon_top.pack(fill="x", padx=6, pady=(4, 0))

        lbl_title = tk.Label(
            balloon_top,
            text="OmniMic",
            font=("Segoe UI", 8, "bold"),
            fg="#1D4ED8",
            bg=XP_BALLOON_BG,
        )
        lbl_title.pack(side="left")

        btn_close_balloon = tk.Label(
            balloon_top,
            text="✕",
            font=("Segoe UI", 8, "bold"),
            fg="#6B7280",
            bg=XP_BALLOON_BG,
            cursor="hand2",
        )
        btn_close_balloon.pack(side="right")
        btn_close_balloon.bind("<Button-1>", lambda _e: self.hide_balloon())

        self._lbl_balloon = tk.Label(
            self._balloon,
            text="",
            font=("Segoe UI", 9),
            fg=XP_BALLOON_TEXT,
            bg=XP_BALLOON_BG,
            justify="left",
            wraplength=200,
            padx=8,
            pady=4,
        )
        self._lbl_balloon.pack(fill="x")

        # 2. Container do sprite 3D
        self._stage = tk.Frame(
            self,
            bg=XP_CARD_BG,
            highlightbackground=XP_CARD_BORDER,
            highlightthickness=1,
            width=164,
            height=176,
            cursor="fleur",
        )
        self._stage.pack_propagate(False)
        self._stage.pack(side="bottom", anchor="e", pady=(4, 0))

        # Botao discreto de opcoes no canto superior do card
        self._btn_menu = tk.Label(
            self._stage,
            text="⋮",
            font=("Segoe UI", 10, "bold"),
            fg="#94A3B8",
            bg=XP_CARD_BG,
            cursor="hand2",
            padx=4,
        )
        self._btn_menu.place(relx=1.0, x=-4, y=2, anchor="ne")

        self._lbl_sprite = tk.Label(
            self._stage,
            bg=XP_CARD_BG,
            bd=0,
            cursor="hand2",
        )
        self._lbl_sprite.place(relx=0.5, rely=0.52, anchor="center")

    def _build_context_menu(self) -> None:
        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="👋 Acenar", command=lambda: self.trigger_greeting())
        self._menu.add_command(label="📝 Anotar no Bloquinho", command=lambda: self.trigger_transcribing(3.5))
        self._menu.add_command(label="🏆 Comemorar (Troféu)", command=lambda: self.trigger_celebrating())
        self._menu.add_command(label="🎲 Giro 360°", command=lambda: self.trigger_spin())
        self._menu.add_separator()
        self._menu.add_command(label="🙈 Ocultar OmniMic", command=self.hide_mascot)

    def _bind_events(self) -> None:
        for widget in (self._stage, self._lbl_sprite):
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_move)
            widget.bind("<ButtonRelease-1>", self._on_drag_end)
            widget.bind("<Button-3>", self._on_right_click)
        self._btn_menu.bind("<Button-1>", self._on_right_click)
        self._master.bind("<Motion>", self._on_mouse_move, add="+")

    def _on_mouse_move(self, event: tk.Event) -> None:
        if not self._visible:
            return
        try:
            wx = self.winfo_rootx() + self.winfo_width() * 0.5
            wy = self.winfo_rooty() + self.winfo_height() * 0.65
            dx = (event.x_root - wx) / 260.0
            dy = -(event.y_root - wy) / 260.0
            self._look_x = max(-1.0, min(1.0, dx))
            self._look_y = max(-1.0, min(1.0, dy))
        except Exception:
            pass

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self._did_drag = False
        self._last_interaction = time.monotonic()

    def _on_drag_move(self, event: tk.Event) -> None:
        dx = event.x_root - self._drag_start_x
        dy = event.y_root - self._drag_start_y
        if abs(dx) > 4 or abs(dy) > 4:
            self._did_drag = True
        if not self._did_drag:
            return

        if self._pos_x is None or self._pos_y is None:
            self._pos_x = self.winfo_x()
            self._pos_y = self.winfo_y()

        new_x = max(8, min(self._master.winfo_width() - self.winfo_width() - 8, self._pos_x + dx))
        new_y = max(8, min(self._master.winfo_height() - self.winfo_height() - 48, self._pos_y + dy))
        self._pos_x = new_x
        self._pos_y = new_y
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self.place(x=new_x, y=new_y, anchor="nw")

    def _on_drag_end(self, _event: tk.Event) -> None:
        if not self._did_drag:
            self._on_click_mascot()

    def _on_right_click(self, event: tk.Event) -> None:
        self._last_interaction = time.monotonic()
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _on_click_mascot(self) -> None:
        self._last_interaction = time.monotonic()
        if self._state == "sleeping":
            self.trigger_greeting("Opa! Acordei e já estou ligado!")
            return
        if self._state == "listening":
            self.say("Tô ouvindo tudinho aqui no áudio!", duration=3.5)
            return

        tips = [
            "Dica: você pode me arrastar para qualquer canto da tela!",
            "Dica: clique com o botão direito em mim para animações extras!",
            "Fique de olho no rastreador de cota para não estourar o limite grátis.",
            "Quando terminar a reunião, eu te entrego o troféu com a Ata pronta!",
        ]
        self.trigger_spin(random.choice(tips))

    # ---------------- API publica de estados ----------------

    def place_default(self) -> None:
        if not self._visible:
            return
        if self._pos_x is not None and self._pos_y is not None:
            self.place(x=self._pos_x, y=self._pos_y, anchor="nw")
        else:
            self.place(relx=1.0, rely=1.0, x=-20, y=-52, anchor="se")

    def show_mascot(self) -> None:
        self._visible = True
        self._last_interaction = time.monotonic()
        self.place_default()
        self.trigger_greeting("Voltei! Precisando de ajuda com a gravação?")
        if self._on_visibility_change:
            self._on_visibility_change(True)

    def hide_mascot(self) -> None:
        self._visible = False
        self.place_forget()
        if self._on_visibility_change:
            self._on_visibility_change(False)

    def toggle_mascot(self) -> None:
        if self._visible:
            self.hide_mascot()
        else:
            self.show_mascot()

    def say(self, text: str, duration: float = 5.0) -> None:
        if not self._visible:
            return
        self._lbl_balloon.configure(text=text)
        self._balloon.pack(side="top", anchor="e", fill="x", pady=(0, 2))
        self._balloon_hide_at = time.monotonic() + duration

    def hide_balloon(self) -> None:
        self._balloon.pack_forget()

    def set_audio_rms(self, rms: float) -> None:
        self._rms = rms

    def trigger_greeting(self, message: Optional[str] = None) -> None:
        self._state = "greeting"
        self._return_state = "idle"
        self._state_until = time.monotonic() + 2.6
        if message:
            self.say(message, duration=4.5)

    def start_listening(self) -> None:
        self._last_interaction = time.monotonic()
        self._state = "listening"
        self._return_state = "listening"
        self._state_until = float("inf")
        self.say("Captando áudio ao vivo... pode deixar comigo!", duration=4.0)

    def stop_listening(self) -> None:
        self._rms = 0.0
        self._state = "idle"
        self._return_state = "idle"
        self._state_until = float("inf")

    def trigger_transcribing(self, duration: float = 2.8, message: Optional[str] = None) -> None:
        self._last_interaction = time.monotonic()
        self._state = "transcribing"
        self._state_until = time.monotonic() + duration
        if message:
            self.say(message, duration=duration + 1.0)

    def trigger_celebrating(self, message: str = "Ata executiva pronta e salva! 🏆") -> None:
        self._last_interaction = time.monotonic()
        self._state = "celebrating"
        self._return_state = "idle"
        self._state_until = time.monotonic() + 4.5
        self.say(message, duration=5.5)

    def trigger_warning(self, message: str) -> None:
        self._last_interaction = time.monotonic()
        self._state = "warning"
        self._state_until = time.monotonic() + 4.0
        self.say(message, duration=6.0)

    def trigger_spin(self, message: Optional[str] = None) -> None:
        self._last_interaction = time.monotonic()
        self._state = "spin"
        self._state_until = time.monotonic() + 2.0
        if message:
            self.say(message, duration=5.0)

    # ---------------- Loop de animacao ----------------

    def _tick(self) -> None:
        if not self._running:
            return
        try:
            now = time.monotonic()
            if self._balloon.winfo_ismapped() and hasattr(self, "_balloon_hide_at"):
                if now > self._balloon_hide_at:
                    self.hide_balloon()

            if now > self._state_until:
                self._state = self._return_state
                self._state_until = float("inf")

            if self._state == "idle" and (now - self._last_interaction) > 45.0:
                self._state = "sleeping"

            if self._visible:
                self._phase += 0.14
                # Fundo Dark Obsidian matching o card do app
                img = self._renderer.render(
                    state=self._state,
                    phase=self._phase,
                    rms=self._rms,
                    look_x=self._look_x,
                    look_y=self._look_y,
                    bg_rgba=(15, 23, 42, 255),
                )
                # Reduz levemente para caber confortavel no card 164x176 mantendo serrilhado
                resized = img.resize((int(DISPLAY_W * 0.80), int(DISPLAY_H * 0.78)), 0)
                self._photo_ref = ImageTk.PhotoImage(resized)
                self._lbl_sprite.configure(image=self._photo_ref)
        except Exception:
            pass
        finally:
            if self._running:
                self.after(55, self._tick)

    def destroy(self) -> None:
        self._running = False
        super().destroy()

