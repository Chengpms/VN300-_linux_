"""
VN-300 simulado (puerto 'sim'). Responde a $VNRRG/$VNWRG/$VNWNV/... y genera
salida asincrona ASCII y binaria segun los registros 6/7 y 75-77, para poder
probar la aplicacion sin el equipo conectado.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from typing import Dict, List

from . import registers as R
from .protocol import (BIN_NAMES, BIN_SIZES, _FMT, build_ascii, crc16,
                       parse_ascii)

DEFAULTS: Dict[int, List[str]] = {
    0: ["SIMULADOR"], 1: ["VN-300T-SIM"], 2: ["2"], 3: ["0100099999"], 4: ["0.5.0.0"],
    5: ["115200"], 6: ["14"], 7: ["40"],
    30: ["0", "0", "0", "0", "1", "0", "1"],
    32: ["3", "0", "0", "0", "0", "1", "0", "100000000", "0"],
    33: ["0", "0", "0"],
    75: ["0", "8", "1", "1029"], 76: ["0", "0", "0"], 77: ["0", "0", "0"],
    101: ["0", "1", "0", "0", "0"], 102: ["0", "1", "0", "0", "0"],
    23: ["1", "0", "0", "0", "1", "0", "0", "0", "1", "0", "0", "0"],
    25: ["1", "0", "0", "0", "1", "0", "0", "0", "1", "0", "0", "0"],
    84: ["1", "0", "0", "0", "1", "0", "0", "0", "1", "0", "0", "0"],
    26: ["1", "0", "0", "0", "1", "0", "0", "0", "1"],
    85: ["0", "4", "4", "4", "0", "0", "3", "3", "3", "0"],
    82: ["0", "0", "0", "0", "0"],
    55: ["0", "0", "5", "0", "1"], 57: ["0", "0", "0"],
    93: ["1", "0", "0", "0.0254", "0.0254", "0.0254"],
    67: ["1", "1", "1", "0"],
    74: ["0", "0", "0", "0", "0", "0", "0"],
    44: ["0", "3", "5"], 21: ["1", "0", "1.8", "0", "0", "-9.79375"],
    83: ["0", "0", "0", "0", "1000", "2025.0", "0", "0", "0"],
}


class SimulatedVN300:
    """Imita la interfaz minima de serial.Serial que usa VN300."""

    def __init__(self, baud: int = 115200):
        self._baud = baud
        self.timeout = 0.05
        self._out = bytearray()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._regs = {k: list(v) for k, v in DEFAULTS.items()}
        self._flash = {k: list(v) for k, v in self._regs.items()}
        self._dev_baud = int(self._regs[5][0])
        self._paused = False
        self._closed = False
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # --------------------------- interfaz serial --------------------------- #
    @property
    def baudrate(self) -> int:
        return self._baud

    @baudrate.setter
    def baudrate(self, v: int) -> None:
        self._baud = v

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._out)

    def read(self, n: int = 1) -> bytes:
        with self._cv:
            if not self._out:
                self._cv.wait(self.timeout)
            if self._baud != self._dev_baud:
                # Baud distinto: el host solo veria basura
                garbage = bytes((b ^ 0x5A) | 0x80 for b in self._out[:n])
                del self._out[:n]
                return garbage
            data = bytes(self._out[:n])
            del self._out[:n]
            return data

    def write(self, data: bytes) -> int:
        if self._baud != self._dev_baud:
            return len(data)
        for line in data.split(b"\n"):
            if line.strip():
                self._handle(line + b"\n")
        return len(data)

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._out.clear()

    def close(self) -> None:
        self._closed = True

    # ------------------------------ logica -------------------------------- #
    def _emit(self, data: bytes) -> None:
        with self._cv:
            self._out.extend(data)
            if len(self._out) > 200_000:
                del self._out[:-100_000]
            self._cv.notify_all()

    def _reply(self, body: str) -> None:
        self._emit(build_ascii(body))

    def _err(self, code: int) -> None:
        self._reply(f"VNERR,{code:02X}")

    def _handle(self, line: bytes) -> None:
        msg = parse_ascii(line)
        if msg is None:
            self._err(3)
            return
        h, f = msg.header, msg.fields
        if h == "VNRRG":
            try:
                rid = int(f[0])
            except (IndexError, ValueError):
                return self._err(5)
            vals = self._live(rid)
            if vals is None:
                return self._err(8)
            self._reply(f"VNRRG,{rid:02d}," + ",".join(vals))
        elif h == "VNWRG":
            try:
                rid = int(f[0])
            except (IndexError, ValueError):
                return self._err(5)
            reg = R.BY_ID.get(rid)
            if reg is None or not reg.writable:
                return self._err(8)
            try:
                vals = R.validate_values(reg, f[1:])
            except ValueError:
                return self._err(7)
            self._reply(f"VNWRG,{rid:02d}," + ",".join(vals))
            if rid in (5, 6, 7) and len(vals) > 1:
                vals = vals[:1]
            self._regs[rid] = vals
            if rid == 5:
                time.sleep(0.02)
                self._dev_baud = int(vals[0])
        elif h == "VNWNV":
            time.sleep(0.3)
            self._flash = {k: list(v) for k, v in self._regs.items()}
            self._reply("VNWNV")
        elif h == "VNRFS":
            self._reply("VNRFS")
            self._regs = {k: list(v) for k, v in DEFAULTS.items()}
            self._flash = {k: list(v) for k, v in self._regs.items()}
            self._dev_baud = int(self._regs[5][0])
        elif h == "VNRST":
            self._reply("VNRST")
            self._regs = {k: list(v) for k, v in self._flash.items()}
            self._dev_baud = int(self._regs[5][0])
            self._t0 = time.monotonic()
        elif h in ("VNSGB", "VNSFB"):
            self._regs[74] = ["+00.000100", "-00.000200", "+00.000050", "+00.010", "-00.005", "+00.002", "+00000.000"]
            self._reply(h)
        elif h == "VNASY":
            self._paused = (f[:1] == ["0"])
            self._reply("VNASY," + ",".join(f))
        elif h == "VNBOM":
            try:
                n = int(f[0])
                self._emit(self._binary(75 + n - 1))
            except Exception:
                self._err(7)
        else:
            self._err(4)

    # ----------------------------- dinamica -------------------------------- #
    def _state(self):
        t = time.monotonic() - self._t0
        yaw = ((t * 12.0 + 180) % 360) - 180
        pitch = 8 * math.sin(t * 0.7)
        roll = 15 * math.sin(t * 0.45)
        gyro = (math.radians(15 * 0.45 * math.cos(t * 0.45)),
                math.radians(8 * 0.7 * math.cos(t * 0.7)), math.radians(12.0))
        cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
        cr, sr = math.cos(math.radians(roll)), math.sin(math.radians(roll))
        acc = (9.81 * sp, -9.81 * cp * sr, -9.81 * cp * cr)
        mag = (0.21 * math.cos(math.radians(yaw)), -0.21 * math.sin(math.radians(yaw)), 0.43)
        lat = 40.4168 + 0.0005 * math.sin(t * 0.05)
        lon = -3.7038 + 0.0005 * math.cos(t * 0.05)
        pct = min(100, int(t * 4))
        mode = 0 if t < 8 else (1 if t < 20 else 2)
        status = mode | (1 << 2) | ((1 << 8) if mode == 2 else 0) | ((1 << 9) if t > 8 else 0)
        return dict(t=t, yaw=yaw, pitch=pitch, roll=roll, gyro=gyro, acc=acc, mag=mag,
                    lat=lat, lon=lon, alt=655.0 + math.sin(t * 0.1), pct=pct, status=status,
                    temp=31.5 + 0.2 * math.sin(t / 30), pres=94.1,
                    vel=(0.2 * math.cos(t * 0.05), -0.2 * math.sin(t * 0.05), 0.0))

    def _live(self, rid: int):
        if rid in self._regs:
            return self._regs[rid]
        s = self._state()
        ypr = [f"{s['yaw']:+08.3f}", f"{s['pitch']:+07.3f}", f"{s['roll']:+08.3f}"]
        mag = [f"{v:+.4f}" for v in s["mag"]]
        acc = [f"{v:+07.3f}" for v in s["acc"]]
        gyr = [f"{v:+.6f}" for v in s["gyro"]]
        tow = f"{(345600 + s['t']):.6f}"
        gnss = [tow, "2345", "3", "14", f"{s['lat']:+.8f}", f"{s['lon']:+.8f}", f"{s['alt']:+.3f}",
                *[f"{v:+.3f}" for v in s["vel"]], "+1.200", "+1.100", "+2.300", "+0.150", "2.0E-08"]
        qy = math.radians(s["yaw"]) / 2
        q = ["+0.000000", "+0.000000", f"{math.sin(qy):+.6f}", f"{math.cos(qy):+.6f}"]
        table = {
            8: ypr, 9: q, 27: ypr + mag + acc + gyr, 15: q + mag + acc + gyr,
            17: mag, 18: acc, 19: gyr, 20: mag + acc + gyr,
            54: mag + acc + gyr + [f"{s['temp']:+.1f}", f"{s['pres']:+.3f}"],
            80: ["+0.025", "+0.01", "+0.01", "+0.3", "+0.0", "+0.0", "-0.245"],
            58: gnss, 103: gnss[:2] + ["3", "13"] + gnss[4:],
            59: [tow, "2345", "3", "14", "+4849202.0", "-293581.0", "+4114913.0",
                 "+0.1", "+0.1", "+0.0", "+1.2", "+1.2", "+2.3", "+0.15", "2.0E-08"],
            104: [tow, "2345", "3", "13", "+4849202.0", "-293581.0", "+4114913.0",
                  "+0.1", "+0.1", "+0.0", "+1.2", "+1.2", "+2.3", "+0.15", "2.0E-08"],
            63: [tow, "2345", f"{s['status']:04X}", *ypr, f"{s['lat']:+.8f}", f"{s['lon']:+.8f}",
                 f"{s['alt']:+.3f}", *[f"{v:+.3f}" for v in s["vel"]], "+0.25", "+1.5", "+0.05"],
            64: [tow, "2345", f"{s['status']:04X}", *ypr, "+4849202.0", "-293581.0", "+4114913.0",
                 "+0.1", "+0.1", "+0.0", "+0.25", "+1.5", "+0.05"],
            72: ypr + [f"{s['lat']:+.8f}", f"{s['lon']:+.8f}", f"{s['alt']:+.3f}"]
                + [f"{v:+.3f}" for v in s["vel"]] + acc + gyr,
            73: ypr + ["+4849202.0", "-293581.0", "+4114913.0"] + ["+0.1", "+0.1", "+0.0"] + acc + gyr,
            98: [f"{s['pct']:03d}", f"{s['yaw']:+08.3f}"],
            86: ["15", "12", "48", "14", "12", "47", "13", "11"],
            47: ["1", "0", "0", "0", "1", "0", "0", "0", "1", "0", "0", "0"],
        }
        return table.get(rid)

    def _binary(self, rid: int) -> bytes:
        regs = self._regs.get(rid, ["0", "0", "0"])
        groups = int(regs[2], 16)
        words = [int(w, 16) for w in regs[3:]]
        s = self._state()
        vals = {
            "YawPitchRoll": (s["yaw"], s["pitch"], s["roll"]),
            "AngularRate": s["gyro"], "Accel": s["acc"], "Mag": s["mag"],
            "UncompAccel": s["acc"], "UncompGyro": s["gyro"], "UncompMag": s["mag"],
            "Imu": (*s["acc"], *s["gyro"]), "MagPres": (*s["mag"], s["temp"], s["pres"]),
            "Temp": (s["temp"],), "Pres": (s["pres"],),
            "Position": (s["lat"], s["lon"], s["alt"]), "PosLla": (s["lat"], s["lon"], s["alt"]),
            "Velocity": s["vel"], "VelNed": s["vel"], "InsStatus": (s["status"],),
            "TimeStartup": (int(s["t"] * 1e9),), "TimeGps": (int((1.4e9 + s["t"]) * 1e9),),
            "NumSats": (14,), "Fix": (3,), "Week": (2345,), "Tow": (int((345600 + s["t"]) * 1e9),),
            "YprU": (0.3, 0.1, 0.1), "PosU": (1.5,), "VelU": (0.05,),
        }
        header = bytearray([groups])
        payload = bytearray()
        wi = 0
        for g in range(1, 8):
            if not groups & (1 << (g - 1)):
                continue
            w = words[wi] if wi < len(words) else 0
            wi += 1
            header += struct.pack("<H", w & 0x7FFF)
            for bit in range(15):
                if not w & (1 << bit) or bit >= len(BIN_SIZES[g]):
                    continue
                name = BIN_NAMES[g][bit]
                size = BIN_SIZES[g][bit] or 2
                fmt = _FMT.get((g, name))
                v = vals.get(name)
                if fmt and v is not None:
                    try:
                        payload += struct.pack(fmt, *v)
                        continue
                    except struct.error:
                        pass
                payload += bytes(size)
        body = bytes(header + payload)
        crc = crc16(body)
        return b"\xFA" + body + struct.pack(">H", crc)

    def _run(self) -> None:
        nxt_a = time.monotonic()
        nxt_b = {75: nxt_a, 76: nxt_a, 77: nxt_a}
        while not self._closed:
            now = time.monotonic()
            if not self._paused:
                ador = int(self._regs[6][0])
                adof = max(1, int(self._regs[7][0]))
                if ador and now >= nxt_a:
                    hdr = R.ADOR.get(ador)
                    reg = R.BY_HEADER.get("VN" + hdr) if hdr else None
                    vals = self._live(reg.id) if reg else None
                    if vals:
                        self._reply(f"VN{hdr}," + ",".join(vals))
                    nxt_a = now + 1.0 / adof
                for rid in (75, 76, 77):
                    mode, div = int(self._regs[rid][0]), int(self._regs[rid][1] or 1)
                    if mode and div and now >= nxt_b[rid]:
                        self._emit(self._binary(rid))
                        nxt_b[rid] = now + div / 400.0
            time.sleep(0.002)
