"""
Estado "en vivo" del VN-300: unifica lo que llega por ASCII asincrono,
por paquetes binarios o por sondeo de registros en un unico diccionario
con historico corto para las graficas.
"""

from __future__ import annotations

import csv
import math
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Sequence

from . import registers as R
from .protocol import AsciiMsg, BinaryMsg

HIST = 24000   # muestras por serie (60 s a 400 Hz)
SERIES = ("ypr", "gyro", "accel", "mag", "vel_ned", "vel_body")


def quat_to_ypr(q: Sequence[float]) -> List[float]:
    """Cuaternion VectorNav [x, y, z, w] (cuerpo respecto a NED) -> yaw, pitch, roll en grados."""
    x, y, z, w = q
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    return [math.degrees(yaw), math.degrees(pitch), math.degrees(roll)]


def _f(xs: Sequence[str]) -> List[float]:
    out = []
    for x in xs:
        try:
            out.append(float(x))
        except ValueError:
            out.append(math.nan)
    return out


class LiveState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.v: Dict[str, object] = {}
        self.t: Dict[str, float] = {}       # instante de la ultima actualizacion por clave
        # series para graficas: deque de (t, v0, v1, v2)
        self.series: Dict[str, Deque[tuple]] = {k: deque(maxlen=HIST) for k in SERIES}
        self.sources: Dict[str, str] = {}
        self._csv = None
        self._csv_file = None
        self.csv_path: Optional[str] = None
        self.csv_rows = 0

    # ------------------------------------------------------------------ #
    def _set(self, src: str, **kw) -> None:
        now = time.monotonic()
        for k, val in kw.items():
            self.v[k] = val
            self.t[k] = now
            self.sources[k] = src
            if k in self.series and val and len(val) >= 3:
                self.series[k].append((now, *val[:3]))

    def _ypr_from_quat(self, src: str) -> None:
        """Si el equipo manda cuaternion pero no YPR, derivar la actitud."""
        # Manda la YPR directa (binaria o asincrona) si es reciente; la consultada
        # por sondeo (RRG..) o la ya derivada del cuaternion se sustituyen.
        cur = self.sources.get("ypr", "")
        if cur.startswith("RRG") or cur.endswith("(q)") or self.age("ypr") > 0.3:
            q = self.v.get("quat")
            if q and not any(math.isnan(c) for c in q):
                self._set(src + "(q)", ypr=quat_to_ypr(q))

    def age(self, key: str) -> float:
        t = self.t.get(key)
        return time.monotonic() - t if t else math.inf

    def get(self, key: str, default=None):
        return self.v.get(key, default)

    # ------------------------------------------------------------------ #
    #  Ingesta ASCII (asincrono o respuesta de sondeo con la misma forma)
    # ------------------------------------------------------------------ #
    def ingest_register(self, rid: int, fields: Sequence[str], src: str) -> None:
        with self.lock:
            self._ingest(rid, list(fields), src)
            self._log_row()

    def ingest_ascii(self, msg: AsciiMsg) -> None:
        reg = R.BY_HEADER.get(msg.header)
        if reg is None:
            return
        self.ingest_register(reg.id, msg.fields, msg.header[2:])

    def _ingest(self, rid: int, f: List[str], src: str) -> None:
        x = _f
        if rid == 8 and len(f) >= 3:
            self._set(src, ypr=x(f[:3]))
        elif rid == 9 and len(f) >= 4:
            self._set(src, quat=x(f[:4]))
            self._ypr_from_quat(src)
        elif rid in (27,) and len(f) >= 12:
            self._set(src, ypr=x(f[:3]), mag=x(f[3:6]), accel=x(f[6:9]), gyro=x(f[9:12]))
        elif rid == 15 and len(f) >= 13:
            self._set(src, quat=x(f[:4]), mag=x(f[4:7]), accel=x(f[7:10]), gyro=x(f[10:13]))
            self._ypr_from_quat(src)
        elif rid == 17 and len(f) >= 3:
            self._set(src, mag=x(f[:3]))
        elif rid == 18 and len(f) >= 3:
            self._set(src, accel=x(f[:3]))
        elif rid == 19 and len(f) >= 3:
            self._set(src, gyro=x(f[:3]))
        elif rid == 20 and len(f) >= 9:
            self._set(src, mag=x(f[:3]), accel=x(f[3:6]), gyro=x(f[6:9]))
        elif rid == 54 and len(f) >= 11:
            self._set(src, mag_raw=x(f[:3]), accel_raw=x(f[3:6]), gyro_raw=x(f[6:9]),
                      temp=x(f[9:10])[0], pres=x(f[10:11])[0])
        elif rid in (58, 103) and len(f) >= 15:
            key = "gnss1" if rid == 58 else "gnss2"
            vals = x(f)
            self._set(src, **{key: {
                "tow": vals[0], "week": int(vals[1]), "fix": int(vals[2]), "sats": int(vals[3]),
                "lla": vals[4:7], "vel": vals[7:10], "acc": vals[10:13],
                "speed_acc": vals[13], "time_acc": vals[14]}})
        elif rid == 63 and len(f) >= 15:
            vals = x(f)
            try:
                st = int(f[2], 16)
            except ValueError:
                st = 0
            self._set(src, tow=vals[0], week=int(vals[1]), ins_status=st,
                      ypr=vals[3:6], lla=vals[6:9], vel_ned=vals[9:12],
                      att_unc=vals[12], pos_unc=vals[13], vel_unc=vals[14])
        elif rid == 72 and len(f) >= 15:
            vals = x(f)
            self._set(src, ypr=vals[0:3], lla=vals[3:6], vel_ned=vals[6:9],
                      accel=vals[9:12], gyro=vals[12:15])
        elif rid == 98 and len(f) >= 2:
            vals = x(f)
            self._set(src, compass_pct=vals[0], compass_heading=vals[1])
        elif rid == 86 and len(f) >= 8:
            self._set(src, health=x(f[:8]))
        elif rid == 80 and len(f) >= 7:
            self._set(src, dtv=x(f[:7]))

    # ------------------------------------------------------------------ #
    #  Ingesta binaria
    # ------------------------------------------------------------------ #
    def ingest_binary(self, msg: BinaryMsg) -> None:
        v = msg.values
        src = "BIN"
        with self.lock:
            def g(*keys):
                for k in keys:
                    if k in v:
                        return v[k]
                return None

            ypr = g((1, "YawPitchRoll"), (5, "YawPitchRoll"))
            if ypr:
                self._set(src, ypr=list(ypr))
            q = g((1, "Quaternion"), (5, "Quaternion"))
            if q:
                self._set(src, quat=list(q))
                if not ypr:
                    self._ypr_from_quat(src)
            w = g((1, "AngularRate"), (3, "AngularRate"))
            if w:
                self._set(src, gyro=list(w))
            a = g((1, "Accel"), (3, "Accel"))
            if a:
                self._set(src, accel=list(a))
            m = g((3, "Mag"))
            if m:
                self._set(src, mag=list(m))
            if (1, "MagPres") in v:
                mp = v[(1, "MagPres")]
                self._set(src, mag=list(mp[:3]), temp=mp[3], pres=mp[4])
            if (1, "Imu") in v:
                imu = v[(1, "Imu")]
                self._set(src, accel_raw=list(imu[:3]), gyro_raw=list(imu[3:6]))
            for key, name in (("accel_raw", "UncompAccel"), ("gyro_raw", "UncompGyro"),
                              ("mag_raw", "UncompMag")):
                if (3, name) in v:
                    self._set(src, **{key: list(v[(3, name)])})
            if (3, "Temp") in v:
                self._set(src, temp=v[(3, "Temp")][0])
            if (3, "Pres") in v:
                self._set(src, pres=v[(3, "Pres")][0])
            st = g((1, "InsStatus"), (6, "InsStatus"))
            if st:
                self._set(src, ins_status=st[0])
            pos = g((1, "Position"), (6, "PosLla"))
            if pos:
                self._set(src, lla=list(pos))
            vel = g((1, "Velocity"), (6, "VelNed"))
            if vel:
                self._set(src, vel_ned=list(vel))
            if (6, "VelBody") in v:
                self._set(src, vel_body=list(v[(6, "VelBody")]))
            if (5, "YprU") in v:
                self._set(src, att_unc=max(v[(5, "YprU")]))
            if (6, "PosU") in v:
                self._set(src, pos_unc=v[(6, "PosU")][0])
            if (6, "VelU") in v:
                self._set(src, vel_unc=v[(6, "VelU")][0])
            for grp, key in ((4, "gnss1"), (7, "gnss2")):
                if any(k[0] == grp for k in v):
                    cur = dict(self.v.get(key) or {})
                    if (grp, "Fix") in v:
                        cur["fix"] = v[(grp, "Fix")][0]
                    if (grp, "NumSats") in v:
                        cur["sats"] = v[(grp, "NumSats")][0]
                    if (grp, "PosLla") in v:
                        cur["lla"] = list(v[(grp, "PosLla")])
                    if (grp, "PosU") in v:
                        cur["acc"] = list(v[(grp, "PosU")])
                    self._set(src, **{key: cur})
            tg = g((1, "TimeGps"), (2, "TimeGps"))
            if tg:
                self._set(src, time_gps_ns=tg[0])
            self._log_row()

    # ------------------------------------------------------------------ #
    #  Grabacion CSV
    # ------------------------------------------------------------------ #
    COLS = ["t_host", "yaw", "pitch", "roll", "gx", "gy", "gz", "ax", "ay", "az",
            "mx", "my", "mz", "temp", "pres", "lat", "lon", "alt", "vn", "ve", "vd",
            "ins_status", "gps1_fix", "gps1_sats"]

    def start_csv(self, path: str) -> None:
        with self.lock:
            self.stop_csv_locked()
            self._csv_file = open(path, "w", newline="")
            self._csv = csv.writer(self._csv_file)
            self._csv.writerow(self.COLS)
            self.csv_path = path
            self.csv_rows = 0

    def stop_csv(self) -> None:
        with self.lock:
            self.stop_csv_locked()

    def stop_csv_locked(self) -> None:
        if self._csv_file:
            self._csv_file.close()
        self._csv = None
        self._csv_file = None

    @property
    def recording(self) -> bool:
        return self._csv is not None

    def _log_row(self) -> None:
        if self._csv is None:
            return
        v = self.v
        nan3 = [math.nan] * 3
        g1 = v.get("gnss1") or {}
        row = [f"{time.time():.4f}", *(v.get("ypr") or nan3), *(v.get("gyro") or nan3),
               *(v.get("accel") or nan3), *(v.get("mag") or nan3),
               v.get("temp", math.nan), v.get("pres", math.nan),
               *(v.get("lla") or nan3), *(v.get("vel_ned") or nan3),
               v.get("ins_status", ""), g1.get("fix", ""), g1.get("sats", "")]
        self._csv.writerow(row)
        self.csv_rows += 1
