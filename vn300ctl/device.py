"""
Conexion con el VN-300: hilo lector, comandos ASCII con respuesta y
decodificacion de los mensajes asincronos (ASCII y binarios).
"""

from __future__ import annotations

from .i18n import _

import queue
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional

from . import registers as R
from .protocol import (ERROR_CODES, AsciiMsg, BinaryMsg, build_ascii,
                       parse_ascii, try_parse_binary)

# Orden de prueba para la autodeteccion (los mas habituales primero).
AUTOBAUD_ORDER = [115200, 921600, 230400, 460800, 128000, 57600, 38400, 19200, 9600]

# Comandos del firmware v0.5.0.0 (manual UM005 rev. 2.22, secciones 6.1, 9.1 y 10.1)
RESPONSE_HEADERS = {"VNRRG", "VNWRG", "VNWNV", "VNRFS", "VNRST", "VNASY",
                    "VNERR", "VNCMD", "VNFWU", "VNSGB", "VNSFB"}


class VNError(Exception):
    def __init__(self, code: int, cmd: str = ""):
        self.code = code
        txt = ERROR_CODES.get(code, _("desconocido"))
        super().__init__(f"VNERR {code}: {txt}" + (f"  [{cmd}]" if cmd else ""))


class VNTimeout(Exception):
    pass


def list_ports() -> List[str]:
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    out = []
    for p in sorted(lp.comports(), key=lambda p: p.device):
        desc = p.description if p.description and p.description != "n/a" else ""
        out.append(p.device + (f"  ({desc})" if desc else ""))
    return out


class RateMeter:
    """Frecuencia de llegada por tipo de mensaje (ventana de 2 s)."""

    def __init__(self, window: float = 2.0):
        self.window = window
        self.t: Dict[str, Deque[float]] = {}

    def tick(self, key: str) -> None:
        now = time.monotonic()
        dq = self.t.setdefault(key, deque())
        dq.append(now)
        while dq and now - dq[0] > self.window:
            dq.popleft()

    def rates(self) -> Dict[str, float]:
        now = time.monotonic()
        out = {}
        for k, dq in self.t.items():
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= 2:
                span = dq[-1] - dq[0]
                out[k] = (len(dq) - 1) / span if span > 0 else 0.0
            elif dq:
                out[k] = 1.0 / self.window
        return out


