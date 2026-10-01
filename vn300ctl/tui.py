"""
Interfaz de consola (TUI) para el VN-300, al estilo de VectorNav Control Center.

Pestanas:
  F1 Conexion     puerto, baudrate (auto), info del equipo, acciones rapidas
  F2 En vivo      actitud, IMU, INS, GPS, compas GPS, grabacion CSV
  F3 Registros    catalogo, lectura/escritura con formulario, lote para flashear varios
  F4 Consola      terminal ASCII con checksum automatico
  F5 Herramientas calibracion HSI, bias (SGB/SFB), montaje (reg 26), salida binaria

Todo segun el manual UM005 para firmware v0.5.0.0 (rev. 2.22).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Input, Label,
                             ProgressBar, RichLog, Select, SelectionList,
                             Static, Switch, TabbedContent, TabPane)

from . import registers as R
from .charts import BrailleChart
from .device import AUTOBAUD_ORDER, VN300, VNError, VNTimeout, list_ports
from .livestate import LiveState
from .protocol import BIN_NAMES, GROUP_TITLES

DEG = 180.0 / math.pi
CONFIG_DIR = os.path.expanduser("~/vn300_configs")


# --------------------------------------------------------------------------- #
#  Utilidades de dibujo
# --------------------------------------------------------------------------- #

def fmt(x, nd=3, w=9) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "---".rjust(w)
    return f"{x:+{w}.{nd}f}"


def compass_tape(yaw: float, width: int = 49) -> Text:
    """Cinta de rumbo centrada en el yaw actual."""
    labels = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}
    half = width // 2
    chars = [" "] * width
    styles = ["dim"] * width
    for col in range(width):
        deg = (yaw + (col - half) * 3) % 360
        d = int(round(deg / 3) * 3) % 360
        if d % 15 == 0:
            chars[col] = "|" if d % 45 else "┃"
    for col in range(width):
        deg = (yaw + (col - half) * 3) % 360
        d = int(round(deg / 3) * 3) % 360
        if d in labels:
            lab = labels[d]
            for k, ch in enumerate(lab):
                if 0 <= col + k < width:
                    chars[col + k] = ch
                    styles[col + k] = "bold cyan"
    t = Text()
    for ch, st in zip(chars, styles):
        t.append(ch, style=st)
    t.append("\n")
    t.append(" " * half + "▲", style="bold yellow")
    return t


def horizon(pitch: float, roll: float, w: int = 33, h: int = 11) -> Text:
    """Horizonte artificial: cada celda son dos 'pixeles' verticales (▀)."""
    sky, ground = "#2563a8", "#7a4b26"
    t = Text()
    hh = h * 2
    k = hh / 2 / 30.0                      # 30 deg de pitch = media pantalla
    tr = math.tan(max(-1.5, min(1.5, math.radians(roll))))

    def color(px_y: float, c: int) -> str:
        x = (c - w // 2)                   # una celda ~ dos pixeles de alto
        return ground if px_y - hh / 2 > pitch * k - x * tr else sky

    for r in range(h):
        for c in range(w):
            top, bot = color(2 * r + 0.5, c), color(2 * r + 1.5, c)
            if r == h // 2 and abs(c - w // 2) <= 5:
                ch = "╋" if c == w // 2 else "━"
                t.append(ch, style=f"bold yellow on {bot}")
            else:
                t.append("▀", style=f"{top} on {bot}")
        t.append("\n")
    return t


def bar(value: float, rng: float, width: int = 21) -> Text:
    """Barra centrada en cero."""
    t = Text()
    if value is None or math.isnan(value):
        return Text("·" * width, style="dim")
    half = width // 2
    pos = int(round(max(-1, min(1, value / rng)) * half))
    for i in range(-half, half + 1):
        if i == 0:
            t.append("│", style="white")
        elif (0 < i <= pos) or (pos <= i < 0):
            t.append("█", style="green" if abs(value) < rng * 0.7 else "red")
        else:
            t.append("·", style="dim")
    return t


def flag(ok: bool, yes: str = "SI", no: str = "NO", bad_is_red=True) -> Text:
    return Text(f" {yes if ok else no} ", style=("bold black on green" if ok else
                                                  ("bold white on red" if bad_is_red else "black on yellow")))


# --------------------------------------------------------------------------- #
#  Pantallas modales
# --------------------------------------------------------------------------- #

class Confirm(ModalScreen[bool]):
    def __init__(self, title: str, body: str, ok: str = "Confirmar", danger: bool = False):
        super().__init__()
        self.t, self.b, self.ok, self.danger = title, body, ok, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.t, classes="dlg-title")
            yield Static(self.b, classes="dlg-body")
            with Horizontal(classes="dlg-buttons"):
                yield Button("Cancelar", id="no")
                yield Button(self.ok, id="yes", variant="error" if self.danger else "primary")

    @on(Button.Pressed)
    def _done(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


class AskPath(ModalScreen[Optional[str]]):
    def __init__(self, title: str, default: str):
        super().__init__()
        self.t, self.default = title, default

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.t, classes="dlg-title")
            yield Input(self.default, id="path")
            with Horizontal(classes="dlg-buttons"):
                yield Button("Cancelar", id="no")
                yield Button("Aceptar", id="yes", variant="primary")

    @on(Input.Submitted)
    def _sub(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value.strip() or None)

    @on(Button.Pressed)
    def _done(self, ev: Button.Pressed) -> None:
        self.dismiss(self.query_one("#path", Input).value.strip() if ev.button.id == "yes" else None)

    def key_escape(self) -> None:
        self.dismiss(None)


class BinaryWizard(ModalScreen[Optional[List[str]]]):
    """Construye los valores de los registros 75-77 marcando casillas."""

    RATES = [1, 2, 4, 5, 8, 10, 16, 20, 25, 40, 50, 80, 100, 200, 400]

    def __init__(self, rid: int, current: List[str]):
        super().__init__()
        self.rid = rid
        self.cur = current

    def compose(self) -> ComposeResult:
        mode, div, groups, words = 1, 8, 0, []
        try:
            mode, div = int(self.cur[0]), int(self.cur[1])
            groups = int(self.cur[2], 16)
            words = [int(w, 16) for w in self.cur[3:]]
        except (ValueError, IndexError):
            pass
        selected = set()
        wi = 0
        for g in range(1, 8):
            if groups & (1 << (g - 1)):
                w = words[wi] if wi < len(words) else 0
                wi += 1
                for b in range(15):
                    if w & (1 << b):
                        selected.add(f"{g}:{b}")
        items = []
        for g in range(1, 8):
            for b, name in enumerate(BIN_NAMES[g]):
                if b >= 15 or name == "Reserved":
                    continue
                key = f"{g}:{b}"
                items.append((f"G{g} {GROUP_TITLES[g]:<8} {name}", key, key in selected))
        rate_opts = [(f"{400 // d if 400 % d == 0 else round(400 / d, 2)} Hz  (divisor {d})", str(d))
                     for d in sorted({400 // r for r in self.RATES}, reverse=True)]
        with Vertical(classes="dialog wide"):
            yield Label(f"Asistente de salida binaria — registro {self.rid}", classes="dlg-title")
            with Horizontal(classes="row"):
                yield Label("Puerto:", classes="lbl")
                yield Select([("Desactivado", "0"), ("Puerto 1", "1"), ("Puerto 2", "2"), ("Ambos", "3")],
                             value=str(mode) if mode in (0, 1, 2, 3) else "1", allow_blank=False, id="bw-mode")
                yield Label("Frecuencia:", classes="lbl")
                vals = [v for _, v in rate_opts]
                yield Select(rate_opts, value=str(div) if str(div) in vals else "8",
                             allow_blank=False, id="bw-div")
            yield Static("Marca los campos a incluir (espacio para marcar):", classes="hint")
            yield SelectionList(*items, id="bw-fields")
            yield Static("", id="bw-preview", classes="hint")
            with Horizontal(classes="dlg-buttons"):
                yield Button("Cancelar", id="no")
                yield Button("Usar estos valores", id="yes", variant="primary")

    def on_mount(self) -> None:
        self._preview()

    def _values(self) -> List[str]:
        sel = self.query_one("#bw-fields", SelectionList).selected
        by_group: Dict[int, int] = {}
        for key in sel:
            g, b = map(int, key.split(":"))
            by_group[g] = by_group.get(g, 0) | (1 << b)
        groups = 0
        for g in by_group:
            groups |= 1 << (g - 1)
        mode = self.query_one("#bw-mode", Select).value
        div = self.query_one("#bw-div", Select).value
        return [str(mode), str(div), f"{groups:X}"] + [f"{by_group[g]:X}" for g in sorted(by_group)]

    @on(SelectionList.SelectedChanged)
    @on(Select.Changed)
    def _preview(self, *_):
        try:
            v = self._values()
            self.query_one("#bw-preview", Static).update(f"$VNWRG,{self.rid}," + ",".join(v))
        except Exception:
            pass

    @on(Button.Pressed)
    def _done(self, ev: Button.Pressed) -> None:
        self.dismiss(self._values() if ev.button.id == "yes" else None)

    def key_escape(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- #
#  Aplicacion
# --------------------------------------------------------------------------- #

class VN300App(App):
    TITLE = "VN-300 Control"
    SUB_TITLE = "VectorNav VN-300 — firmware v0.5.0.0"

    CSS = """
    Screen { background: $surface; }
    #status { height: 1; background: $primary-background; color: $text; padding: 0 1; }
    .panel { border: round $primary; padding: 0 1; height: auto; }
    .panel-title { color: $accent; text-style: bold; }
    .row { height: auto; margin: 0 0 1 0; }
    .lbl { width: auto; padding: 1 1 0 0; }
    .hint { color: $text-muted; }
    Button { margin: 0 1 0 0; min-width: 10; }
    Select { width: 30; }
    .wide-select { width: 50; }
    #conn-grid { grid-size: 2; grid-columns: 1fr 1fr; grid-gutter: 1; height: auto; }
    #live-grid { grid-size: 3; grid-columns: 38 1fr 1fr; grid-rows: auto; grid-gutter: 0 1; height: auto; }
    #chart-grid { grid-size: 2; grid-columns: 1fr 1fr; grid-rows: auto; grid-gutter: 0 1; height: auto; }
    .chart { height: 17; }
    .narrow-select { width: 14; }
    .-narrow #live-grid { grid-size: 1; grid-columns: 1fr; }
    .-narrow #chart-grid { grid-size: 1; grid-columns: 1fr; }
    .-narrow #conn-grid { grid-size: 1; }
    .-narrow .wide-select { width: 28; }
    .-narrow #reg-split { layout: vertical; }
    .-narrow #reg-left { width: 1fr; height: 60%; }
    .-narrow #reg-right { width: 1fr; height: 40%; }
    #reg-left { width: 120; }
    .filter-row { height: 3; }
    #reg-filter { width: 1fr; }
    #reg-scope { width: 34; }
    #reg-table { height: 1fr; }
    #reg-filter { margin: 0 0 0 0; }
    #reg-right { padding: 0 1; }
    #reg-form { height: auto; }
    .field-row { height: auto; }
    .field-name { width: 26; padding: 1 1 0 0; }
    .field-input { width: 30; }
    .field-hint { padding: 1 0 0 1; color: $text-muted; width: 1fr; }
    #batch-table { height: 8; }
    #console-log { height: 1fr; border: round $primary; }
    #console-input { dock: bottom; }
    .dialog { width: 70; height: auto; max-height: 90%; border: thick $accent; background: $panel; padding: 1 2; }
    .dialog.wide { width: 100; }
    ModalScreen { align: center middle; }
    .dlg-title { text-style: bold; color: $accent; margin: 0 0 1 0; }
    .dlg-buttons { height: auto; margin: 1 0 0 0; align-horizontal: right; }
    #bw-fields { height: 20; }
    .danger { color: $error; }
    #hsi-progress { width: 40; }
    """

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (150, "-wide")]

    BINDINGS = [
        Binding("f1", "tab('tab-conn')", "Conexion", priority=True),
        Binding("f2", "tab('tab-live')", "En vivo", priority=True),
        Binding("f3", "tab('tab-regs')", "Registros", priority=True),
        Binding("f4", "tab('tab-console')", "Consola", priority=True),
        Binding("f5", "tab('tab-tools')", "Herramientas", priority=True),
        Binding("ctrl+s", "save_flash", "Guardar flash", priority=True),
        Binding("ctrl+r", "record", "Grabar CSV"),
        Binding("ctrl+f", "freeze", "Congelar graficas"),
        Binding("ctrl+q", "quit", "Salir", priority=True),
    ]

    def __init__(self, port: Optional[str] = None, baud: Optional[int] = None):
        super().__init__()
        self.dev = VN300()
        self.live = LiveState()
        self.init_port, self.init_baud = port, baud
        self.info: Dict[str, str] = {}
        self.reg_cache: Dict[int, List[str]] = {}
        # resultado de la ultima lectura de cada ID: lista de valores, o codigo VNERR / "timeout"
        self.reg_state: Dict[int, object] = {}
        self.batch: Dict[int, List[str]] = {}
        self.cur_reg: Optional[R.Register] = None
        self.field_widgets: List = []
        self.raw_q: deque = deque(maxlen=2000)
        self.show_async_in_console = False
        self.poll_mode = "auto"
        self._poll_stop = threading.Event()
        self._last_bytes = 0
        self._last_bytes_t = time.monotonic()
        self._bps = 0.0

        self.dev.on_ascii = self._on_ascii
        self.dev.on_binary = self.live.ingest_binary
        self.dev.on_raw = lambda d, t: self.raw_q.append((d, t))
        self.dev.on_disconnect = lambda e: self.call_from_thread(self._lost, e)

    # ------------------------------------------------------------------ #
    #  Composicion
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with TabbedContent(initial="tab-conn"):
            with TabPane("F1 Conexion", id="tab-conn"):
                yield from self._compose_conn()
            with TabPane("F2 En vivo", id="tab-live"):
                yield from self._compose_live()
            with TabPane("F3 Registros", id="tab-regs"):
                yield from self._compose_regs()
            with TabPane("F4 Consola", id="tab-console"):
                yield from self._compose_console()
            with TabPane("F5 Herramientas", id="tab-tools"):
                yield from self._compose_tools()
        yield Footer()

    def _port_options(self):
        opts = [(p, p.split("  ")[0]) for p in list_ports()]
        opts.append(("sim  (VN-300 simulado, sin hardware)", "sim"))
        return opts

    def _compose_conn(self) -> ComposeResult:
        with VerticalScroll():
            with Vertical(classes="panel"):
                yield Label("1. Conectar", classes="panel-title")
                with Horizontal(classes="row"):
                    yield Label("Puerto:", classes="lbl")
                    yield Select(self._port_options(), id="sel-port", prompt="Elige puerto",
                                 classes="wide-select")
                    yield Button("↻", id="btn-refresh", tooltip="Volver a buscar puertos")
                    yield Label("Baudrate:", classes="lbl")
                    yield Select([("Auto-detectar", "auto")] + [(str(b), str(b)) for b in sorted(AUTOBAUD_ORDER)],
                                 value="auto", allow_blank=False, id="sel-baud")
                with Horizontal(classes="row"):
                    yield Button("Conectar", id="btn-connect", variant="success")
                    yield Button("Desconectar", id="btn-disconnect", variant="warning")
                yield Static("Si no aparece /dev/ttyUSB0: revisa el cable y que tu usuario este en el grupo "
                             "'dialout' (sudo usermod -aG dialout $USER y vuelve a iniciar sesion).", classes="hint")
            with Grid(id="conn-grid"):
                with Vertical(classes="panel"):
                    yield Label("2. Equipo", classes="panel-title")
                    yield Static("Sin conexion", id="dev-info")
                with Vertical(classes="panel"):
                    yield Label("3. Salida asincrona ASCII (registros 6 y 7)", classes="panel-title")
                    with Horizontal(classes="row"):
                        yield Label("Mensaje:", classes="lbl")
                        yield Select([(f"{k:>2} {v}", str(k)) for k, v in R.ADOR.items()], id="sel-ador",
                                     prompt="ADOR")
                    with Horizontal(classes="row"):
                        yield Label("Frecuencia:", classes="lbl")
                        yield Select([(f"{f} Hz", str(f)) for f in R.ADOF], id="sel-adof", prompt="ADOF")
                    with Horizontal(classes="row"):
                        yield Button("Aplicar", id="btn-async-apply", variant="primary")
                        yield Button("Pausar", id="btn-async-pause")
                        yield Button("Reanudar", id="btn-async-resume")
                with Vertical(classes="panel"):
                    yield Label("4. Acciones del equipo", classes="panel-title")
                    with Horizontal(classes="row"):
                        yield Button("Guardar en flash", id="btn-wnv", variant="primary",
                                     tooltip="$VNWNV: guarda todos los registros en memoria no volatil (Ctrl+S)")
                        yield Button("Reset", id="btn-reset", tooltip="$VNRST: reinicia el equipo")
                        yield Button("Restaurar fabrica", id="btn-rfs", variant="error")
                    yield Static("Los cambios en registros quedan en RAM hasta pulsar 'Guardar en flash'. "
                                 "El equipo debe estar quieto al guardar.", classes="hint")
                with Vertical(classes="panel"):
                    yield Label("5. Trafico", classes="panel-title")
                    yield Static("", id="traffic")

    def _chart(self, key: str, title: str, labels, unit: str, **kw) -> BrailleChart:
        return BrailleChart(title, labels, ["#ff5f5f", "#5fd75f", "#5fafff"], unit,
                            source=lambda k=key: self.live.series.get(k), lock=self.live.lock,
                            id=f"ch-{key}", classes="chart", **kw)

    def _vel_series(self):
        """NED si el equipo la manda; si no, velocidad en ejes del cuerpo."""
        ned = self.live.series.get("vel_ned")
        if ned and self.live.age("vel_ned") < 2.0:
            return ned
        return self.live.series.get("vel_body") or ned

    def _compose_live(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(classes="row"):
                yield Label("Fuente:", classes="lbl")
                yield Select([("Auto (asincrono + sondeo de lo que falte)", "auto"),
                              ("Solo asincrono / binario", "async"),
                              ("Solo sondeo de registros", "poll")],
                             value="auto", allow_blank=False, id="sel-poll", classes="wide-select")
                yield Label("Ventana:", classes="lbl")
                yield Select([(f"{w} s", str(w)) for w in (2, 5, 10, 30, 60)], value="10",
                             allow_blank=False, id="sel-window", classes="narrow-select")
                yield Button("⏸ Congelar", id="btn-freeze", tooltip="Congela las graficas (Ctrl+F)")
                yield Button("● Grabar CSV", id="btn-rec", variant="error")
                yield Static("", id="rec-info", classes="lbl")
            with Grid(id="live-grid"):
                with Vertical(classes="panel", id="att-panel"):
                    yield Label("Actitud", classes="panel-title")
                    yield Static("", id="att-text")
                    yield Static("", id="horizon")
                    yield Static("", id="compass")
                with Vertical(classes="panel"):
                    yield Label("IMU (compensada)", classes="panel-title")
                    yield Static("", id="imu-text")
                    yield Label("Compas GPS", classes="panel-title")
                    yield Static("", id="compass-text")
                with Vertical(classes="panel"):
                    yield Label("INS", classes="panel-title")
                    yield Static("", id="ins-text")
                    yield Label("GPS A / B", classes="panel-title")
                    yield Static("", id="gnss-text")
            with Grid(id="chart-grid"):
                with Vertical(classes="panel"):
                    yield BrailleChart("Pitch/Roll", ["Pitch", "Roll"], ["#5fd75f", "#5fafff"], "deg",
                                       source=lambda: self.live.series.get("ypr"), lock=self.live.lock,
                                       fields=(2, 3), id="ch-pr", classes="chart", min_span=1.0)
                with Vertical(classes="panel"):
                    yield self._chart("gyro", "Gyro", ["X", "Y", "Z"], "deg/s", scale=DEG, min_span=0.5)
                with Vertical(classes="panel"):
                    yield self._chart("accel", "Accel", ["X", "Y", "Z"], "m/s²", min_span=0.05)
                with Vertical(classes="panel"):
                    yield self._chart("mag", "Mag", ["X", "Y", "Z"], "Gauss", min_span=0.01)
                with Vertical(classes="panel"):
                    yield BrailleChart("Velocidad", ["N|X", "E|Y", "D|Z"], ["#ff5f5f", "#5fd75f", "#5fafff"], "m/s",
                                       source=self._vel_series, lock=self.live.lock,
                                       id="ch-vel", classes="chart", min_span=0.1)
                with Vertical(classes="panel"):
                    yield BrailleChart("Yaw", ["Yaw"], ["#ffaf00"], "deg",
                                       source=lambda: self.live.series.get("ypr"), lock=self.live.lock,
                                       fields=(1,), fixed=(-180.0, 180.0), wrap=180.0,
                                       id="ch-yaw", classes="chart")

    def _compose_regs(self) -> ComposeResult:
        with Horizontal(id="reg-split"):
            with Vertical(id="reg-left"):
                with Horizontal(classes="filter-row"):
                    yield Input(placeholder="Filtrar por nombre, ID, grupo o estado…", id="reg-filter")
                    yield Select([("Documentados + detectados", "known"), ("Solo los que responden", "resp"),
                                  ("Solo activos", "active"), ("Todos (0-255)", "all")],
                                 value="known", allow_blank=False, id="reg-scope")
                yield Static("", id="scan-info", classes="hint")
                yield DataTable(id="reg-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="row"):
                    yield Button("Escanear equipo", id="btn-read-all", variant="primary",
                                 tooltip="Lee los 256 IDs ($VNRRG) y marca cuales existen y cuales estan activos")
                    yield Button("Exportar config", id="btn-export")
                    yield Button("Importar config", id="btn-import")
            with VerticalScroll(id="reg-right"):
                yield Static("Selecciona un registro de la lista.", id="reg-title")
                yield Static("", id="reg-desc", classes="hint")
                yield Vertical(id="reg-form")
                with Horizontal(classes="row"):
                    yield Button("Leer", id="btn-read", variant="primary", tooltip="$VNRRG")
                    yield Button("Escribir", id="btn-write", variant="warning", tooltip="$VNWRG (queda en RAM)")
                    yield Button("Escribir + flash", id="btn-write-flash", variant="error",
                                 tooltip="$VNWRG y luego $VNWNV")
                    yield Button("+ Lote", id="btn-batch-add", tooltip="Agregar al lote para flashear varios juntos")
                    yield Button("Asistente binario", id="btn-bin-wizard")
                yield Label("Lote de escritura (varios registros a la vez)", classes="panel-title")
                yield DataTable(id="batch-table", cursor_type="row")
                with Horizontal(classes="row"):
                    yield Button("Flashear lote", id="btn-batch-flash", variant="error",
                                 tooltip="Escribe todos los registros del lote y guarda en flash")
                    yield Button("Quitar seleccionado", id="btn-batch-del")
                    yield Button("Vaciar", id="btn-batch-clear")
                    yield Button("Guardar lote…", id="btn-batch-save")
                yield ProgressBar(id="batch-progress", show_eta=False)

    def _compose_console(self) -> ComposeResult:
        with Vertical():
            with Horizontal(classes="row"):
                yield Label("Mostrar trafico de fondo (asincrono + sondeo):", classes="lbl")
                yield Switch(False, id="sw-async-console")
                yield Button("Limpiar", id="btn-console-clear")
                yield Static("Escribe p.ej.  RRG,8   WRG,7,40   WNV   ASY,0  — '$VN' y el checksum se agregan solos.",
                             classes="hint lbl")
            yield RichLog(id="console-log", max_lines=3000, markup=False, wrap=False)
            yield Input(placeholder="Comando…  (Enter para enviar)", id="console-input")

    def _compose_tools(self) -> ComposeResult:
        with VerticalScroll():
            with Vertical(classes="panel"):
                yield Label("Calibracion hard/soft iron (registros 44 / 47 / 23)", classes="panel-title")
                yield Static("1) Iniciar  2) Gira el equipo despacio en todas las orientaciones  "
                             "3) Detener  4) Guardar en flash.", classes="hint")
                with Horizontal(classes="row"):
                    yield Label("Velocidad:", classes="lbl")
                    hsi_lbl = {1: "1  lento (60-90 s), preciso", 3: "3  medio", 5: "5  rapido (15-20 s)"}
                    yield Select([(hsi_lbl.get(i, str(i)), str(i)) for i in range(1, 6)],
                                 value="3", allow_blank=False, id="sel-hsi-rate", classes="wide-select")
                    yield Button("Iniciar", id="btn-hsi-start", variant="success")
                    yield Button("Detener", id="btn-hsi-stop", variant="warning")
                    yield Button("Reiniciar solucion", id="btn-hsi-reset")
                    yield Button("Leer solucion", id="btn-hsi-read")
                    yield Button("Copiar a reg 23", id="btn-hsi-copy")
                yield Static("", id="hsi-out")
            with Vertical(classes="panel"):
                yield Label("Bias de arranque (registro 74, comandos $VNSGB / $VNSFB)", classes="panel-title")
                yield Static("Con el equipo quieto y el filtro convergido, copia los bias estimados al registro 74 "
                             "para que el filtro arranque con ellos. Se guarda en flash ($VNWNV).", classes="hint")
                with Horizontal(classes="row"):
                    yield Button("Guardar bias giroscopo (SGB)", id="btn-sgb")
                    yield Button("Guardar bias filtro INS (SFB)", id="btn-sfb")
                    yield Button("Leer reg 74", id="btn-read74")
                yield Static("", id="bias-out")
            with Vertical(classes="panel"):
                yield Label("Montaje del sensor (registro 26, Reference Frame Rotation)", classes="panel-title")
                yield Static("Angulos (deg) del sensor respecto al vehiculo, secuencia 3-2-1. "
                             "Requiere guardar en flash y reset para aplicarse.", classes="hint")
                with Horizontal(classes="row"):
                    for k in ("Yaw", "Pitch", "Roll"):
                        yield Label(f"{k}:", classes="lbl")
                        yield Input("0", id=f"mnt-{k.lower()}", classes="field-input", type="number")
                with Horizontal(classes="row"):
                    yield Button("Calcular matriz", id="btn-mnt-calc")
                    yield Button("Escribir + flash + reset", id="btn-mnt-write", variant="error")
                yield Static("", id="mnt-out")
            with Vertical(classes="panel"):
                yield Label("Salida binaria (registros 75-77)", classes="panel-title")
                with Horizontal(classes="row"):
                    for n, rid in ((1, 75), (2, 76), (3, 77)):
                        yield Button(f"Configurar salida {n} (reg {rid})", id=f"btn-binwiz-{rid}")
                yield Static("", id="bin-out")

    # ------------------------------------------------------------------ #
    #  Arranque
    # ------------------------------------------------------------------ #
    def on_mount(self) -> None:
        t = self.query_one("#reg-table", DataTable)
        t.add_column("ID", key="id", width=4)
        t.add_column("Nombre", key="name", width=30)
        t.add_column("Grupo", key="group", width=11)
        t.add_column("Acc", key="acc", width=3)
        t.add_column("Estado", key="state", width=11)
        t.add_column("Detalle", key="detail", width=44)
        self._fill_reg_table("")
        b = self.query_one("#batch-table", DataTable)
        b.add_column("ID", key="id", width=4)
        b.add_column("Registro", key="name", width=28)
        b.add_column("Valores", key="vals")
        self.query_one("#batch-progress", ProgressBar).display = False
        self.set_interval(0.066, self._refresh_live)
        self.set_interval(0.2, self._drain_console)
        self.set_interval(0.5, self._refresh_status)
        self._refresh_status()
        if self.init_port:
            sel = self.query_one("#sel-port", Select)
            opts = [v for _, v in self._port_options()]
            if self.init_port not in opts:
                sel.set_options([(self.init_port, self.init_port)] + self._port_options())
            sel.value = self.init_port
            if self.init_baud:
                self.query_one("#sel-baud", Select).value = str(self.init_baud)
            self.do_connect()

    def action_tab(self, tab: str) -> None:
        # Si el foco queda en un widget de la pestana anterior, TabbedContent
        # vuelve a activarla: soltar el foco antes de cambiar.
        self.screen.set_focus(None)
        self.query_one(TabbedContent).active = tab
        if tab == "tab-console":
            self.query_one("#console-input", Input).focus()
        elif tab == "tab-regs":
            self.query_one("#reg-table", DataTable).focus()

    # ------------------------------------------------------------------ #
    #  Conexion
    # ------------------------------------------------------------------ #
    @on(Button.Pressed, "#btn-refresh")
    def _refresh_ports(self) -> None:
        self.query_one("#sel-port", Select).set_options(self._port_options())
        self.notify("Lista de puertos actualizada")

    @on(Button.Pressed, "#btn-connect")
    def _connect_btn(self) -> None:
        self.do_connect()

    @work(thread=True, exclusive=True, group="conn")
    def do_connect(self) -> None:
        port = self.query_one("#sel-port", Select).value
        baud = self.query_one("#sel-baud", Select).value
        if port is Select.NULL or not port:
            self.call_from_thread(self.notify, "Elige un puerto primero", severity="warning")
            return
        self._stop_polling()
        self.call_from_thread(self.notify, f"Conectando a {port}…")
        try:
            if baud == "auto":
                b = self.dev.autodetect(port)
            else:
                self.dev.open(port, int(baud))
                self.dev.read_register(1, timeout=0.6)
                b = int(baud)
        except Exception as exc:
            self.dev.close()
            self.call_from_thread(self.notify, f"No se pudo conectar: {exc}", severity="error", timeout=8)
            return
        self.info = self.dev.device_info()
        for rid in (6, 7):
            try:
                self.reg_cache[rid] = self.dev.read_register(rid)
            except (VNTimeout, VNError):
                pass
        self.call_from_thread(self._after_connect, b)
        self._start_polling()
        self.call_from_thread(self.scan_regs, False)

    def _after_connect(self, baud: int) -> None:
        self.reg_state.clear()          # puede ser otro equipo
        self._fill_reg_table()
        fw = self.info.get("fw", "")
        if fw and not self.dev.firmware_matches(fw):
            self.notify(f"El equipo tiene firmware v{fw}; la app usa la configuracion de v{R.FIRMWARE} "
                        f"({R.MANUAL}). Los registros que no esten en ese manual se muestran como "
                        "'sin documentar'.", severity="warning", timeout=15)
        self.notify(f"Conectado a {self.dev.port} @ {baud} baud", severity="information")
        self._render_info()
        for rid, sel in ((6, "#sel-ador"), (7, "#sel-adof")):
            v = self.reg_cache.get(rid)
            if v:
                try:
                    self.query_one(sel, Select).value = str(int(v[0]))
                except Exception:
                    pass

    def _render_info(self) -> None:
        if not self.dev.connected:
            self.query_one("#dev-info", Static).update("Sin conexion")
            return
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold")
        t.add_column()
        for k, lab in (("model", "Modelo"), ("serial", "N/S"), ("fw", "Firmware"), ("hw", "Hardware"),
                       ("tag", "User tag")):
            t.add_row(lab, self.info.get(k, "?"))
        fw = self.info.get("fw", "")
        t.add_row("Catalogo", Text(f"firmware v{R.FIRMWARE}", style="green" if self.dev.firmware_matches(fw)
                                   else "bold black on yellow"))
        t.add_row("Puerto", f"{self.dev.port} @ {self.dev.baud}")
        t.add_row("Checksum", "CRC16" if self.dev.use_crc else "8-bit")
        self.query_one("#dev-info", Static).update(t)

    @on(Button.Pressed, "#btn-disconnect")
    def _disconnect(self) -> None:
        self._stop_polling()
        self.dev.close()
        self.live.stop_csv()
        self._render_info()
        self.notify("Desconectado")

    def _lost(self, err: str) -> None:
        self._stop_polling()
        self.notify(f"Conexion perdida: {err}", severity="error", timeout=10)
        self._render_info()

    # ------------------------------------------------------------------ #
    #  Sondeo en segundo plano
    # ------------------------------------------------------------------ #
    def _start_polling(self) -> None:
        self._poll_stop.clear()
        threading.Thread(target=self._poll_loop, daemon=True, name="vn300-poll").start()

    def _stop_polling(self) -> None:
        self._poll_stop.set()

    def _poll_loop(self) -> None:
        slow = [63, 58, 103, 98, 86, 54]
        k = 0
        last_slow = 0.0
        while not self._poll_stop.is_set() and self.dev.connected:
            mode = self.poll_mode
            if mode == "async":
                time.sleep(0.2)
                continue
            did = False
            # Rapido: actitud + IMU si nadie la esta mandando
            if mode == "poll" or self.live.age("ypr") > 0.6 or self.live.age("gyro") > 0.6:
                self._poll_one(27)
                did = True
            now = time.monotonic()
            if now - last_slow > 0.25:
                rid = slow[k % len(slow)]
                k += 1
                last_slow = now
                key = {63: "ins_status", 58: "gnss1", 103: "gnss2", 98: "compass_pct",
                       86: "health", 54: "temp"}[rid]
                if mode == "poll" or self.live.age(key) > 1.0:
                    self._poll_one(rid)
                    did = True
            time.sleep(0.04 if did else 0.1)

    def _poll_one(self, rid: int) -> None:
        try:
            vals = self.dev.read_register(rid, timeout=0.3, retries=0, quiet=True)
            self.live.ingest_register(rid, vals, f"RRG{rid}")
        except (VNTimeout, VNError):
            pass

    @on(Select.Changed, "#sel-window")
    def _window(self, ev: Select.Changed) -> None:
        for ch in self.query(BrailleChart):
            ch.win = float(ev.value)

    @on(Button.Pressed, "#btn-freeze")
    def action_freeze(self) -> None:
        charts = list(self.query(BrailleChart))
        frozen = charts[0].frozen_at is None if charts else False
        now = time.monotonic()
        for ch in charts:
            ch.frozen_at = now if frozen else None
            ch.refresh()
        self.query_one("#btn-freeze", Button).label = "▶ Reanudar" if frozen else "⏸ Congelar"

    @on(Select.Changed, "#sel-poll")
    def _poll_mode(self, ev: Select.Changed) -> None:
        self.poll_mode = str(ev.value)

    def _on_ascii(self, msg) -> None:
        self.live.ingest_ascii(msg)
        if self.show_async_in_console:
            self.raw_q.append(("AS", msg.raw))

    # ------------------------------------------------------------------ #
    #  Refresco de pantallas
    # ------------------------------------------------------------------ #
    def _refresh_status(self) -> None:
        if not self.is_running or not self.screen.query("#status"):
            return              # app cerrandose
        now = time.monotonic()
        dt = now - self._last_bytes_t
        if dt > 0:
            self._bps = (self.dev.stats["bytes"] - self._last_bytes) / dt
        self._last_bytes, self._last_bytes_t = self.dev.stats["bytes"], now
        t = Text()
        if self.dev.connected:
            t.append(" ● CONECTADO ", style="bold black on green")
            t.append(f"  {self.dev.port} @ {self.dev.baud}  ")
            t.append(f"{self.info.get('model', '')}  SN {self.info.get('serial', '')}  ", style="cyan")
            t.append(f"RX {self._bps / 1024:.1f} kB/s  ")
        else:
            t.append(" ○ DESCONECTADO ", style="bold white on red")
            t.append("  Ve a F1 para conectar  ")
        if self.batch:
            t.append(f" LOTE: {len(self.batch)} ", style="black on yellow")
            t.append(" ")
        if self.live.recording:
            t.append(f" ● REC {self.live.csv_rows} ", style="bold white on red")
        self.query_one("#status", Static).update(t)

        rates = self.dev.rates.rates()
        tr = Table.grid(padding=(0, 2))
        tr.add_column(style="bold")
        tr.add_column(justify="right")
        for k, v in sorted(rates.items()):
            tr.add_row(k, f"{v:6.1f} Hz")
        s = self.dev.stats
        tr.add_row("Bytes", f"{s['bytes']:,}")
        tr.add_row("ASCII / BIN", f"{s['ascii']:,} / {s['binary']:,}")
        tr.add_row("Tramas malas", f"{s['bad']:,}")
        tr.add_row("VNERR", f"{s['errors']:,}")
        self.query_one("#traffic", Static).update(tr)
        if self.live.recording:
            self.query_one("#rec-info", Static).update(f"{self.live.csv_path}  ({self.live.csv_rows} filas)")

    def _refresh_live(self) -> None:
        if not self.is_running or not self.screen.query("#att-text"):
            return
        if self.query_one(TabbedContent).active != "tab-live":
            return
        L = self.live
        with L.lock:
            v = dict(L.v)
            ages = {k: L.age(k) for k in ("ypr", "gyro", "ins_status", "gnss1", "gnss2", "compass_pct", "health")}
            srcs = dict(L.sources)

        def stale(key):
            return ages.get(key, math.inf) > 2.0

        # Actitud
        ypr = v.get("ypr") or [math.nan] * 3
        at = Text()
        for name, val, unit in (("Yaw  ", ypr[0], "°"), ("Pitch", ypr[1], "°"), ("Roll ", ypr[2], "°")):
            at.append(f"{name} ", style="bold")
            at.append(f"{fmt(val, 2, 8)}{unit}", style="bold yellow" if not stale("ypr") else "dim")
            at.append("\n")
        at.append(f"fuente: {srcs.get('ypr', '-')}", style="dim")
        self.query_one("#att-text", Static).update(at)
        p, r = (ypr[1], ypr[2]) if not math.isnan(ypr[1]) else (0.0, 0.0)
        self.query_one("#horizon", Static).update(horizon(p, r))
        self.query_one("#compass", Static).update(compass_tape(0.0 if math.isnan(ypr[0]) else ypr[0], 33))

        # IMU
        g = v.get("gyro") or [math.nan] * 3
        a = v.get("accel") or [math.nan] * 3
        m = v.get("mag") or [math.nan] * 3
        ti = Table.grid(padding=(0, 1))
        for _ in range(4):
            ti.add_column(justify="right")
        ti.add_column()
        ti.add_row("", Text("X", style="bold"), Text("Y", style="bold"), Text("Z", style="bold"), "")
        ti.add_row(Text("Gyro", style="bold"), *[fmt(x * DEG, 2, 8) for x in g], "deg/s")
        ti.add_row(Text("Accel", style="bold"), *[fmt(x, 3, 8) for x in a], "m/s²")
        ti.add_row(Text("Mag", style="bold"), *[fmt(x, 4, 8) for x in m], "Gauss")
        an = math.sqrt(sum(x * x for x in a)) if not any(math.isnan(x) for x in a) else math.nan
        ti.add_row(Text("|a|", style="bold"), fmt(an, 3, 8), "", "", "m/s²")
        ti.add_row(Text("Temp", style="bold"), fmt(v.get("temp"), 1, 8), "", "", "°C")
        ti.add_row(Text("Pres", style="bold"), fmt(v.get("pres"), 2, 8), "", "", "kPa")
        self.query_one("#imu-text", Static).update(ti)

        for ch in self.query(BrailleChart):
            ch.refresh()

        # INS
        tins = Table.grid(padding=(0, 1))
        tins.add_column(style="bold")
        tins.add_column()
        st = v.get("ins_status")
        if st is not None:
            d = R.decode_ins_status(int(st))
            mode_style = {0: "black on yellow", 1: "black on yellow", 2: "bold black on green",
                          3: "bold white on red"}[d["mode"]]
            tins.add_row("Modo", Text(f" {d['mode']} {d['mode_txt']} ", style=mode_style))
            tins.add_row("GPS fix", flag(d["gnss_fix"]))
            tins.add_row("Compas GPS", flag(d["gnss_compass"], "OPERATIVO", "NO", False))
            tins.add_row("Rumbo GPS→INS", flag(d["gnss_heading_ins"], "ALINEADO", "NO", False))
            errs = [n for n, kk in (("IMU", "imu_err"), ("Mag/Pres", "magpres_err"), ("GPS", "gnss_err")) if d[kk]]
            tins.add_row("Errores", Text(", ".join(errs), style="bold red") if errs else Text("ninguno", style="green"))
        else:
            tins.add_row("Modo", Text("sin datos", style="dim"))
        lla = v.get("lla") or [math.nan] * 3
        vel = v.get("vel_ned") or [math.nan] * 3
        tins.add_row("Lat / Lon", f"{fmt(lla[0], 7, 12)}  {fmt(lla[1], 7, 12)}")
        tins.add_row("Altitud", f"{fmt(lla[2], 2, 9)} m")
        tins.add_row("Vel NED", " ".join(fmt(x, 2, 7) for x in vel) + " m/s")
        tins.add_row("Incert. att/pos/vel",
                     f"{fmt(v.get('att_unc'), 2, 5)}°  {fmt(v.get('pos_unc'), 2, 5)} m  {fmt(v.get('vel_unc'), 2, 5)} m/s")
        if stale("ins_status"):
            tins.add_row("", Text("(dato antiguo)", style="dim"))
        self.query_one("#ins-text", Static).update(tins)

        # GPS
        tg = Table(box=None, padding=(0, 1), show_edge=False)
        tg.add_column("")
        tg.add_column("Antena A", justify="right")
        tg.add_column("Antena B", justify="right")
        g1, g2 = v.get("gnss1") or {}, v.get("gnss2") or {}

        def fixtxt(gg):
            if "fix" not in gg:
                return Text("---", style="dim")
            f = int(gg["fix"])
            return Text(R.GPS_FIX.get(f, str(f)), style="green" if f >= 3 else ("yellow" if f else "red"))
        tg.add_row("Fix", fixtxt(g1), fixtxt(g2))
        tg.add_row("Satelites", str(g1.get("sats", "---")), str(g2.get("sats", "---")))
        def accs(gg):
            a = gg.get("acc") or [math.nan] * 3
            if not gg.get("fix") or any(x > 1e5 for x in a if not math.isnan(x)):
                return [math.nan] * 3          # sin fix el receptor reporta valores enormes
            return a
        acc1, acc2 = accs(g1), accs(g2)
        tg.add_row("Prec. N/E", f"{fmt(acc1[0], 2, 5)}/{fmt(acc1[1], 2, 5)}", f"{fmt(acc2[0], 2, 5)}/{fmt(acc2[1], 2, 5)}")
        tg.add_row("Prec. vert", fmt(acc1[2], 2, 6), fmt(acc2[2], 2, 6))
        l1 = g1.get("lla") or [math.nan] * 3
        tg.add_row("Lat", fmt(l1[0], 7, 12), "")
        tg.add_row("Lon", fmt(l1[1], 7, 12), "")
        self.query_one("#gnss-text", Static).update(tg)

        # Compas GPS
        tc = Table.grid(padding=(0, 1))
        tc.add_column(style="bold")
        tc.add_column()
        pct = v.get("compass_pct")
        if pct is not None and not math.isnan(pct):
            n = int(pct / 5)
            tc.add_row("Arranque", Text("█" * n + "░" * (20 - n) + f" {pct:3.0f}%",
                                        style="green" if pct >= 100 else "yellow"))
            tc.add_row("Rumbo est.", f"{fmt(v.get('compass_heading'), 2, 8)}°")
        h = v.get("health")
        if h:
            tc.add_row("Sats PVT A/B", f"{h[0]:.0f} / {h[3]:.0f}")
            tc.add_row("Sats RTK A/B", f"{h[1]:.0f} / {h[4]:.0f}")
            tc.add_row("CN0 max A/B", f"{h[2]:.0f} / {h[5]:.0f} dBHz")
            tc.add_row("Comunes PVT/RTK", f"{h[6]:.0f} / {h[7]:.0f}")
            if max(h) == 0:
                tc.add_row("Diag.", Text("Sin senal GPS: revisa antenas y cielo despejado", style="red"))
            elif h[2] < 40 and h[5] < 40:
                tc.add_row("Diag.", Text("CN0 bajo: interior / bloqueo / jamming", style="red"))
            elif h[1] < 5 or h[4] < 5:
                tc.add_row("Diag.", Text("Pocos sats RTK: cielo parcialmente tapado", style="yellow"))
        if not pct and not h:
            tc.add_row("", Text("sin datos", style="dim"))
        self.query_one("#compass-text", Static).update(tc)

    # ------------------------------------------------------------------ #
    #  Grabacion
    # ------------------------------------------------------------------ #
    @on(Button.Pressed, "#btn-rec")
    def action_record(self) -> None:
        btn = self.query_one("#btn-rec", Button)
        if self.live.recording:
            self.live.stop_csv()
            btn.label = "● Grabar CSV"
            self.notify(f"Grabacion guardada: {self.live.csv_path} ({self.live.csv_rows} filas)")
            self.query_one("#rec-info", Static).update(f"Ultimo: {self.live.csv_path}")
            return
        os.makedirs(os.path.expanduser("~/vn300_logs"), exist_ok=True)
        path = os.path.expanduser(f"~/vn300_logs/vn300_{datetime.now():%Y%m%d_%H%M%S}.csv")
        try:
            self.live.start_csv(path)
        except OSError as exc:
            self.notify(f"No se pudo crear {path}: {exc}", severity="error")
            return
        btn.label = "■ Detener"
        self.notify(f"Grabando en {path}")

    # ------------------------------------------------------------------ #
    #  Salida asincrona y acciones del equipo
    # ------------------------------------------------------------------ #
    @on(Button.Pressed, "#btn-async-apply")
    def _async_apply(self) -> None:
        ador = self.query_one("#sel-ador", Select).value
        adof = self.query_one("#sel-adof", Select).value
        items = {}
        if ador is not Select.NULL:
            items[6] = [str(ador)]
        if adof is not Select.NULL:
            items[7] = [str(adof)]
        if items:
            self.run_writes(items, save=False, label="Salida asincrona")

    @on(Button.Pressed, "#btn-async-pause")
    def _pause(self) -> None:
        self.run_cmd(lambda: self.dev.async_pause(True), "Salida asincrona en pausa")

    @on(Button.Pressed, "#btn-async-resume")
    def _resume(self) -> None:
        self.run_cmd(lambda: self.dev.async_pause(False), "Salida asincrona reanudada")

    @on(Button.Pressed, "#btn-wnv")
    def action_save_flash(self) -> None:
        def go(ok):
            if ok:
                self.run_cmd(self.dev.write_settings, "Configuracion guardada en flash")
        self.push_screen(Confirm("Guardar en flash",
                                 "Se guardaran TODOS los registros actuales en memoria no volatil ($VNWNV).\n"
                                 "El equipo debe estar quieto durante ~0.5 s.", "Guardar"), go)

    @on(Button.Pressed, "#btn-reset")
    def _reset(self) -> None:
        def go(ok):
            if ok:
                self.run_cmd(self.dev.reset, "Equipo reiniciado")
        self.push_screen(Confirm("Reset", "Se reiniciara el VN-300. Los cambios no guardados en flash se pierden.",
                                 "Reiniciar"), go)

    @on(Button.Pressed, "#btn-rfs")
    def _rfs(self) -> None:
        def go(ok):
            if ok:
                self.run_cmd(self.dev.restore_factory, "Configuracion de fabrica restaurada")
        self.push_screen(Confirm("Restaurar configuracion de fabrica",
                                 "Se borrara TODA la configuracion guardada (baudrate, salidas, baseline, "
                                 "offsets de antena…) y el equipo se reiniciara.\n\n¿Seguro?",
                                 "Restaurar", danger=True), go)

    @work(thread=True, group="cmd")
    def run_cmd(self, fn, ok_msg: str) -> None:
        try:
            fn()
            self.call_from_thread(self.notify, ok_msg)
            self.info = self.dev.device_info()
            self.call_from_thread(self._render_info)
        except Exception as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=8)

    # ------------------------------------------------------------------ #
    #  Registros
    # ------------------------------------------------------------------ #
    def _reg_status(self, rid: int):
        """(texto estado, detalle) para la columna de la tabla."""
        st = self.reg_state.get(rid)
        if st is None:
            return Text("· sin leer", style="dim"), Text("")
        if st == 8:
            return Text("✕ no existe", style="grey50"), Text("VNERR 8: registro invalido", style="grey50")
        if not isinstance(st, list):
            txt = "sin respuesta" if st == "timeout" else f"VNERR {st}"
            return Text("! error", style="bold red"), Text(txt, style="red")
        act = R.activity(rid, st)
        preview = ",".join(st)
        if act is None:
            return Text("✓ responde", style="green"), Text(preview[:60], style="dim")
        on_, detail = act
        if on_:
            return Text("● ACTIVO", style="bold black on green"), Text(detail)
        return Text("○ inactivo", style="black on yellow"), Text(detail, style="yellow")

    def _reg_ids(self, scope: str) -> List[int]:
        known = [r.id for r in R.REGISTERS]
        found = [rid for rid, st in sorted(self.reg_state.items())
                 if isinstance(st, list) and rid not in R.BY_ID]
        if scope == "all":
            return known + [rid for rid in range(256) if rid not in R.BY_ID]
        if scope == "resp":
            return [rid for rid in known + found if isinstance(self.reg_state.get(rid), list)]
        if scope == "active":
            out = []
            for rid in known + found:
                st = self.reg_state.get(rid)
                if isinstance(st, list):
                    act = R.activity(rid, st)
                    if act and act[0]:
                        out.append(rid)
            return out
        return known + found

    def _fill_reg_table(self, flt: Optional[str] = None) -> None:
        if flt is None:
            flt = self.query_one("#reg-filter", Input).value
        t = self.query_one("#reg-table", DataTable)
        cur_key = None
        if t.row_count:
            try:
                cur_key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
            except Exception:
                cur_key = None
        t.clear()
        f = flt.lower().strip()
        scope = str(self.query_one("#reg-scope", Select).value)
        for rid in self._reg_ids(scope):
            reg = R.get(rid)
            state, detail = self._reg_status(rid)
            hay = f"{reg.id} {reg.name} {reg.group} {reg.async_header} {state.plain} {detail.plain}".lower()
            if f and f not in hay:
                continue
            t.add_row(str(reg.id), reg.name, reg.group,
                      Text(reg.access, style="green" if reg.writable else "dim"), state, detail,
                      key=str(reg.id))
        if cur_key is not None:
            try:
                t.move_cursor(row=t.get_row_index(cur_key))
            except Exception:
                pass
        self._update_scan_info()

    def _update_scan_info(self) -> None:
        resp = [rid for rid, st in self.reg_state.items() if isinstance(st, list)]
        if not self.reg_state:
            self.query_one("#scan-info", Static).update("Pulsa 'Escanear equipo' para ver que registros existen y cuales estan activos.")
            return
        active = sum(1 for rid in resp if (R.activity(rid, self.reg_state[rid]) or (False,))[0])
        undoc = sum(1 for rid in resp if rid not in R.BY_ID)
        self.query_one("#scan-info", Static).update(
            f"Responden {len(resp)} registros ({undoc} sin documentar) · {active} configuraciones activas")

    def _set_reg_state(self, rid: int, st) -> None:
        """Actualiza una fila sin reconstruir la tabla."""
        self.reg_state[rid] = st
        t = self.query_one("#reg-table", DataTable)
        state, detail = self._reg_status(rid)
        try:
            t.update_cell(str(rid), "state", state)
            t.update_cell(str(rid), "detail", detail)
        except Exception:
            pass
        self._update_scan_info()

    @on(Select.Changed, "#reg-scope")
    def _scope(self, ev: Select.Changed) -> None:
        self._fill_reg_table()

    @on(Input.Changed, "#reg-filter")
    def _filter(self, ev: Input.Changed) -> None:
        self._fill_reg_table(ev.value)

    @on(Input.Submitted, "#reg-filter")
    def _filter_go(self, ev: Input.Submitted) -> None:
        txt = ev.value.strip()
        if txt.isdigit():
            self._select_reg(int(txt))
        self.query_one("#reg-table", DataTable).focus()

    @on(DataTable.RowHighlighted, "#reg-table")
    def _reg_highlight(self, ev: DataTable.RowHighlighted) -> None:
        if ev.row_key is not None and ev.row_key.value:
            self._select_reg(int(ev.row_key.value))

    def _select_reg(self, rid: int) -> None:
        reg = R.get(rid)
        if self.cur_reg is reg:
            return
        self.cur_reg = reg
        self._build_form(reg, self.reg_cache.get(rid))

    def _build_form(self, reg: R.Register, values: Optional[List[str]]) -> None:
        title = Text()
        title.append(f"Registro {reg.id} — {reg.name}", style="bold")
        title.append(f"   [{reg.group}] ", style="cyan")
        title.append(" R/W " if reg.writable else " SOLO LECTURA ",
                     style="black on green" if reg.writable else "black on grey50")
        if reg.async_header:
            title.append(f"  async: $VN{reg.async_header}", style="dim")
        self.query_one("#reg-title", Static).update(title)
        desc = reg.desc
        if reg.caution:
            desc += ("\n" if desc else "") + "⚠ " + reg.caution
        if values is None:
            desc += ("\n" if desc else "") + "Valores aun no leidos: pulsa 'Leer'."
        self.query_one("#reg-desc", Static).update(desc)

        form = self.query_one("#reg-form", Vertical)
        form.remove_children()
        self.field_widgets = []
        rows = []
        vals = list(values or [])
        for i, f in enumerate(reg.fields):
            cur = vals[i] if i < len(vals) else ""
            w = self._field_widget(f, cur, reg.writable)
            self.field_widgets.append((f, w))
            unit = f" [{f.unit}]" if f.unit else ""
            hint = f.desc or ""
            if not reg.writable and f.choices and cur:
                hint = R.label_value(f, cur)
            rows.append(Horizontal(Label(f"{f.name}{unit}", classes="field-name"), w,
                                   Static(hint, classes="field-hint"), classes="field-row"))
        if reg.variable:
            extra = ",".join(vals[len(reg.fields):])
            w = Input(extra, placeholder="campos extra separados por coma (hex en reg 75-77)",
                      classes="field-input", disabled=not reg.writable)
            self.field_widgets.append((None, w))
            rows.append(Horizontal(Label("Campos extra", classes="field-name"), w,
                                   Static("OutputField por grupo, en hex", classes="field-hint"),
                                   classes="field-row"))
        form.mount_all(rows)
        self.query_one("#btn-bin-wizard", Button).display = reg.id in (75, 76, 77)
        for bid in ("#btn-write", "#btn-write-flash", "#btn-batch-add"):
            self.query_one(bid, Button).disabled = not reg.writable

    def _field_widget(self, f: R.Field, cur: str, writable: bool):
        if f.choices and writable:
            opts = [(f"{k} — {lab}" if lab != str(k) else str(k), str(k)) for k, lab in f.choices.items()]
            norm = cur
            try:
                norm = str(int(cur))
            except ValueError:
                pass
            if norm in [v for _, v in opts] or cur == "":
                sel = Select(opts, value=norm if norm else Select.NULL, prompt="(sin cambiar)" if f.optional else "elige",
                             classes="field-input")
                return sel
        return Input(cur, classes="field-input", disabled=not writable,
                     placeholder="opcional" if f.optional else "")

    def _form_values(self) -> List[str]:
        out: List[str] = []
        for f, w in self.field_widgets:
            if isinstance(w, Select):
                out.append("" if w.value is Select.NULL else str(w.value))
            elif f is None:
                out.extend([x.strip() for x in w.value.split(",") if x.strip()])
            else:
                out.append(w.value.strip())
        return out

    def _set_form_values(self, vals: List[str]) -> None:
        if self.cur_reg:
            self._build_form(self.cur_reg, vals)

    @on(Button.Pressed, "#btn-read")
    def _read_btn(self) -> None:
        if self.cur_reg:
            self.read_regs([self.cur_reg.id], show=True)

    @on(Button.Pressed, "#btn-read-all")
    def _read_all(self) -> None:
        self.scan_regs()

    @work(thread=True, exclusive=True, group="scan")
    def scan_regs(self, notify_done: bool = True) -> None:
        """Lee los 256 IDs para saber cuales existen y su estado."""
        if not self.dev.connected:
            self.call_from_thread(self.notify, "No conectado", severity="warning")
            return
        pb = self.query_one("#batch-progress", ProgressBar)

        def prog(k):
            pb.display = True
            pb.update(total=256, progress=k)
        for rid in range(256):
            if not self.dev.connected:
                return
            try:
                vals = self.dev.read_register(rid, timeout=0.3, retries=1, quiet=True)
                self.reg_cache[rid] = vals
                st = vals
            except VNError as exc:
                st = exc.code
            except VNTimeout:
                st = "timeout"
            self.reg_state[rid] = st
            if rid % 16 == 15:
                self.call_from_thread(prog, rid + 1)
        self.call_from_thread(self._fill_reg_table)
        self.call_from_thread(lambda: setattr(pb, "display", False))
        if self.cur_reg and self.cur_reg.id in self.reg_cache:
            self.call_from_thread(self._set_form_values, self.reg_cache[self.cur_reg.id])
        if notify_done:
            n = sum(1 for st in self.reg_state.values() if isinstance(st, list))
            self.call_from_thread(self.notify, f"Escaneo completo: responden {n} de 256 registros")

    @work(thread=True, group="regs")
    def read_regs(self, ids: List[int], show: bool) -> None:
        if not self.dev.connected:
            self.call_from_thread(self.notify, "No conectado", severity="warning")
            return
        errors = 0
        for rid in ids:
            try:
                vals = self.dev.read_register(rid, timeout=0.8, retries=1)
                self.reg_cache[rid] = vals
                self.call_from_thread(self._set_reg_state, rid, vals)
            except (VNTimeout, VNError) as exc:
                self.call_from_thread(self._set_reg_state, rid,
                                      exc.code if isinstance(exc, VNError) else "timeout")
                errors += 1
                if show:
                    self.call_from_thread(self.notify, f"Reg {rid}: {exc}", severity="error")
        if show and self.cur_reg and self.cur_reg.id in ids and self.cur_reg.id in self.reg_cache:
            self.call_from_thread(self._set_form_values, self.reg_cache[self.cur_reg.id])
        if not show:
            self.call_from_thread(self.notify, f"Leidos {len(ids) - errors}/{len(ids)} registros")
            if self.cur_reg and self.cur_reg.id in self.reg_cache:
                self.call_from_thread(self._set_form_values, self.reg_cache[self.cur_reg.id])

    def _validated_form(self) -> Optional[List[str]]:
        reg = self.cur_reg
        if not reg:
            return None
        try:
            return R.validate_values(reg, self._form_values())
        except ValueError as exc:
            self.notify(f"Valor invalido: {exc}", severity="error", timeout=6)
            return None

    @on(Button.Pressed, "#btn-write")
    def _write_btn(self) -> None:
        vals = self._validated_form()
        if vals is not None:
            self.run_writes({self.cur_reg.id: vals}, save=False, label=f"Reg {self.cur_reg.id}")

    @on(Button.Pressed, "#btn-write-flash")
    def _write_flash_btn(self) -> None:
        vals = self._validated_form()
        if vals is None:
            return
        rid = self.cur_reg.id

        def go(ok):
            if ok:
                self.run_writes({rid: vals}, save=True, label=f"Reg {rid}")
        self.push_screen(Confirm("Escribir y guardar en flash",
                                 f"$VNWRG,{rid},{','.join(vals)}\n\nY despues $VNWNV (guardar en flash).",
                                 "Escribir + flash"), go)

    @work(thread=True, group="regs")
    def run_writes(self, items: Dict[int, List[str]], save: bool, label: str = "") -> None:
        if not self.dev.connected:
            self.call_from_thread(self.notify, "No conectado", severity="warning")
            return
        pb = self.query_one("#batch-progress", ProgressBar)

        def prog(k, total, msg):
            def upd():
                pb.display = True
                pb.update(total=total, progress=k)
                if k >= total:
                    self.set_timer(1.5, lambda: setattr(pb, "display", False))
            self.call_from_thread(upd)
        try:
            res = self.dev.flash_many(items, save=save, progress=prog if len(items) > 1 or save else None)
        except Exception as exc:
            self.call_from_thread(self.notify, f"Error: {exc}", severity="error", timeout=8)
            return
        bad = {rid: r for rid, r in res.items() if isinstance(r, Exception)}
        for rid, r in res.items():
            if not isinstance(r, Exception):
                self.reg_cache[rid] = r
                self.call_from_thread(self._set_reg_state, rid, r)
        if bad:
            msg = "\n".join(f"Reg {rid}: {e}" for rid, e in bad.items())
            self.call_from_thread(self.notify, f"Fallaron {len(bad)} escrituras:\n{msg}", severity="error", timeout=12)
        ok = len(res) - len(bad)
        if ok:
            self.call_from_thread(self.notify, f"{label}: {ok} registro(s) escritos" +
                                  (" y guardados en flash" if save else " (en RAM; Ctrl+S para guardar)"))
        if self.cur_reg and self.cur_reg.id in res and self.cur_reg.id not in bad:
            self.call_from_thread(self._set_form_values, self.reg_cache[self.cur_reg.id])
        if 5 in res:
            self.info = self.dev.device_info()
            self.call_from_thread(self._render_info)

    @on(Button.Pressed, "#btn-batch-add")
    def _batch_add(self) -> None:
        vals = self._validated_form()
        if vals is None:
            return
        self.batch[self.cur_reg.id] = vals
        self._render_batch()
        self.notify(f"Reg {self.cur_reg.id} agregado al lote ({len(self.batch)} en total)")

    def _render_batch(self) -> None:
        t = self.query_one("#batch-table", DataTable)
        t.clear()
        for rid, vals in sorted(self.batch.items()):
            t.add_row(str(rid), R.get(rid).name, ",".join(vals), key=str(rid))

    @on(Button.Pressed, "#btn-batch-del")
    def _batch_del(self) -> None:
        t = self.query_one("#batch-table", DataTable)
        if t.row_count == 0:
            return
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        self.batch.pop(int(key), None)
        self._render_batch()

    @on(Button.Pressed, "#btn-batch-clear")
    def _batch_clear(self) -> None:
        self.batch.clear()
        self._render_batch()

    @on(Button.Pressed, "#btn-batch-flash")
    def _batch_flash(self) -> None:
        if not self.batch:
            self.notify("El lote esta vacio. Usa '+ Lote' o 'Importar config'.", severity="warning")
            return
        body = "\n".join(f"$VNWRG,{rid},{','.join(v)}" for rid, v in sorted(self.batch.items()))
        items = dict(self.batch)

        def go(ok):
            if ok:
                self.run_writes(items, save=True, label="Lote")
        self.push_screen(Confirm(f"Flashear {len(items)} registros",
                                 body + "\n\n+ $VNWNV (guardar en flash)", "Flashear", danger=True), go)

    @on(Button.Pressed, "#btn-batch-save")
    def _batch_save(self) -> None:
        if not self.batch:
            self.notify("El lote esta vacio", severity="warning")
            return
        os.makedirs(CONFIG_DIR, exist_ok=True)
        default = os.path.join(CONFIG_DIR, f"lote_{datetime.now():%Y%m%d_%H%M%S}.json")

        def go(path):
            if path:
                self._save_json(path, self.batch, "lote")
        self.push_screen(AskPath("Guardar lote como…", default), go)

    def _save_json(self, path: str, regs: Dict[int, List[str]], kind: str) -> None:
        data = {
            "tipo": kind, "fecha": datetime.now().isoformat(timespec="seconds"),
            "equipo": self.info,
            "registros": {str(rid): {"nombre": R.get(rid).name, "valores": vals}
                          for rid, vals in sorted(regs.items())},
        }
        try:
            with open(os.path.expanduser(path), "w") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            self.notify(f"Guardado {path} ({len(regs)} registros)")
        except OSError as exc:
            self.notify(f"No se pudo guardar: {exc}", severity="error")

    @on(Button.Pressed, "#btn-export")
    def _export(self) -> None:
        if not self.dev.connected:
            self.notify("Conecta primero para leer la configuracion", severity="warning")
            return
        os.makedirs(CONFIG_DIR, exist_ok=True)
        sn = self.info.get("serial", "vn300")
        default = os.path.join(CONFIG_DIR, f"config_{sn}_{datetime.now():%Y%m%d_%H%M%S}.json")

        def go(path):
            if path:
                self.export_config(path)
        self.push_screen(AskPath("Exportar todos los registros configurables a…", default), go)

    @work(thread=True, group="regs")
    def export_config(self, path: str) -> None:
        ids = [r.id for r in R.REGISTERS if r.writable and r.id not in (33,)]
        res = self.dev.read_many(ids)
        regs = {rid: v for rid, v in res.items() if not isinstance(v, Exception)}
        self.reg_cache.update(regs)
        self.call_from_thread(self._save_json, path, regs, "config")

    @on(Button.Pressed, "#btn-import")
    def _import(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)

        def go(path):
            if not path:
                return
            try:
                with open(os.path.expanduser(path)) as fh:
                    data = json.load(fh)
                regs = data.get("registros", data)
                n = 0
                for rid, entry in regs.items():
                    vals = entry["valores"] if isinstance(entry, dict) else entry
                    reg = R.get(int(rid))
                    if not reg.writable:
                        continue
                    self.batch[int(rid)] = R.validate_values(reg, [str(x) for x in vals])
                    n += 1
                self._render_batch()
                self.notify(f"{n} registros cargados en el lote. Revisalos y pulsa 'Flashear lote'.")
            except Exception as exc:
                self.notify(f"No se pudo importar: {exc}", severity="error", timeout=8)
        files = sorted(f for f in os.listdir(CONFIG_DIR) if f.endswith(".json"))
        default = os.path.join(CONFIG_DIR, files[-1]) if files else os.path.join(CONFIG_DIR, "config.json")
        self.push_screen(AskPath("Importar configuracion (JSON) al lote", default), go)

    @on(Button.Pressed, "#btn-bin-wizard")
    def _bin_wizard_from_regs(self) -> None:
        if self.cur_reg and self.cur_reg.id in (75, 76, 77):
            self._open_bin_wizard(self.cur_reg.id, self._form_values(), to_form=True)

    def _open_bin_wizard(self, rid: int, current: List[str], to_form: bool) -> None:
        def go(vals):
            if not vals:
                return
            if to_form:
                self._set_form_values(vals)
                self.notify("Valores cargados en el formulario. Pulsa 'Escribir' o 'Escribir + flash'.")
            else:
                self.query_one("#bin-out", Static).update(f"$VNWRG,{rid},{','.join(vals)}")
                self.run_writes({rid: vals}, save=False, label=f"Salida binaria {rid}")
        self.push_screen(BinaryWizard(rid, current), go)

    # ------------------------------------------------------------------ #
    #  Consola
    # ------------------------------------------------------------------ #
    @on(Switch.Changed, "#sw-async-console")
    def _sw_async(self, ev: Switch.Changed) -> None:
        self.show_async_in_console = ev.value
        self.dev.show_background = ev.value

    @on(Button.Pressed, "#btn-console-clear")
    def _console_clear(self) -> None:
        self.query_one("#console-log", RichLog).clear()

    @on(Input.Submitted, "#console-input")
    def _console_send(self, ev: Input.Submitted) -> None:
        cmd = ev.value.strip()
        if not cmd:
            return
        ev.input.value = ""
        try:
            self.dev.send_raw(cmd)
        except Exception as exc:
            self.query_one("#console-log", RichLog).write(Text(f"!! {exc}", style="red"))

    def _drain_console(self) -> None:
        if not self.raw_q:
            return
        log = self.query_one("#console-log", RichLog)
        n = 0
        while self.raw_q and n < 300:
            d, t = self.raw_q.popleft()
            n += 1
            style = {"TX": "bold cyan", "RX": "green", "AS": "dim"}.get(d, "")
            line = Text(f"{datetime.now():%H:%M:%S.%f}"[:-3] + f" {d} ", style="dim")
            if "VNERR" in t:
                style = "bold red"
                try:
                    code = int(t.split(",")[1].split("*")[0], 16)
                    from .protocol import ERROR_CODES
                    t += f"   ← {ERROR_CODES.get(code, '?')}"
                except (IndexError, ValueError):
                    pass
            line.append(t, style=style)
            log.write(line)

    # ------------------------------------------------------------------ #
    #  Herramientas
    # ------------------------------------------------------------------ #
    @on(Button.Pressed, "#btn-hsi-start")
    def _hsi_start(self) -> None:
        rate = self.query_one("#sel-hsi-rate", Select).value
        self.run_writes({44: ["1", "3", str(rate)]}, save=False, label="HSI iniciado")
        self.query_one("#hsi-out", Static).update("Calibrando… gira el equipo lentamente en todas las direcciones.")

    @on(Button.Pressed, "#btn-hsi-stop")
    def _hsi_stop(self) -> None:
        rate = self.query_one("#sel-hsi-rate", Select).value
        self.run_writes({44: ["0", "3", str(rate)]}, save=False, label="HSI detenido")

    @on(Button.Pressed, "#btn-hsi-reset")
    def _hsi_reset(self) -> None:
        rate = self.query_one("#sel-hsi-rate", Select).value
        self.run_writes({44: ["2", "3", str(rate)]}, save=False, label="HSI reiniciado")

    @on(Button.Pressed, "#btn-hsi-read")
    def _hsi_read(self) -> None:
        self.hsi_read()

    @work(thread=True, group="regs")
    def hsi_read(self) -> None:
        try:
            v = self.dev.read_register(47)
            ctl = self.dev.read_register(44)
        except Exception as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.reg_cache[47] = v
        t = Table(title="Solucion HSI (reg 47)", box=None)
        for h in ("", "C[.,0]", "C[.,1]", "C[.,2]", "B"):
            t.add_column(h, justify="right")
        for i in range(3):
            t.add_row(f"fila {i}", *v[i * 3:i * 3 + 3], v[9 + i])
        mode = {0: "OFF", 1: "RUN", 2: "RESET"}.get(int(ctl[0]), ctl[0])
        self.call_from_thread(self.query_one("#hsi-out", Static).update, t)
        self.call_from_thread(self.notify, f"HSI modo {mode}, salida {ctl[1]}, velocidad {ctl[2]}")

    @on(Button.Pressed, "#btn-hsi-copy")
    def _hsi_copy(self) -> None:
        v = self.reg_cache.get(47)
        if not v:
            self.notify("Primero 'Leer solucion'", severity="warning")
            return
        self.batch[23] = list(v)
        self._render_batch()
        self.notify("Solucion HSI agregada al lote como registro 23 (ve a F3 para flashear).")

    def _mount_matrix(self) -> Optional[List[float]]:
        try:
            y, p, r = (math.radians(float(self.query_one(f"#mnt-{k}", Input).value or 0))
                       for k in ("yaw", "pitch", "roll"))
        except ValueError:
            self.notify("Angulos invalidos", severity="error")
            return None
        cy, sy, cp, sp, cr, sr = math.cos(y), math.sin(y), math.cos(p), math.sin(p), math.cos(r), math.sin(r)
        # R (vehiculo -> sensor) = Rx(r) Ry(p) Rz(y);  C = R^T (sensor -> vehiculo)
        Rm = [[cp * cy, cp * sy, -sp],
              [sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, sr * cp],
              [cr * sp * cy + sr * sy, cr * sp * sy - sr * cy, cr * cp]]
        C = [[Rm[j][i] for j in range(3)] for i in range(3)]
        return [0.0 if abs(x) < 1e-9 else round(x, 6) for row in C for x in row]

    @on(Button.Pressed, "#btn-mnt-calc")
    def _mnt_calc(self) -> None:
        m = self._mount_matrix()
        if m:
            t = Table(box=None, title="C (sensor → vehiculo)")
            for _ in range(3):
                t.add_column(justify="right")
            for i in range(3):
                t.add_row(*[f"{x:+.6f}" for x in m[i * 3:i * 3 + 3]])
            self.query_one("#mnt-out", Static).update(t)

    @on(Button.Pressed, "#btn-mnt-write")
    def _mnt_write(self) -> None:
        m = self._mount_matrix()
        if not m:
            return
        vals = [f"{x:.6f}" for x in m]

        def go(ok):
            if ok:
                self.mount_write(vals)
        self.push_screen(Confirm("Escribir matriz de montaje",
                                 f"$VNWRG,26,{','.join(vals)}\n+ $VNWNV + $VNRST\n\n"
                                 "El equipo se reiniciara y el filtro volvera a converger.",
                                 "Aplicar", danger=True), go)

    @work(thread=True, group="regs")
    def mount_write(self, vals: List[str]) -> None:
        try:
            self.dev.write_register(26, vals)
            self.dev.write_settings()
            self.dev.reset()
            self.call_from_thread(self.notify, "Matriz de montaje aplicada (guardada y reiniciado)")
        except Exception as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=8)

    @on(Button.Pressed, "#btn-sgb")
    @on(Button.Pressed, "#btn-sfb")
    def _bias(self, ev: Button.Pressed) -> None:
        gyro = ev.button.id == "btn-sgb"
        cmd = "$VNSGB (Set Gyro Bias)" if gyro else "$VNSFB (Set Filter Bias)"

        def go(ok):
            if ok:
                self.bias_save(gyro)
        self.push_screen(Confirm("Guardar bias de arranque",
                                 f"{cmd} copia los bias estimados al registro 74 y despues $VNWNV los "
                                 "guarda en flash.\n\nEl equipo debe estar quieto y con el filtro convergido.",
                                 "Guardar"), go)

    @work(thread=True, group="regs")
    def bias_save(self, gyro: bool) -> None:
        try:
            (self.dev.set_gyro_bias if gyro else self.dev.set_filter_bias)()
            self.dev.write_settings()
            vals = self.dev.read_register(74)
        except Exception as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=8)
            return
        self.reg_cache[74] = vals
        self.call_from_thread(self._show_bias, vals)
        self.call_from_thread(self.notify, "Bias copiado al registro 74 y guardado en flash")

    @on(Button.Pressed, "#btn-read74")
    def _read74(self) -> None:
        self.read74()

    @work(thread=True, group="regs")
    def read74(self) -> None:
        try:
            vals = self.dev.read_register(74)
        except Exception as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.call_from_thread(self._show_bias, vals)

    def _show_bias(self, vals: List[str]) -> None:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold")
        t.add_column(justify="right")
        t.add_column(style="dim")
        for f, v in zip(R.BY_ID[74].fields, vals):
            t.add_row(f.name, v, f.unit)
        self.query_one("#bias-out", Static).update(t)

    @on(Button.Pressed, "#btn-binwiz-75")
    @on(Button.Pressed, "#btn-binwiz-76")
    @on(Button.Pressed, "#btn-binwiz-77")
    def _binwiz_tools(self, ev: Button.Pressed) -> None:
        rid = int(ev.button.id.rsplit("-", 1)[1])
        self.binwiz_load(rid)

    @work(thread=True, group="regs")
    def binwiz_load(self, rid: int) -> None:
        cur = self.reg_cache.get(rid, ["1", "8", "1", "0"])
        if self.dev.connected:
            try:
                cur = self.dev.read_register(rid)
                self.reg_cache[rid] = cur
            except (VNTimeout, VNError):
                pass
        self.call_from_thread(self._open_bin_wizard, rid, cur, False)

    # ------------------------------------------------------------------ #
    def on_unmount(self) -> None:
        self._stop_polling()
        self.live.stop_csv()
        self.dev.close()
