"""Motor de renderizacao 3D Ultra Low-Poly estilo Windows 95 para o mascote OmniMic.

Modela e rasteriza em tempo real a malha 3D do microfone radialista #03-A Ultra Low-Poly
usando apenas NumPy e Pillow (sem dependencias externas pesadas), preservando o visual
facetado (flat-shading) e serrilhado autentico de 1995.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

# Dimensao interna baixa para gerar serrilhado autentico 1995 ao ampliar com NEAREST
RENDER_W = 128
RENDER_H = 144
DISPLAY_W = 192
DISPLAY_H = 216


def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]], dtype=np.float32)


def _rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]], dtype=np.float32)


def _rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32)


class Mesh3D:
    """Malha poligonal simples composta por vertices (Nx3), faces (triangulos) e cores base."""

    def __init__(self) -> None:
        self.verts: List[Tuple[float, float, float]] = []
        self.tris: List[Tuple[int, int, int, Tuple[int, int, int], float]] = []

    def add_box(
        self,
        cx: float,
        cy: float,
        cz: float,
        sx: float,
        sy: float,
        sz: float,
        color: Tuple[int, int, int],
        shininess: float = 0.35,
    ) -> None:
        hx, hy, hz = sx * 0.5, sy * 0.5, sz * 0.5
        base = len(self.verts)
        pts = [
            (cx - hx, cy - hy, cz - hz),
            (cx + hx, cy - hy, cz - hz),
            (cx + hx, cy + hy, cz - hz),
            (cx - hx, cy + hy, cz - hz),
            (cx - hx, cy - hy, cz + hz),
            (cx + hx, cy - hy, cz + hz),
            (cx + hx, cy + hy, cz + hz),
            (cx - hx, cy + hy, cz + hz),
        ]
        self.verts.extend(pts)
        faces = [
            (0, 2, 1), (0, 3, 2),  # back
            (4, 5, 6), (4, 6, 7),  # front
            (0, 1, 5), (0, 5, 4),  # bottom
            (3, 7, 6), (3, 6, 2),  # top
            (0, 4, 7), (0, 7, 3),  # left
            (1, 2, 6), (1, 6, 5),  # right
        ]
        for a, b, c in faces:
            self.tris.append((base + a, base + b, base + c, color, shininess))

    def add_prism(
        self,
        cx: float,
        cy: float,
        cz: float,
        rx: float,
        rz: float,
        height: float,
        sides: int,
        color: Tuple[int, int, int],
        shininess: float = 0.45,
        rx_top: Optional[float] = None,
        rz_top: Optional[float] = None,
    ) -> None:
        rx_t = rx if rx_top is None else rx_top
        rz_t = rz if rz_top is None else rz_top
        hy = height * 0.5
        base = len(self.verts)

        for i in range(sides):
            ang = (2.0 * math.pi * i) / sides + (math.pi / sides)
            self.verts.append((cx + rx * math.cos(ang), cy - hy, cz + rz * math.sin(ang)))
        for i in range(sides):
            ang = (2.0 * math.pi * i) / sides + (math.pi / sides)
            self.verts.append((cx + rx_t * math.cos(ang), cy + hy, cz + rz_t * math.sin(ang)))

        bot_c = len(self.verts)
        self.verts.append((cx, cy - hy, cz))
        top_c = len(self.verts)
        self.verts.append((cx, cy + hy, cz))

        for i in range(sides):
            nxt = (i + 1) % sides
            b0, b1 = base + i, base + nxt
            t0, t1 = base + sides + i, base + sides + nxt
            self.tris.append((b0, t0, t1, color, shininess))
            self.tris.append((b0, t1, b1, color, shininess))
            self.tris.append((bot_c, b0, b1, color, shininess * 0.6))
            self.tris.append((top_c, t1, t0, color, shininess * 0.7))

    def add_capsule_head(
        self,
        cx: float,
        cy: float,
        cz: float,
        rx: float,
        rz: float,
        body_h: float,
        sides: int,
        color: Tuple[int, int, int],
        slot_color: Tuple[int, int, int],
    ) -> None:
        """Constroi a cabeca radialista facetada com fendas horizontais na grade frontal."""
        rings = [
            (-body_h * 0.58, 0.55, False),
            (-body_h * 0.46, 0.85, False),
            (-body_h * 0.34, 0.96, True),
            (-body_h * 0.22, 1.00, False),
            (-body_h * 0.10, 1.00, True),
            (body_h * 0.02, 1.00, False),
            (body_h * 0.14, 1.00, True),
            (body_h * 0.26, 1.00, False),
            (body_h * 0.38, 0.96, True),
            (body_h * 0.50, 0.84, False),
            (body_h * 0.60, 0.50, False),
        ]
        base = len(self.verts)
        for y_off, scale, _ in rings:
            for i in range(sides):
                ang = (2.0 * math.pi * i) / sides + (math.pi / sides)
                x = cx + rx * scale * math.cos(ang)
                z = cz + rz * scale * math.sin(ang)
                self.verts.append((x, cy + y_off, z))

        bot_c = len(self.verts)
        self.verts.append((cx, cy + rings[0][0], cz))
        top_c = len(self.verts)
        self.verts.append((cx, cy + rings[-1][0], cz))

        for r in range(len(rings) - 1):
            is_slot_band = rings[r][2]
            for i in range(sides):
                nxt = (i + 1) % sides
                a0 = base + r * sides + i
                a1 = base + r * sides + nxt
                b0 = base + (r + 1) * sides + i
                b1 = base + (r + 1) * sides + nxt

                # Fendas horizontais visiveis nas laterais e topo/base, deixando o centro frontal liso para o rosto
                ang_mid = (2.0 * math.pi * (i + 0.5)) / sides + (math.pi / sides)
                is_front_center = math.sin(ang_mid) > 0.72 and abs(math.cos(ang_mid)) < 0.65 and (-0.42 * body_h < y_off < 0.36 * body_h)
                is_front_or_side = math.sin(ang_mid) > -0.25
                if is_slot_band and is_front_or_side and not is_front_center:
                    tri_col = slot_color
                    shine = 0.08
                else:
                    tri_col = color
                    shine = 0.62

                self.tris.append((a0, b0, b1, tri_col, shine))
                self.tris.append((a0, b1, a1, tri_col, shine))

        for i in range(sides):
            nxt = (i + 1) % sides
            self.tris.append((bot_c, base + i, base + nxt, color, 0.35))
            top_ring = base + (len(rings) - 1) * sides
            self.tris.append((top_c, top_ring + nxt, top_ring + i, color, 0.45))

    def add_octa_sphere(
        self,
        cx: float,
        cy: float,
        cz: float,
        rx: float,
        ry: float,
        rz: float,
        color: Tuple[int, int, int],
        shininess: float = 0.35,
    ) -> None:
        """Esfera low-poly de 12 lados/aneis reduzidos (aspecto poligonal 1995)."""
        lat_angles = [-math.pi * 0.34, 0.0, math.pi * 0.34]
        sides = 8
        base = len(self.verts)
        for phi in lat_angles:
            cp, sp = math.cos(phi), math.sin(phi)
            for i in range(sides):
                th = (2.0 * math.pi * i) / sides
                self.verts.append((
                    cx + rx * cp * math.cos(th),
                    cy + ry * sp,
                    cz + rz * cp * math.sin(th),
                ))
        bot_c = len(self.verts)
        self.verts.append((cx, cy - ry, cz))
        top_c = len(self.verts)
        self.verts.append((cx, cy + ry, cz))

        for r in range(len(lat_angles) - 1):
            for i in range(sides):
                nxt = (i + 1) % sides
                a0 = base + r * sides + i
                a1 = base + r * sides + nxt
                b0 = base + (r + 1) * sides + i
                b1 = base + (r + 1) * sides + nxt
                self.tris.append((a0, b0, b1, color, shininess))
                self.tris.append((a0, b1, a1, color, shininess))

        for i in range(sides):
            nxt = (i + 1) % sides
            self.tris.append((bot_c, base + i, base + nxt, color, shininess))
            top_ring = base + (len(lat_angles) - 1) * sides
            self.tris.append((top_c, top_ring + nxt, top_ring + i, color, shininess))

    def build_arrays(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        v = np.array(self.verts, dtype=np.float32) if self.verts else np.zeros((0, 3), dtype=np.float32)
        idx = np.array([(t[0], t[1], t[2]) for t in self.tris], dtype=np.int32) if self.tris else np.zeros((0, 3), dtype=np.int32)
        cols = np.array([t[3] for t in self.tris], dtype=np.float32) if self.tris else np.zeros((0, 3), dtype=np.float32)
        shines = np.array([t[4] for t in self.tris], dtype=np.float32) if self.tris else np.zeros((0,), dtype=np.float32)
        return v, idx, cols, shines


class OmniMic3DRenderer:
    """Renderiza o OmniMic #03-A Ultra Low-Poly com esqueleto articulado e acessorios 3D."""

    def __init__(self) -> None:
        # Cores base estilo 1995 (cromado prata, fendas escuras, detalhes XP)
        silver = (192, 200, 212)
        silver_dark = (146, 156, 170)
        slot_dark = (26, 32, 42)
        white = (248, 250, 252)
        black = (18, 22, 28)
        notepad_yellow = (246, 224, 94)
        notepad_top = (180, 120, 48)
        pen_blue = (37, 99, 235)
        gold = (245, 196, 48)

        # 1. Base e Pedestal (fixo na cena)
        m_base = Mesh3D()
        m_base.add_prism(0.0, -1.32, 0.0, 0.92, 0.85, 0.18, 8, silver_dark, shininess=0.50, rx_top=0.80, rz_top=0.74)
        m_base.add_prism(0.0, -0.96, 0.0, 0.16, 0.16, 0.58, 6, silver, shininess=0.55)
        # Garfo inferior em U
        m_base.add_box(0.0, -0.66, 0.0, 0.56, 0.12, 0.22, silver_dark, shininess=0.45)
        m_base.add_box(-0.28, -0.45, 0.0, 0.12, 0.42, 0.18, silver, shininess=0.5)
        m_base.add_box(0.28, -0.45, 0.0, 0.12, 0.42, 0.18, silver, shininess=0.5)
        self.base_mesh = m_base.build_arrays()

        # 2. Cabeca do microfone (com pivô no centro da cabeca)
        m_head = Mesh3D()
        m_head.add_capsule_head(0.0, 0.0, 0.0, 0.68, 0.56, 1.50, 10, silver, slot_dark)
        # Anel de articulacao inferior
        m_head.add_prism(0.0, -0.82, 0.0, 0.24, 0.24, 0.18, 6, silver_dark, shininess=0.45)
        # Olhos brancos saltados
        m_head.add_octa_sphere(-0.24, 0.28, 0.56, 0.23, 0.25, 0.16, white, shininess=0.20)
        m_head.add_octa_sphere(0.24, 0.28, 0.56, 0.23, 0.25, 0.16, white, shininess=0.20)
        self.head_mesh = m_head.build_arrays()

        # 3. Pupilas (se movem conforme olhar/mouse)
        m_pupil = Mesh3D()
        m_pupil.add_octa_sphere(-0.24, 0.28, 0.70, 0.09, 0.10, 0.07, black, shininess=0.10)
        m_pupil.add_octa_sphere(0.24, 0.28, 0.70, 0.09, 0.10, 0.07, black, shininess=0.10)
        self.pupil_mesh = m_pupil.build_arrays()

        # 4. Palpebras (para piscar/soneca - fecham so sobre os olhos)
        m_eyelid = Mesh3D()
        m_eyelid.add_octa_sphere(-0.24, 0.28, 0.57, 0.24, 0.26, 0.17, silver_dark, shininess=0.4)
        m_eyelid.add_octa_sphere(0.24, 0.28, 0.57, 0.24, 0.26, 0.17, silver_dark, shininess=0.4)
        m_eyelid.add_box(-0.24, 0.24, 0.71, 0.32, 0.05, 0.06, black, shininess=0.05)
        m_eyelid.add_box(0.24, 0.24, 0.71, 0.32, 0.05, 0.06, black, shininess=0.05)
        self.eyelid_mesh = m_eyelid.build_arrays()

        # 5. Sobrancelhas esquerda e direita (arqueadas estilo Clippy)
        m_brow_l = Mesh3D()
        m_brow_l.add_box(-0.07, 0.02, 0.0, 0.20, 0.08, 0.11, black, shininess=0.1)
        m_brow_l.add_box(0.09, -0.02, 0.0, 0.18, 0.08, 0.11, black, shininess=0.1)
        self.brow_l_mesh = m_brow_l.build_arrays()

        m_brow_r = Mesh3D()
        m_brow_r.add_box(-0.09, -0.02, 0.0, 0.18, 0.08, 0.11, black, shininess=0.1)
        m_brow_r.add_box(0.07, 0.02, 0.0, 0.20, 0.08, 0.11, black, shininess=0.1)
        self.brow_r_mesh = m_brow_r.build_arrays()

        # 6. Boca (sorriso / falando / surpresa)
        m_mouth_smile = Mesh3D()
        m_mouth_smile.add_box(-0.14, -0.22, 0.58, 0.18, 0.07, 0.08, black, shininess=0.05)
        m_mouth_smile.add_box(0.0, -0.26, 0.60, 0.24, 0.08, 0.08, black, shininess=0.05)
        m_mouth_smile.add_box(0.14, -0.22, 0.58, 0.18, 0.07, 0.08, black, shininess=0.05)
        self.mouth_smile_mesh = m_mouth_smile.build_arrays()

        m_mouth_open = Mesh3D()
        m_mouth_open.add_prism(0.0, -0.25, 0.58, 0.22, 0.10, 0.20, 6, (128, 28, 38), shininess=0.05)
        m_mouth_open.add_box(0.0, -0.17, 0.65, 0.22, 0.05, 0.05, white, shininess=0.10)
        self.mouth_open_mesh = m_mouth_open.build_arrays()

        # 7. Braco + Mao (lado esquerdo e lado direito espelhados)
        self.arm_l_mesh = self._build_arm_mesh(side=-1, silver=silver, silver_dark=silver_dark)
        self.arm_r_mesh = self._build_arm_mesh(side=1, silver=silver, silver_dark=silver_dark)

        # 8. Acessorio Bloquinho Amarelo + Caneta (homenagem ao Clippy!)
        m_pad = Mesh3D()
        m_pad.add_box(0.0, 0.0, 0.0, 0.52, 0.66, 0.08, notepad_yellow, shininess=0.15)
        m_pad.add_box(0.0, 0.30, 0.01, 0.54, 0.10, 0.10, notepad_top, shininess=0.2)
        m_pad.add_box(0.16, 0.05, 0.08, 0.08, 0.56, 0.08, pen_blue, shininess=0.4)
        self.notepad_mesh = m_pad.build_arrays()

        # 9. Acessorio Trofeu Estrela Dourado (homenagem ao Merlin!)
        m_trophy = Mesh3D()
        m_trophy.add_prism(0.0, -0.34, 0.0, 0.22, 0.22, 0.14, 6, black, shininess=0.2)
        m_trophy.add_prism(0.0, -0.18, 0.0, 0.08, 0.08, 0.22, 6, gold, shininess=0.75)
        m_trophy.add_box(0.0, 0.12, 0.0, 0.34, 0.34, 0.14, gold, shininess=0.80)
        m_trophy.add_box(0.0, 0.12, 0.0, 0.48, 0.14, 0.12, gold, shininess=0.80)
        m_trophy.add_box(0.0, 0.12, 0.0, 0.14, 0.48, 0.12, gold, shininess=0.80)
        self.trophy_mesh = m_trophy.build_arrays()

        # Direcao da luz principal (canto superior esquerdo frontal estilo Win95)
        light = np.array([-0.45, 0.65, 0.85], dtype=np.float32)
        self.light_dir = light / np.linalg.norm(light)

    def _build_arm_mesh(
        self,
        side: int,
        silver: Tuple[int, int, int],
        silver_dark: Tuple[int, int, int],
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        m = Mesh3D()
        s = float(side)
        # Ombro -> braco superior -> antebraco -> mao poligonal
        m.add_box(s * 0.22, -0.06, 0.0, 0.44, 0.13, 0.13, silver, shininess=0.55)
        m.add_box(s * 0.46, -0.30, 0.06, 0.13, 0.48, 0.13, silver, shininess=0.55)
        # Palma da mao facetada
        m.add_prism(s * 0.50, -0.60, 0.12, 0.18, 0.12, 0.22, 6, silver_dark, shininess=0.50)
        # Dedos blocados
        m.add_box(s * 0.40, -0.76, 0.14, 0.10, 0.16, 0.10, silver, shininess=0.45)
        m.add_box(s * 0.56, -0.76, 0.14, 0.10, 0.16, 0.10, silver, shininess=0.45)
        return m.build_arrays()

    def _transform_and_collect(
        self,
        mesh: Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
        rot_mat: np.ndarray,
        offset: Tuple[float, float, float],
        bucket_verts: List[np.ndarray],
        bucket_tris: List[np.ndarray],
        bucket_cols: List[np.ndarray],
        bucket_shines: List[np.ndarray],
        vert_offset: int,
    ) -> int:
        v, idx, cols, shines = mesh
        if len(v) == 0:
            return vert_offset
        tv = (v @ rot_mat.T) + np.array(offset, dtype=np.float32)
        bucket_verts.append(tv)
        bucket_tris.append(idx + vert_offset)
        bucket_cols.append(cols)
        bucket_shines.append(shines)
        return vert_offset + len(v)

    def render(
        self,
        state: str = "idle",
        phase: float = 0.0,
        rms: float = 0.0,
        look_x: float = 0.0,
        look_y: float = 0.0,
        bg_rgba: Tuple[int, int, int, int] = (0, 0, 0, 0),
    ) -> Image.Image:
        """Renderiza 1 frame 3D Ultra Low-Poly para o estado e fase informados."""
        verts_list: List[np.ndarray] = []
        tris_list: List[np.ndarray] = []
        cols_list: List[np.ndarray] = []
        shines_list: List[np.ndarray] = []
        v_off = 0

        # Parametros cinematicos por estado
        yaw = 0.22 + 0.10 * math.sin(phase * 0.8)
        pitch = 0.04 * math.sin(phase * 1.4)
        roll = 0.0
        bob_y = 0.03 * math.sin(phase * 1.8)
        blink = False
        mouth_open = False
        arm_l_rot = _rot_z(0.18) @ _rot_x(0.1)
        arm_r_rot = _rot_z(-0.18) @ _rot_x(0.1)
        brow_l_z = 0.14
        brow_r_z = -0.14
        brow_y = 0.62

        if state == "idle":
            # Piscada ocasional a cada ciclo
            if (phase % 6.28) > 5.95:
                blink = True
            arm_r_rot = _rot_z(0.25 + 0.22 * math.sin(phase * 2.2)) @ _rot_x(0.15)
        elif state == "greeting":
            yaw = 0.15 * math.sin(phase * 2.0)
            bob_y = abs(math.sin(phase * 3.0)) * 0.10
            arm_r_rot = _rot_z(1.75 + 0.35 * math.sin(phase * 6.0)) @ _rot_x(-0.2)
            mouth_open = math.sin(phase * 4.0) > 0.0
        elif state == "listening":
            # Inclina atento + boca/sobrancelhas reagem ao RMS do audio
            level = min(1.0, max(0.0, rms * 5.0))
            yaw = -0.18 + 0.08 * math.sin(phase * 1.6)
            roll = 0.10 + 0.05 * level
            bob_y = level * 0.08
            arm_l_rot = _rot_z(-1.35 - 0.15 * level) @ _rot_x(-0.35)
            arm_r_rot = _rot_z(-0.25)
            mouth_open = level > 0.22
            brow_y = 0.64 + 0.06 * level
        elif state == "transcribing":
            # Segurando bloquinho amarelo + anotando com a mao direita
            yaw = -0.15
            pitch = 0.12
            arm_l_rot = _rot_z(-0.65) @ _rot_y(0.45) @ _rot_x(-0.55)
            arm_r_rot = _rot_z(0.55 + 0.25 * math.sin(phase * 9.0)) @ _rot_x(-0.65)
            brow_l_z = -0.16
            brow_r_z = 0.16
        elif state == "celebrating":
            # Pulando + segurando trofeu dourado
            yaw = 0.28 * math.sin(phase * 2.2)
            bob_y = abs(math.sin(phase * 4.5)) * 0.14
            arm_l_rot = _rot_z(-1.65) @ _rot_x(-0.1)
            arm_r_rot = _rot_z(1.85 + 0.18 * math.sin(phase * 5.0)) @ _rot_x(-0.15)
            mouth_open = True
            brow_y = 0.68
        elif state == "warning":
            yaw = 0.22 * math.sin(phase * 5.0)
            roll = 0.08 * math.sin(phase * 5.0)
            arm_l_rot = _rot_z(-0.85)
            arm_r_rot = _rot_z(1.45)
            brow_l_z = -0.25
            brow_r_z = 0.25
            mouth_open = True
        elif state == "spin":
            yaw = phase * 3.2
            bob_y = abs(math.sin(phase * 3.2)) * 0.12
            arm_l_rot = _rot_z(-1.1)
            arm_r_rot = _rot_z(1.1)
            mouth_open = True
        elif state == "sleeping":
            blink = True
            pitch = 0.24 + 0.05 * math.sin(phase * 1.2)
            yaw = 0.10
            bob_y = -0.04 + 0.02 * math.sin(phase * 1.2)
            arm_l_rot = _rot_z(0.10)
            arm_r_rot = _rot_z(-0.10)

        # Rotacao global leve de camera/personagem
        base_rot = _rot_y(yaw * 0.45)
        head_rot = _rot_y(yaw) @ _rot_x(pitch) @ _rot_z(roll)
        head_center = (0.0, 0.36 + bob_y, 0.0)

        # Base fixa
        v_off = self._transform_and_collect(
            self.base_mesh, base_rot, (0.0, 0.0, 0.0),
            verts_list, tris_list, cols_list, shines_list, v_off,
        )

        # Cabeca
        v_off = self._transform_and_collect(
            self.head_mesh, head_rot, head_center,
            verts_list, tris_list, cols_list, shines_list, v_off,
        )

        # Pupilas ou Palpebras fechadas
        if blink:
            v_off = self._transform_and_collect(
                self.eyelid_mesh, head_rot, head_center,
                verts_list, tris_list, cols_list, shines_list, v_off,
            )
        else:
            lx = max(-0.08, min(0.08, look_x * 0.08))
            ly = max(-0.06, min(0.06, look_y * 0.06))
            pupil_off = np.array([lx, ly, 0.0], dtype=np.float32) @ head_rot.T
            v_off = self._transform_and_collect(
                self.pupil_mesh,
                head_rot,
                (head_center[0] + float(pupil_off[0]), head_center[1] + float(pupil_off[1]), head_center[2] + float(pupil_off[2])),
                verts_list, tris_list, cols_list, shines_list, v_off,
            )

        # Sobrancelhas
        bl_pos = np.array([-0.25, brow_y, 0.62], dtype=np.float32) @ head_rot.T
        br_pos = np.array([0.25, brow_y, 0.62], dtype=np.float32) @ head_rot.T
        v_off = self._transform_and_collect(
            self.brow_l_mesh, head_rot @ _rot_z(brow_l_z),
            (head_center[0] + float(bl_pos[0]), head_center[1] + float(bl_pos[1]), head_center[2] + float(bl_pos[2])),
            verts_list, tris_list, cols_list, shines_list, v_off,
        )
        v_off = self._transform_and_collect(
            self.brow_r_mesh, head_rot @ _rot_z(brow_r_z),
            (head_center[0] + float(br_pos[0]), head_center[1] + float(br_pos[1]), head_center[2] + float(br_pos[2])),
            verts_list, tris_list, cols_list, shines_list, v_off,
        )

        # Boca
        mouth_mesh = self.mouth_open_mesh if mouth_open else self.mouth_smile_mesh
        v_off = self._transform_and_collect(
            mouth_mesh, head_rot, head_center,
            verts_list, tris_list, cols_list, shines_list, v_off,
        )

        # Bracos
        sh_l = np.array([-0.62, -0.05, 0.0], dtype=np.float32) @ head_rot.T
        sh_r = np.array([0.62, -0.05, 0.0], dtype=np.float32) @ head_rot.T
        v_off = self._transform_and_collect(
            self.arm_l_mesh, head_rot @ arm_l_rot,
            (head_center[0] + float(sh_l[0]), head_center[1] + float(sh_l[1]), head_center[2] + float(sh_l[2])),
            verts_list, tris_list, cols_list, shines_list, v_off,
        )
        v_off = self._transform_and_collect(
            self.arm_r_mesh, head_rot @ arm_r_rot,
            (head_center[0] + float(sh_r[0]), head_center[1] + float(sh_r[1]), head_center[2] + float(sh_r[2])),
            verts_list, tris_list, cols_list, shines_list, v_off,
        )

        # Acessorios 3D por estado
        if state == "transcribing":
            pad_pos = np.array([-0.55, -0.45, 0.68], dtype=np.float32) @ head_rot.T
            v_off = self._transform_and_collect(
                self.notepad_mesh, head_rot @ _rot_y(0.35) @ _rot_x(-0.20),
                (head_center[0] + float(pad_pos[0]), head_center[1] + float(pad_pos[1]), head_center[2] + float(pad_pos[2])),
                verts_list, tris_list, cols_list, shines_list, v_off,
            )
        elif state == "celebrating":
            tr_pos = np.array([1.05, 0.72, 0.30], dtype=np.float32) @ head_rot.T
            v_off = self._transform_and_collect(
                self.trophy_mesh, head_rot @ _rot_y(0.35 * math.sin(phase * 1.5)),
                (head_center[0] + float(tr_pos[0]), head_center[1] + float(tr_pos[1]), head_center[2] + float(tr_pos[2])),
                verts_list, tris_list, cols_list, shines_list, v_off,
            )

        # Monta arrays unificados para rasterizacao rapida vetorizada
        all_v = np.vstack(verts_list)
        all_t = np.vstack(tris_list)
        all_c = np.vstack(cols_list)
        all_s = np.concatenate(shines_list)

        p0 = all_v[all_t[:, 0]]
        p1 = all_v[all_t[:, 1]]
        p2 = all_v[all_t[:, 2]]

        # Normais dos triangulos
        e1 = p1 - p0
        e2 = p2 - p0
        normals = np.cross(e1, e2)
        n_len = np.linalg.norm(normals, axis=1, keepdims=True)
        n_len[n_len < 1e-6] = 1.0
        normals = normals / n_len

        # Backface culling (camera olhando para -Z a partir de Z = +5.2)
        cam_pos = np.array([0.0, -0.15, 5.2], dtype=np.float32)
        centers = (p0 + p1 + p2) / 3.0
        view_vec = cam_pos - centers
        visible_mask = np.sum(normals * view_vec, axis=1) > 0.0

        p0 = p0[visible_mask]
        p1 = p1[visible_mask]
        p2 = p2[visible_mask]
        normals = normals[visible_mask]
        cols = all_c[visible_mask]
        shines = all_s[visible_mask]
        centers = centers[visible_mask]

        # Iluminacao Flat-Shading + Especular duro estilo Phong 1995
        diff = np.clip(np.sum(normals * self.light_dir, axis=1), 0.0, 1.0)
        # Luz de preenchimento sutil vinda de baixo/direita
        fill = np.clip(np.sum(normals * np.array([0.4, -0.2, 0.5], dtype=np.float32), axis=1), 0.0, 1.0) * 0.25
        ambient = 0.28
        lit = cols * (ambient + 0.72 * diff[:, None] + fill[:, None])

        # Reflexo especular duro (caracteristica 3D anos 95)
        half_vec = self.light_dir + np.array([0.0, 0.0, 1.0], dtype=np.float32)
        half_vec = half_vec / np.linalg.norm(half_vec)
        spec_dot = np.clip(np.sum(normals * half_vec, axis=1), 0.0, 1.0)
        spec = (spec_dot ** 14) * shines * 195.0
        final_rgb = np.clip(lit + spec[:, None], 0, 255).astype(np.uint8)

        # Projecao perspectiva simples para 128x144
        fov = 142.0
        z_cam = 5.2 - all_v[:, 2]
        z_cam = np.maximum(z_cam, 0.5)
        sx = (all_v[:, 0] * fov) / z_cam + RENDER_W * 0.5
        sy = (-((all_v[:, 1] + 0.12) * fov) / z_cam) + RENDER_H * 0.52

        # Ordenacao Painter's Algorithm (mais distante primeiro)
        depths = np.linalg.norm(centers - cam_pos, axis=1)
        order = np.argsort(-depths)
        vis_idx = all_t[visible_mask][order]
        vis_rgb = final_rgb[order]

        img = Image.new("RGBA", (RENDER_W, RENDER_H), bg_rgba)
        draw = ImageDraw.Draw(img)

        for k in range(len(order)):
            i0, i1, i2 = int(vis_idx[k, 0]), int(vis_idx[k, 1]), int(vis_idx[k, 2])
            r, g, b = int(vis_rgb[k, 0]), int(vis_rgb[k, 1]), int(vis_rgb[k, 2])
            poly = [
                (int(round(sx[i0])), int(round(sy[i0]))),
                (int(round(sx[i1])), int(round(sy[i1]))),
                (int(round(sx[i2])), int(round(sy[i2]))),
            ]
            draw.polygon(poly, fill=(r, g, b, 255))

        # Amplia 2x com NEAREST para preservar bordas serrilhadas de sprite Win95
        return img.resize((DISPLAY_W, DISPLAY_H), Image.Resampling.NEAREST)