class VN300:
    def __init__(self) -> None:
        self.ser = None
        self.port: str = ""
        self.baud: int = 0
        self.use_crc = False
        self._rx = bytearray()
        self._resp: "queue.Queue[AsciiMsg]" = queue.Queue()
        self._cmd_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._quiet = False              # comando de fondo: no mostrar en consola
        self.show_background = False

        self.on_ascii: Optional[Callable[[AsciiMsg], None]] = None
        self.on_binary: Optional[Callable[[BinaryMsg], None]] = None
        self.on_raw: Optional[Callable[[str, str], None]] = None   # (dir, texto)
        self.on_disconnect: Optional[Callable[[str], None]] = None

        self.rates = RateMeter()
        self.stats = {"bytes": 0, "ascii": 0, "binary": 0, "bad": 0, "errors": 0}

    # ------------------------------------------------------------------ #
    #  Apertura / cierre
    # ------------------------------------------------------------------ #

    @property
    def connected(self) -> bool:
        return self.ser is not None

    def open(self, port: str, baud: int) -> None:
        self.close()
        if port.startswith("sim"):
            from .sim import SimulatedVN300
            self.ser = SimulatedVN300(baud)
        else:
            import serial
            self.ser = serial.Serial(port, baud, timeout=0.05, write_timeout=1.0)
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
        self.port, self.baud = port, baud
        self._rx.clear()
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="vn300-rx", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def set_baud(self, baud: int) -> None:
        if self.ser is not None:
            self.ser.baudrate = baud
            self.baud = baud
            self._rx.clear()

    def autodetect(self, port: str, order: Optional[List[int]] = None) -> int:
        """Prueba baudrates hasta que el equipo responde a $VNRRG,1. Devuelve el baud."""
        for baud in order or AUTOBAUD_ORDER:
            try:
                self.open(port, baud)
            except Exception:
                self.close()
                raise
            try:
                self.read_register(1, timeout=0.35, retries=1)
                return baud
            except (VNTimeout, VNError):
                continue
        self.close()
        raise VNTimeout(_("El equipo no responde en {0} con ningun baudrate").format(port))

    # ------------------------------------------------------------------ #
    #  Lectura
    # ------------------------------------------------------------------ #

    def _reader(self) -> None:
        ser = self.ser
        while not self._stop.is_set():
            try:
                n = ser.in_waiting
                chunk = ser.read(n if n else 1)
            except Exception as exc:          # cable desconectado, etc.
                if not self._stop.is_set():
                    self.ser = None
                    if self.on_disconnect:
                        self.on_disconnect(str(exc))
                return
            if not chunk:
                continue
            self.stats["bytes"] += len(chunk)
            self._rx.extend(chunk)
            self._drain()

    def _drain(self) -> None:
        buf = self._rx
        i = 0
        n = len(buf)
        while i < n:
            b = buf[i]
            if b == 0x24:                       # '$'
                end = buf.find(b"\n", i)
                nxt = buf.find(b"$VN", i + 1)
                if end < 0:
                    if nxt > 0:                 # linea cortada por otra
                        self.stats["bad"] += 1
                        i = nxt
                        continue
                    if n - i > 512:             # basura sin fin de linea
                        i += 1
                        continue
                    break
                if 0 < nxt < end:
                    self.stats["bad"] += 1
                    i = nxt
                    continue
                line = bytes(buf[i:end + 1])
                msg = parse_ascii(line)
                if msg is None:
                    self.stats["bad"] += 1
                    i += 1
                    continue
                i = end + 1
                self._handle_ascii(msg)
            elif b == 0xFA:
                msg, ln = try_parse_binary(buf, i)
                if ln == 0:                     # faltan bytes
                    if n - i > 4096:
                        i += 1
                        continue
                    break
                if msg is None:
                    i += 1
                    continue
                i += ln
                self.stats["binary"] += 1
                self.rates.tick("BIN")
                if self.on_binary:
                    try:
                        self.on_binary(msg)
                    except Exception:
                        pass
            else:
                i += 1
        del buf[:i]

    def _handle_ascii(self, msg: AsciiMsg) -> None:
        self.stats["ascii"] += 1
        if msg.header in RESPONSE_HEADERS:
            if msg.header == "VNERR":
                self.stats["errors"] += 1
            if self.on_raw and (not self._quiet or self.show_background):
                self.on_raw("RX", msg.raw)
            self._resp.put(msg)
            return
        self.rates.tick(msg.header)
        if self.on_ascii:
            try:
                self.on_ascii(msg)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Comandos
    # ------------------------------------------------------------------ #

    def send_raw(self, body: str) -> None:
        """Envia un comando sin esperar respuesta (consola)."""
        if self.ser is None:
            raise VNTimeout(_("No conectado"))
        body = body.strip().lstrip("$")
        if "*" in body:
            body = body.split("*", 1)[0]
        if not body.upper().startswith("VN"):
            body = "VN" + body
        frame = build_ascii(body, self.use_crc)
        if self.on_raw:
            self.on_raw("TX", frame.decode().strip())
        self.ser.write(frame)

    def command(self, body: str, expect: str, match_id: Optional[int] = None,
                timeout: float = 1.0, retries: int = 2, quiet: bool = False) -> AsciiMsg:
        if self.ser is None:
            raise VNTimeout(_("No conectado"))
        with self._cmd_lock:
            self._quiet = quiet
            try:
                return self._command(body, expect, match_id, timeout, retries)
            finally:
                self._quiet = False

    def _command(self, body: str, expect: str, match_id: Optional[int],
                 timeout: float, retries: int) -> AsciiMsg:
        frame = build_ascii(body, self.use_crc)
        last: Exception = VNTimeout(_("Sin respuesta a {0}").format(body))
        for _attempt in range(retries + 1):
            while not self._resp.empty():
                try:
                    self._resp.get_nowait()
                except queue.Empty:
                    break
            if self.on_raw and (not self._quiet or self.show_background):
                self.on_raw("TX", frame.decode().strip())
            try:
                self.ser.write(frame)
            except Exception as exc:
                raise VNTimeout(_("Error escribiendo: {0}").format(exc))
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    msg = self._resp.get(timeout=left)
                except queue.Empty:
                    break
                if msg.header == "VNERR":
                    code = int(msg.fields[0], 16) if msg.fields else -1
                    if code == 3 and not self.use_crc:
                        # Quiza el equipo espera CRC16: reintentar con CRC
                        self.use_crc = True
                        frame = build_ascii(body, True)
                        last = VNError(code, body)
                        break
                    raise VNError(code, body)
                if msg.header != expect:
                    continue
                if match_id is not None:
                    try:
                        if not msg.fields or int(msg.fields[0]) != match_id:
                            continue
                    except ValueError:
                        continue
                if len(msg.raw.split("*")[-1]) == 4:
                    self.use_crc = True
                return msg
        raise last

    def read_register(self, rid: int, timeout: float = 1.0, retries: int = 2,
                      quiet: bool = False) -> List[str]:
        msg = self.command(f"VNRRG,{rid}", "VNRRG", rid, timeout, retries, quiet)
        return msg.fields[1:]

    def write_register(self, rid: int, values: List[str], timeout: float = 1.5) -> List[str]:
        body = f"VNWRG,{rid}" + ("," + ",".join(values) if values else "")
        msg = self.command(body, "VNWRG", rid, timeout, retries=1)
        if rid == 5 and values:
            self._follow_baud_change(int(values[0]))
        return msg.fields[1:]

    def _follow_baud_change(self, new_baud: int) -> None:
        """Tras escribir el reg 5, comprobar a que velocidad quedo nuestro puerto."""
        old = self.baud
        time.sleep(0.15)
        for b in (new_baud, old):
            self.set_baud(b)
            time.sleep(0.05)
            try:
                self.read_register(1, timeout=0.5, retries=1)
                return
            except (VNTimeout, VNError):
                continue
        self.set_baud(old)

    def write_settings(self) -> None:
        """Guarda la configuracion actual en flash ($VNWNV). Tarda ~500 ms."""
        self.command("VNWNV", "VNWNV", timeout=3.0, retries=1)

    def restore_factory(self) -> None:
        self.command("VNRFS", "VNRFS", timeout=3.0, retries=0)
        self._after_reset()

    def reset(self) -> None:
        self.command("VNRST", "VNRST", timeout=2.0, retries=0)
        self._after_reset()

    def _after_reset(self) -> None:
        """Tras un reset el equipo puede volver al baud guardado: re-sincronizar."""
        time.sleep(1.5)
        try:
            self.read_register(1, timeout=0.5, retries=2)
            return
        except (VNTimeout, VNError):
            pass
        port = self.port
        cbs = (self.on_ascii, self.on_binary, self.on_raw, self.on_disconnect)
        self.autodetect(port)
        self.on_ascii, self.on_binary, self.on_raw, self.on_disconnect = cbs

    def set_gyro_bias(self) -> None:
        """$VNSGB: copia la estimacion actual del bias del giroscopo al registro 74.
        Hay que guardar en flash despues para que se use al arrancar."""
        self.command("VNSGB", "VNSGB", timeout=1.5, retries=1)

    def set_filter_bias(self) -> None:
        """$VNSFB: copia los bias estimados por el filtro INS al registro 74
        (Startup Filter Bias Estimate). Guardar en flash despues."""
        self.command("VNSFB", "VNSFB", timeout=1.5, retries=1)

    def firmware_matches(self, fw: str) -> bool:
        return fw.strip() == R.FIRMWARE

    def async_pause(self, pause: bool) -> None:
        self.command(f"VNASY,{0 if pause else 1}", "VNASY", timeout=1.0)

    def poll_binary(self, n: int) -> None:
        """$VNBOM,n: pide un paquete binario del registro 75+n-1."""
        self.send_raw(f"VNBOM,{n}")

    # ------------------------------------------------------------------ #
    #  Conveniencia
    # ------------------------------------------------------------------ #

    def device_info(self) -> Dict[str, str]:
        info = {}
        for rid, key in ((1, "model"), (2, "hw"), (3, "serial"), (4, "fw"), (0, "tag")):
            try:
                info[key] = ",".join(self.read_register(rid))
            except (VNTimeout, VNError) as exc:
                info[key] = f"? ({exc})"
        return info

    def read_many(self, ids: List[int]) -> Dict[int, object]:
        out: Dict[int, object] = {}
        for rid in ids:
            try:
                out[rid] = self.read_register(rid)
            except (VNTimeout, VNError) as exc:
                out[rid] = exc
        return out

    def flash_many(self, items: Dict[int, List[str]], save: bool = True,
                   progress: Optional[Callable[[int, int, str], None]] = None) -> Dict[int, object]:
        """
        Escribe varios registros y (opcional) los guarda en flash.
        El registro 5 (baudrate) se escribe al final para no perder la conexion
        a mitad de la secuencia.
        """
        # Un User Tag vacio no se puede enviar por ASCII: se omite
        items = {r: v for r, v in items.items() if not (r == 0 and v in ([], [""]))}
        order = sorted(items, key=lambda r: (r == 5, r))
        out: Dict[int, object] = {}
        total = len(order) + (1 if save else 0)
        for k, rid in enumerate(order):
            vals = R.validate_values(R.get(rid), items[rid])
            if progress:
                progress(k, total, _("Escribiendo reg {0}").format(rid))
            try:
                out[rid] = self.write_register(rid, vals)
            except (VNTimeout, VNError) as exc:
                out[rid] = exc
        if save:
            if progress:
                progress(total - 1, total, _("Guardando en flash (WNV)"))
            self.write_settings()
        if progress:
            progress(total, total, _("Listo"))
        return out
