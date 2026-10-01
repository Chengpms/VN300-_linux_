"""
Vista 3D de la actitud del VN-300 en la terminal.

Se rasteriza a media celda: cada caracter '▀' son dos pixeles (arriba / abajo)
con su propio color, asi que una vista de W x H celdas tiene W x 2H pixeles
casi cuadrados. Hay z-buffer, sombreado plano (Lambert) y lineas con prueba
de profundidad para los ejes y la rejilla del suelo.

Marcos:
  NED (mundo)  x = Norte, y = Este, z = Abajo   (el del VN-300)
  cuerpo       x = adelante, y = derecha, z = abajo
  C_nb = Rz(yaw) Ry(pitch) Rx(roll)   (secuencia 3-2-1, como el manual)

Raton: arrastrar gira la camara, la rueda acerca/aleja, doble clic reinicia.
Teclado (con el foco en la vista): flechas giran, +/- zoom, r reinicia.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.widget import Widget

from .i18n import _

Vec = Tuple[float, float, float]
RGB = Tuple[int, int, int]

# Caja del VN-300 en ejes del cuerpo (aprox. 45 x 44 x 12 mm, escalada)
BOX_HALF: Vec = (1.0, 0.9, 0.26)

COL_TOP: RGB = (32, 178, 160)       # cara superior (-Z cuerpo)
COL_BOTTOM: RGB = (70, 78, 92)
COL_SIDE: RGB = (120, 130, 145)
COL_FRONT: RGB = (240, 140, 40)     # cara +X: hacia donde apunta el equipo
COL_ARROW: RGB = (250, 250, 250)
AXIS_COL = {"X": (255, 95, 95), "Y": (95, 215, 95), "Z": (95, 175, 255)}
GRID_COL: RGB = (64, 78, 108)
NORTH_COL: RGB = (255, 200, 60)
SKY_TOP: RGB = (10, 14, 28)
SKY_BOT: RGB = (24, 30, 48)

DEFAULT_CAM = (205.0, 30.0, 6.4)    # azimut (deg, rumbo desde el Norte), elevacion (deg), distancia


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: Vec) -> Vec:
    n = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def dcm_body_to_ned(yaw: float, pitch: float, roll: float) -> List[List[float]]:
    """C_nb para yaw/pitch/roll en grados (3-2-1)."""
    y, p, r = (math.radians(a) for a in (yaw, pitch, roll))
    cy, sy, cp, sp, cr, sr = math.cos(y), math.sin(y), math.cos(p), math.sin(p), math.cos(r), math.sin(r)
    return [[cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy],
            [cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy],
            [-sp, sr * cp, cr * cp]]


def _mul(m: List[List[float]], v: Vec) -> Vec:
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def _box_faces() -> List[Tuple[List[Vec], RGB]]:
    """Caras (cuadrilateros en ejes del cuerpo, orden antihorario visto desde fuera) y su color."""
    hx, hy, hz = BOX_HALF
    v = {(sx, sy, sz): (sx * hx, sy * hy, sz * hz) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)}
    q = lambda *k: [v[c] for c in k]  # noqa: E731
    return [
        (q((1, -1, -1), (1, 1, -1), (1, 1, 1), (1, -1, 1)), COL_FRONT),       # +X
        (q((-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)), COL_SIDE),    # -X
        (q((-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)), COL_SIDE),        # +Y
        (q((-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)), COL_SIDE),    # -Y
        (q((-1, -1, -1), (-1, 1, -1), (1, 1, -1), (1, -1, -1)), COL_TOP),     # -Z (arriba)
        (q((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)), COL_BOTTOM),      # +Z (abajo)
    ]


def _arrow_tris() -> List[Tuple[List[Vec], RGB]]:
    """Flecha blanca sobre la cara superior apuntando a +X (un poco por encima para no pelear con el z)."""
    z = -BOX_HALF[2] - 0.02
    return [
        ([(0.82, 0.0, z), (0.25, 0.42, z), (0.25, -0.42, z)], COL_ARROW),
        ([(0.30, 0.15, z), (-0.6, 0.15, z), (-0.6, -0.15, z)], COL_ARROW),
        ([(0.30, 0.15, z), (-0.6, -0.15, z), (0.30, -0.15, z)], COL_ARROW),
    ]


class Attitude3D(Widget, can_focus=True):
    DEFAULT_CSS = """
    Attitude3D { height: 1fr; width: 1fr; min-height: 8; }
    """

    BINDINGS = [
        Binding("left", "orbit(-10, 0)", show=False),
        Binding("right", "orbit(10, 0)", show=False),
        Binding("up", "orbit(0, 6)", show=False),
        Binding("down", "orbit(0, -6)", show=False),
        Binding("plus,equals_sign", "zoom(0.9)", show=False),
        Binding("minus", "zoom(1.1)", show=False),
        Binding("r", "reset_view", show=False),
    ]

    def __init__(self, source: Callable[[], Optional[Sequence[float]]], lock=None, show_help: bool = True, **kw):
        super().__init__(**kw)
        self.source = source            # devuelve [yaw, pitch, roll] en grados, o None
        self.lock = lock
        self.show_help = show_help
        self.az, self.el, self.dist = DEFAULT_CAM
        self._drag: Optional[Tuple[int, int]] = None
        self._faces = _box_faces()
        self._arrow = _arrow_tris()
        self._styles: Dict[Tuple[RGB, RGB], Style] = {}

    # ------------------------------------------------------------------ #
    #  Interaccion
    # ------------------------------------------------------------------ #
    def action_orbit(self, daz: float, del_: float) -> None:
        self.az = (self.az + daz) % 360
        self.el = max(-80.0, min(85.0, self.el + del_))
        self.refresh()

    def action_zoom(self, k: float) -> None:
        self.dist = max(2.8, min(20.0, self.dist * k))
        self.refresh()

    def action_reset_view(self) -> None:
        self.az, self.el, self.dist = DEFAULT_CAM
        self.refresh()

    def on_mouse_down(self, ev: events.MouseDown) -> None:
        self._drag = (ev.screen_x, ev.screen_y)
        self.capture_mouse()

    def on_mouse_up(self, ev: events.MouseUp) -> None:
        self._drag = None
        self.release_mouse()

    def on_mouse_move(self, ev: events.MouseMove) -> None:
        if self._drag is None:
            return
        dx, dy = ev.screen_x - self._drag[0], ev.screen_y - self._drag[1]
        self._drag = (ev.screen_x, ev.screen_y)
        self.action_orbit(-dx * 4.0, dy * 6.0)

    def on_mouse_scroll_up(self, ev: events.MouseScrollUp) -> None:
        self.action_zoom(0.9)
        ev.stop()

    def on_mouse_scroll_down(self, ev: events.MouseScrollDown) -> None:
        self.action_zoom(1.1)
        ev.stop()

    def on_click(self, ev: events.Click) -> None:
        if ev.chain >= 2:
            self.action_reset_view()

    # ------------------------------------------------------------------ #
    #  Camara
    # ------------------------------------------------------------------ #
    def _camera(self):
        """Base de la camara en NED. La camara orbita alrededor del origen."""
        az, el = math.radians(self.az), math.radians(self.el)
        cam: Vec = (self.dist * math.cos(el) * math.cos(az),
                    self.dist * math.cos(el) * math.sin(az),
                    -self.dist * math.sin(el))
        target: Vec = (0.0, 0.0, 0.45)       # entre el equipo y el suelo: encuadra los dos
        cam = (cam[0], cam[1], cam[2] + target[2])
        fwd = _norm(_sub(target, cam))
        up: Vec = (0.0, 0.0, -1.0)
        right = _norm(_cross(fwd, up))
        if abs(_dot(fwd, up)) > 0.999:
            right = (0.0, 1.0, 0.0)
        cam_up = _cross(right, fwd)
        return cam, fwd, right, cam_up

    # ------------------------------------------------------------------ #
    #  Render
    # ------------------------------------------------------------------ #
    def _style(self, top: RGB, bot: RGB) -> Style:
        key = (top, bot)
        st = self._styles.get(key)
        if st is None:
            if len(self._styles) > 20000:
                self._styles.clear()
            st = Style(color="#%02x%02x%02x" % top, bgcolor="#%02x%02x%02x" % bot)
            self._styles[key] = st
        return st

    def render(self) -> Text:
        W = max(10, self.size.width)
        H = max(4, self.size.height)
        PW, PH = W, H * 2

        ypr = None
        if self.source:
            if self.lock:
                with self.lock:
                    ypr = self.source()
            else:
                ypr = self.source()
        valid = bool(ypr) and len(ypr) >= 3 and not any(math.isnan(a) for a in ypr[:3])
        yaw, pitch, roll = (ypr[0], ypr[1], ypr[2]) if valid else (0.0, 0.0, 0.0)
        C = dcm_body_to_ned(yaw, pitch, roll)

        cam, fwd, right, cup = self._camera()
        focal = min(PH * (1.6 if PH < 60 else 1.15), PW * 0.55)
        cx, cy = PW / 2.0, PH * 0.44

        def project(p: Vec) -> Optional[Tuple[float, float, float]]:
            d = _sub(p, cam)
            zc = _dot(d, fwd)
            if zc < 0.1:
                return None
            return (cx + focal * _dot(d, right) / zc, cy - focal * _dot(d, cup) / zc, zc)

        # fondo en degradado
        pix: List[List[RGB]] = []
        for y in range(PH):
            t = y / max(1, PH - 1)
            c = tuple(int(SKY_TOP[i] + (SKY_BOT[i] - SKY_TOP[i]) * t) for i in range(3))
            pix.append([c] * PW)  # type: ignore[list-item]
        zbuf = [[math.inf] * PW for _ in range(PH)]

        def put(x: int, y: int, z: float, col: RGB) -> None:
            if 0 <= x < PW and 0 <= y < PH and z < zbuf[y][x]:
                zbuf[y][x] = z
                pix[y][x] = col

        def line(a: Vec, b: Vec, col: RGB, bias: float = 0.0, write_z: bool = True) -> None:
            pa, pb = project(a), project(b)
            if pa is None or pb is None:
                return
            n = int(max(abs(pb[0] - pa[0]), abs(pb[1] - pa[1]))) + 1
            for k in range(n + 1):
                t = k / n
                x = int(round(pa[0] + (pb[0] - pa[0]) * t))
                y = int(round(pa[1] + (pb[1] - pa[1]) * t))
                z = pa[2] + (pb[2] - pa[2]) * t - bias
                if 0 <= x < PW and 0 <= y < PH and z < zbuf[y][x]:
                    if write_z:
                        zbuf[y][x] = z
                    pix[y][x] = col

        light = _norm((-0.35, -0.55, -0.75))        # desde arriba, algo al noroeste
        to_cam_light = _norm(_sub(cam, (0.0, 0.0, 0.0)))

        def tri(a: Vec, b: Vec, c: Vec, col: RGB, normal: Vec) -> None:
            pa, pb, pc = project(a), project(b), project(c)
            if pa is None or pb is None or pc is None:
                return
            # sombreado: luz fija + un poco de luz desde la camara para que nada quede negro
            diff = max(0.0, _dot(normal, light))
            fill = max(0.0, _dot(normal, to_cam_light))
            k = min(1.0, 0.28 + 0.55 * diff + 0.30 * fill)
            shade = (int(col[0] * k), int(col[1] * k), int(col[2] * k))
            x0 = max(0, int(math.floor(min(pa[0], pb[0], pc[0]))))
            x1 = min(PW - 1, int(math.ceil(max(pa[0], pb[0], pc[0]))))
            y0 = max(0, int(math.floor(min(pa[1], pb[1], pc[1]))))
            y1 = min(PH - 1, int(math.ceil(max(pa[1], pb[1], pc[1]))))
            den = (pb[1] - pc[1]) * (pa[0] - pc[0]) + (pc[0] - pb[0]) * (pa[1] - pc[1])
            if abs(den) < 1e-9:
                return
            for y in range(y0, y1 + 1):
                py = y + 0.5
                for x in range(x0, x1 + 1):
                    px = x + 0.5
                    w0 = ((pb[1] - pc[1]) * (px - pc[0]) + (pc[0] - pb[0]) * (py - pc[1])) / den
                    w1 = ((pc[1] - pa[1]) * (px - pc[0]) + (pa[0] - pc[0]) * (py - pc[1])) / den
                    w2 = 1.0 - w0 - w1
                    if w0 < -1e-6 or w1 < -1e-6 or w2 < -1e-6:
                        continue
                    z = w0 * pa[2] + w1 * pb[2] + w2 * pc[2]
                    if z < zbuf[y][x]:
                        zbuf[y][x] = z
                        pix[y][x] = shade

        # suelo: rejilla de puntos NED bajo el equipo (se apaga con la distancia) y flecha al Norte
        g, zg = 2.4, 1.15
        step = 0.8 if PH < 60 else 0.6

        def dot(p: Vec) -> None:
            q = project(p)
            if q is None:
                return
            fade = max(0.15, min(1.0, 1.0 - (q[2] - self.dist + g) / (2.6 * g)))
            col = tuple(int(SKY_BOT[i] + (GRID_COL[i] - SKY_BOT[i]) * fade) for i in range(3))
            put(int(q[0]), int(q[1]), q[2], col)  # type: ignore[arg-type]
        n = int(round(2 * g / step))
        sub = 18
        for a in range(n + 1):
            for b in range(n * sub + 1):
                u, w = -g + a * step, -g + b * step / sub
                dot((u, w, zg))
                dot((w, u, zg))
        line((0.0, 0.0, zg), (g + 0.5, 0.0, zg), NORTH_COL, bias=0.01)
        line((g + 0.5, 0.0, zg), (g + 0.1, 0.25, zg), NORTH_COL, bias=0.01)
        line((g + 0.5, 0.0, zg), (g + 0.1, -0.25, zg), NORTH_COL, bias=0.01)

        # caja del equipo (cuadrilateros -> dos triangulos), solo caras visibles
        for quad, col in self._faces:
            w = [_mul(C, v) for v in quad]
            nrm = _norm(_cross(_sub(w[1], w[0]), _sub(w[2], w[0])))
            if _dot(nrm, _sub(cam, w[0])) <= 0:
                continue
            tri(w[0], w[1], w[2], col, nrm)
            tri(w[0], w[2], w[3], col, nrm)
        for t3, col in self._arrow:
            w = [_mul(C, v) for v in t3]
            nrm = _mul(C, (0.0, 0.0, -1.0))
            if _dot(nrm, _sub(cam, w[0])) <= 0:
                continue
            tri(w[0], w[1], w[2], col, nrm)

        # ejes del cuerpo
        tips: Dict[str, Tuple[float, float]] = {}
        for name, v in (("X", (2.0, 0.0, 0.0)), ("Y", (0.0, 1.8, 0.0)), ("Z", (0.0, 0.0, 1.4))):
            tip = _mul(C, v)
            line((0.0, 0.0, 0.0), tip, AXIS_COL[name], bias=0.02)
            p = project(tip)
            if p:
                tips[name] = (p[0], p[1])
        pn = project((g + 0.85, 0.0, zg))

        # pixeles -> celdas
        labels: Dict[Tuple[int, int], Tuple[str, RGB]] = {}

        def label(px: float, py: float, txt: str, col: RGB) -> None:
            cxl, cyl = int(px), int(py) // 2
            for i, ch in enumerate(txt):
                if 0 <= cxl + i < W and 0 <= cyl < H:
                    labels[(cxl + i, cyl)] = (ch, col)
        for name, (px, py) in tips.items():
            label(px + 1, py, name, AXIS_COL[name])
        if pn:
            label(pn[0], pn[1], "N", NORTH_COL)

        # rotulos fijos: valores y ayuda
        if valid:
            info = f"{_('Rumbo')} {yaw:+7.1f}°  {_('Cabeceo')} {pitch:+6.1f}°  {_('Alabeo')} {roll:+6.1f}°"
            info_col = (255, 220, 120)
        else:
            info = _("sin actitud")
            info_col = (140, 140, 150)
        for i, ch in enumerate(info[:W - 2]):
            labels[(1 + i, 0)] = (ch, info_col)
        if self.show_help and H > 10:
            hint = _("Arrastra para girar la camara · rueda para acercar · doble clic para reiniciar la vista")
            for i, ch in enumerate(hint[:W - 2]):
                labels[(1 + i, H - 1)] = (ch, (120, 130, 150))

        out = Text(no_wrap=True, overflow="crop")
        for r in range(H):
            top_row, bot_row = pix[2 * r], pix[2 * r + 1]
            for c in range(W):
                lab = labels.get((c, r))
                if lab:
                    ch, col = lab
                    out.append(ch, Style(color="#%02x%02x%02x" % col, bgcolor="#%02x%02x%02x" % bot_row[c],
                                         bold=True))
                else:
                    out.append("▀", self._style(top_row[c], bot_row[c]))
            if r < H - 1:
                out.append("\n")
        return out
