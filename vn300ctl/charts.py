"""
Grafica de lineas en braille para la terminal.

Cada caracter braille tiene 2x4 puntos, asi que una grafica de W x H celdas
tiene 2W x 4H "pixeles". Las muestras se agrupan por columna de pixel
(envolvente min/max) y se unen con la columna anterior, de modo que no se
pierden picos aunque haya mas muestras que pixeles.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Deque, List, Optional, Sequence, Tuple

from rich.text import Text
from textual.widget import Widget

# bit del punto (x, y) dentro de la celda braille
_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))

AXIS_W = 9          # ancho de la columna de etiquetas del eje Y


class BrailleChart(Widget):
    DEFAULT_CSS = """
    BrailleChart { height: 10; width: 1fr; }
    """

    def __init__(self, title: str, labels: Sequence[str], colors: Sequence[str],
                 unit: str = "", scale: float = 1.0, fixed: Optional[Tuple[float, float]] = None,
                 wrap: Optional[float] = None, min_span: float = 1e-3,
                 source: Optional[Callable[[], Optional[Deque]]] = None,
                 lock=None, fields: Optional[Sequence[int]] = None, **kw):
        super().__init__(**kw)
        self.ctitle = title
        self.labels = list(labels)
        self.pal = list(colors)
        self.unit = unit
        self.scale = scale          # p.ej. rad/s -> deg/s
        self.fixed = fixed          # rango Y fijo (yaw: -180..180)
        self.wrap = wrap            # no unir saltos mayores que esto (yaw)
        self.min_span = min_span
        self.source = source        # devuelve el deque [(t, v0, v1, ...)]
        self.lock = lock            # lock del productor (el deque se llena desde otro hilo)
        # indice de cada serie dentro de la tupla (t, v0, v1, v2)
        self.fields = list(fields) if fields else list(range(1, len(self.labels) + 1))
        self.win = 10.0
        self.frozen_at: Optional[float] = None

    # ------------------------------------------------------------------ #
    def _visible(self, now: float) -> List[tuple]:
        dq = self.source() if self.source else None
        if not dq:
            return []
        t0 = now - self.win
        out = []
        if self.lock:
            self.lock.acquire()
        try:
            # recorrer desde el final hasta salir de la ventana
            for item in reversed(dq):
                if item[0] < t0:
                    break
                if item[0] <= now:
                    out.append(item)
        finally:
            if self.lock:
                self.lock.release()
        out.reverse()
        return out

    def render(self) -> Text:
        W = max(10, self.size.width - AXIS_W)
        H = max(2, self.size.height - 1)
        PW, PH = W * 2, H * 4
        now = self.frozen_at or time.monotonic()
        data = self._visible(now)
        nser = len(self.labels)

        # rango Y
        if self.fixed:
            lo, hi = self.fixed
        else:
            vals = [s[i] * self.scale for s in data for i in self.fields if not math.isnan(s[i])]
            if vals:
                lo, hi = min(vals), max(vals)
            else:
                lo, hi = -1.0, 1.0
            span = max(hi - lo, self.min_span)
            mid = (hi + lo) / 2
            lo, hi = mid - span * 0.55, mid + span * 0.55

        def ypix(v: float) -> int:
            p = (hi - v) / (hi - lo) * (PH - 1)
            return int(round(min(PH - 1, max(0, p))))

        bits = [[0] * W for _ in range(H)]
        color = [[None] * W for _ in range(H)]

        def dot(px: int, py: int, k: int) -> None:
            cx, cy = px >> 1, py >> 2
            bits[cy][cx] |= _BITS[py & 3][px & 1]
            color[cy][cx] = k

        def vline(px: int, a: int, b: int, k: int) -> None:
            if a > b:
                a, b = b, a
            for py in range(a, b + 1):
                dot(px, py, k)

        # agrupar por columna de pixel
        t0 = now - self.win
        for k in range(nser):
            cols: List[Optional[Tuple[float, float, float, float]]] = [None] * PW
            for s in data:
                v = s[self.fields[k]]
                if math.isnan(v):
                    continue
                v *= self.scale
                px = int((s[0] - t0) / self.win * (PW - 1))
                if not 0 <= px < PW:
                    continue
                c = cols[px]
                if c is None:
                    cols[px] = (v, v, v, v)          # primero, min, max, ultimo
                else:
                    cols[px] = (c[0], min(c[1], v), max(c[2], v), v)
            # unir huecos de hasta 1.5 s (fuentes lentas, p.ej. INS por sondeo a 4 Hz)
            max_gap = max(3, int(1.5 / self.win * PW))
            prev_last: Optional[float] = None
            prev_px = -10
            for px, c in enumerate(cols):
                if c is None:
                    continue
                first, mn, mx, last = c
                if prev_last is not None and px - prev_px <= max_gap and \
                        (self.wrap is None or abs(first - prev_last) < self.wrap):
                    # segmento interpolado desde la columna anterior
                    ya, yb = ypix(prev_last), ypix(first)
                    n = px - prev_px
                    yprev = ya
                    for x in range(prev_px + 1, px + 1):
                        y = int(round(ya + (yb - ya) * (x - prev_px) / n))
                        vline(x, yprev, y, k)
                        yprev = y
                vline(px, ypix(mn), ypix(mx), k)
                prev_last, prev_px = last, px

        # linea de cero
        zero_row = None
        if lo < 0 < hi:
            zero_row = ypix(0.0) >> 2

        out = Text(no_wrap=True, overflow="crop")
        for cy in range(H):
            if cy == 0:
                lab = f"{hi:>{AXIS_W - 2}.{self._nd(hi, lo)}f} ┤"
            elif cy == H - 1:
                lab = f"{lo:>{AXIS_W - 2}.{self._nd(hi, lo)}f} ┤"
            elif zero_row is not None and cy == zero_row:
                lab = f"{0:>{AXIS_W - 2}} ┤"
            else:
                lab = " " * (AXIS_W - 1) + "│"
            out.append(lab, style="dim")
            for cx in range(W):
                b = bits[cy][cx]
                if b:
                    out.append(chr(0x2800 + b), style=self.pal[color[cy][cx]])
                elif cy == zero_row:
                    out.append("·", style="grey35")
                else:
                    out.append(" ")
            out.append("\n")

        # leyenda con el ultimo valor
        out.append(f"{self.ctitle:<{AXIS_W}}", style="bold")
        last = data[-1] if data else None
        for k, (lab, col) in enumerate(zip(self.labels, self.pal)):
            out.append("━━ ", style=col)
            i = self.fields[k]
            val = last[i] * self.scale if last and not math.isnan(last[i]) else math.nan
            out.append(f"{lab} ", style="bold")
            out.append(f"{val:+9.3f}  " if not math.isnan(val) else "     ---  ")
        out.append(f"{self.unit}   ", style="dim")
        hz = (len(data) - 1) / (data[-1][0] - data[0][0]) if len(data) > 2 and data[-1][0] > data[0][0] else 0
        out.append(f"{self.win:g} s · {hz:.0f} Hz", style="dim")
        if self.frozen_at:
            out.append("  ⏸", style="bold yellow")
        return out

    @staticmethod
    def _nd(hi: float, lo: float) -> int:
        span = abs(hi - lo)
        if span >= 100:
            return 0
        if span >= 10:
            return 1
        if span >= 1:
            return 2
        return 3
