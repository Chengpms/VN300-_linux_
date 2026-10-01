"""
Catalogo de registros del VN-300 para firmware v0.5.0.0
(manual UM005 "Firmware v0.5.0.0, Document Revision 2.22", capitulos 6-12).

Solo se incluyen los 51 registros documentados para ese firmware; cualquier
otro ID que responda el equipo se trata como "sin documentar" (lectura y
escritura en crudo).

Cada registro describe sus campos en el mismo orden en que aparecen en la
trama ASCII ($VNRRG / $VNWRG). Los tipos solo se usan para validar lo que
escribe el usuario antes de mandarlo al equipo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

FIRMWARE = "0.5.0.0"
MANUAL = "UM005 VN-300 User Manual, Firmware v0.5.0.0, Document Revision 2.22"


@dataclass
class Field:
    name: str
    kind: str = "f"            # f, d, u8, u16, u32, i32, str, hex
    unit: str = ""
    desc: str = ""
    choices: Dict[int, str] = field(default_factory=dict)
    optional: bool = False     # p.ej. el puerto serie opcional de los reg 5/6/7

    def validate(self, text: str) -> str:
        """Devuelve el texto normalizado o lanza ValueError."""
        t = text.strip()
        if t == "" and self.kind != "str":
            raise ValueError(f"{self.name}: vacio")
        if self.kind == "str":
            bad = set("$,*")
            if any(c in bad or not (0x20 <= ord(c) <= 0x7E) for c in t):
                raise ValueError(f"{self.name}: no se permiten '$', ',' ni '*'")
            return t[:20]
        if self.kind == "hex":
            int(t, 16)
            return t.upper().removeprefix("0X")
        if self.kind in ("f", "d"):
            float(t)
            return t
        v = int(t, 0)
        lim = {"u8": (0, 0xFF), "u16": (0, 0xFFFF), "u32": (0, 0xFFFFFFFF),
               "i32": (-2**31, 2**31 - 1)}.get(self.kind)
        if lim and not (lim[0] <= v <= lim[1]):
            raise ValueError(f"{self.name}: fuera de rango {lim}")
        if self.choices and v not in self.choices:
            opts = ", ".join(f"{k}={n}" for k, n in self.choices.items())
            raise ValueError(f"{self.name}: valor {v} no valido ({opts})")
        return str(v)


@dataclass
class Register:
    id: int
    name: str
    group: str
    writable: bool
    fields: List[Field]
    desc: str = ""
    async_header: str = ""
    variable: bool = False     # admite campos extra (binary output 75-77)
    caution: str = ""          # aviso al escribir (baudrate, etc.)

    @property
    def access(self) -> str:
        return "R/W" if self.writable else "R"


# --------------------------------------------------------------------------- #
#  Ayudas para construir campos repetitivos
# --------------------------------------------------------------------------- #

def _xyz(prefix: str, unit: str, kind: str = "f", desc: str = "") -> List[Field]:
    return [Field(f"{prefix}{a}", kind, unit, desc) for a in "XYZ"]


def _ypr() -> List[Field]:
    return [Field("Yaw", "f", "deg"), Field("Pitch", "f", "deg"), Field("Roll", "f", "deg")]


def _matrix(name: str = "C") -> List[Field]:
    return [Field(f"{name}[{i},{j}]", "f") for i in range(3) for j in range(3)]


def _comp(bias_unit: str) -> List[Field]:
    return _matrix() + [Field(f"B[{i}]", "f", bias_unit) for i in range(3)]


PORT = Field("SerialPort", "u8", "", "Opcional: 1 o 2. Vacio = puerto activo",
             {1: "Puerto 1", 2: "Puerto 2"}, optional=True)

BAUDS = [9600, 19200, 38400, 57600, 115200, 128000, 230400, 460800, 921600]

ADOR = {
    0: "OFF", 1: "YPR", 2: "QTN", 8: "QMR", 9: "DCM", 10: "MAG", 11: "ACC",
    12: "GYR", 13: "MAR", 14: "YMR", 16: "YBA", 17: "YIA", 19: "IMU",
    20: "GPS", 21: "GPE", 22: "INS", 23: "INE", 28: "ISL", 29: "ISE",
    30: "DTV", 32: "G2S", 33: "G2E",
}
ADOF = [1, 2, 4, 5, 10, 20, 25, 40, 50, 100, 200]

GPS_FIX = {0: "Sin fix", 1: "Solo tiempo", 2: "2D", 3: "3D"}

GPS_SOL = [
    Field("Time", "d", "s", "GPS time of week"), Field("Week", "u16", "week"),
    Field("GpsFix", "u8", "", "", GPS_FIX), Field("NumSats", "u8"),
    Field("Latitude", "d", "deg"), Field("Longitude", "d", "deg"),
    Field("Altitude", "d", "m"),
    Field("NedVelX", "f", "m/s"), Field("NedVelY", "f", "m/s"), Field("NedVelZ", "f", "m/s"),
    Field("NorthAcc", "f", "m"), Field("EastAcc", "f", "m"), Field("VertAcc", "f", "m"),
    Field("SpeedAcc", "f", "m/s"), Field("TimeAcc", "f", "s"),
]
GPS_ECEF = [
    Field("Tow", "d", "s"), Field("Week", "u16", "week"),
    Field("GpsFix", "u8", "", "", GPS_FIX), Field("NumSats", "u8"),
    *_xyz("Position", "m", "d"), *_xyz("Velocity", "m/s"), *_xyz("PosAcc", "m"),
    Field("SpeedAcc", "f", "m/s"), Field("TimeAcc", "f", "s"),
]

MAG = _xyz("Mag", "Gauss")
ACC = _xyz("Accel", "m/s2")
GYR = _xyz("Gyro", "rad/s")


REGISTERS: List[Register] = [
    # ---------------- Sistema -------------------------------------------- #
    Register(0, "User Tag", "Sistema", True,
             [Field("Tag", "str", "", "Hasta 20 caracteres ASCII imprimibles")],
             "Etiqueta libre del usuario. Se guarda en flash con Write Settings."),
    Register(1, "Model Number", "Sistema", False, [Field("Product", "str")]),
    Register(2, "Hardware Revision", "Sistema", False, [Field("Revision", "u32")]),
    Register(3, "Serial Number", "Sistema", False, [Field("SerialNum", "u32")]),
    Register(4, "Firmware Version", "Sistema", False, [Field("Version", "str", "", "Major.Minor.Feature.HotFix")]),
    Register(5, "Serial Baud Rate", "Sistema", True,
             [Field("BaudRate", "u32", "baud", "Velocidad del puerto",
                    {b: str(b) for b in BAUDS}), PORT],
             "Velocidad del puerto serie.",
             caution="Al cambiar el baudrate del puerto activo la app se reconecta "
                     "automaticamente a la nueva velocidad."),
    Register(6, "Async Data Output Type", "Sistema", True,
             [Field("ADOR", "u32", "", "Mensaje ASCII asincrono", ADOR), PORT],
             "Que registro se emite automaticamente en ASCII."),
    Register(7, "Async Data Output Freq", "Sistema", True,
             [Field("ADOF", "u32", "Hz", "Frecuencia de salida ASCII",
                    {0: "0 (sin salida)", **{f: f"{f} Hz" for f in ADOF}}), PORT]),
    Register(30, "Communication Protocol Control", "Sistema", True, [
        Field("SerialCount", "u8", "", "Contador anexado a mensajes async",
              {0: "OFF", 1: "SyncIn count", 2: "SyncIn time", 3: "SyncOut count", 4: "GPS PPS time"}),
        Field("SerialStatus", "u8", "", "Estado anexado", {0: "OFF", 1: "VPE status", 2: "INS status"}),
        Field("SPICount", "u8", "", "", {0: "OFF", 1: "SyncIn count", 2: "SyncIn time", 3: "SyncOut count", 4: "GPS PPS time"}),
        Field("SPIStatus", "u8", "", "", {0: "OFF", 1: "VPE status", 2: "INS status"}),
        Field("SerialChecksum", "u8", "", "", {1: "8-bit checksum", 3: "16-bit CRC"}),
        Field("SPIChecksum", "u8", "", "", {0: "OFF", 1: "8-bit checksum", 3: "16-bit CRC"}),
        Field("ErrorMode", "u8", "", "", {0: "Ignorar", 1: "Enviar error", 2: "Enviar error y ADOR=OFF"}),
    ]),
    Register(32, "Synchronization Control", "Sistema", True, [
        Field("SyncInMode", "u8", "", "", {3: "COUNT", 4: "IMU", 5: "ASYNC", 6: "ASYNC3"}),
        Field("SyncInEdge", "u8", "", "", {0: "Flanco subida", 1: "Flanco bajada"}),
        Field("SyncInSkipFactor", "u16"),
        Field("Reserved", "u32"),
        Field("SyncOutMode", "u8", "", "", {0: "NONE", 1: "IMU_START", 2: "IMU_READY", 3: "INS", 6: "GPS_PPS"}),
        Field("SyncOutPolarity", "u8", "", "", {0: "Pulso negativo", 1: "Pulso positivo"}),
        Field("SyncOutSkipFactor", "u16"),
        Field("SyncOutPulseWidth", "u32", "ns"),
        Field("Reserved", "u32"),
    ]),
    Register(33, "Synchronization Status", "Sistema", True, [
        Field("SyncInCount", "u32"), Field("SyncInTime", "u32", "us"), Field("SyncOutCount", "u32"),
    ]),
    *[Register(rid, f"Binary Output {n}", "Sistema", True, [
        Field("AsyncMode", "u16", "", "", {0: "Ninguno", 1: "Puerto 1", 2: "Puerto 2", 3: "Ambos"}),
        Field("RateDivisor", "u16", "", "400 Hz / divisor"),
        Field("OutputGroup", "hex", "", "Bits de grupos activos (hex)"),
        ], "Mensaje binario configurable. Usa el boton 'Asistente binario' para armarlo.",
        variable=True) for n, rid in ((1, 75), (2, 76), (3, 77))],
    *[Register(rid, f"NMEA Output {n}", "Sistema", True, [
        Field("Port", "u8", "", "", {0: "Ninguno", 1: "Puerto 1", 2: "Puerto 2", 3: "Ambos"}),
        Field("Rate", "u8", "Hz"), Field("Mode", "u8"), Field("Reserved", "u8"),
        Field("MessageSelection", "hex", "", "Bitfield de mensajes NMEA (hex)"),
    ]) for n, rid in ((1, 101), (2, 102))],

    # ---------------- IMU ------------------------------------------------- #
    Register(54, "IMU Measurements", "IMU", False,
             MAG + ACC + GYR + [Field("Temp", "f", "C"), Field("Pressure", "f", "kPa")],
             "Medidas IMU sin compensar.", "IMU"),
    Register(80, "Delta Theta / Delta Velocity", "IMU", False,
             [Field("DeltaTime", "f", "s"), *_xyz("DeltaTheta", "deg"), *_xyz("DeltaVelocity", "m/s")],
             async_header="DTV"),
    Register(23, "Magnetometer Compensation", "IMU", True, _comp("Gauss")),
    Register(25, "Accelerometer Compensation", "IMU", True, _comp("m/s2")),
    Register(84, "Gyro Compensation", "IMU", True, _comp("rad/s")),
    Register(26, "Reference Frame Rotation", "IMU", True, _matrix(),
             "Matriz de rotacion cuerpo->usuario. Requiere Write Settings + Reset.",
             caution="Este registro solo se aplica tras guardar en flash y reiniciar."),
    Register(85, "IMU Filtering Configuration", "IMU", True, [
        *[Field(f"{s}WindowSize", "u16") for s in ("Mag", "Accel", "Gyro", "Temp", "Pres")],
        *[Field(f"{s}FilterMode", "u8", "", "", {0: "Sin filtro", 1: "Solo raw", 2: "Solo compensado", 3: "Ambos"})
          for s in ("Mag", "Accel", "Gyro", "Temp", "Pres")],
    ]),
    Register(82, "Delta Theta/Velocity Config", "IMU", True, [
        Field("IntegrationFrame", "u8", "", "", {0: "Body", 1: "NED", 2: "ECEF"}),
        Field("GyroCompensation", "u8", "", "", {0: "Ninguna", 1: "Bias"}),
        Field("AccelCompensation", "u8", "", "", {0: "Ninguna", 1: "Bias"}),
        Field("Reserved", "u8"), Field("Reserved", "u16"),
    ]),

    # ---------------- GNSS ------------------------------------------------ #
    Register(58, "GPS Solution - LLA", "GPS", False, GPS_SOL, async_header="GPS"),
    Register(59, "GPS Solution - ECEF", "GPS", False, GPS_ECEF, async_header="GPE"),
    Register(103, "GPS2 Solution - LLA", "GPS", False, GPS_SOL, async_header="G2S"),
    Register(104, "GPS2 Solution - ECEF", "GPS", False, GPS_ECEF, async_header="G2E"),
    Register(55, "GPS Configuration", "GPS", True, [
        Field("Mode", "u8", "", "", {0: "GPS interno", 1: "GPS externo", 2: "Sensor VectorNav externo como GPS"}),
        Field("PpsSource", "u8", "", "", {0: "GPS_PPS subida", 1: "GPS_PPS bajada", 2: "SyncIn subida", 3: "SyncIn bajada"}),
        Field("Rate", "u8", "Hz", "Debe ser 5"),
        Field("TimeSyncDelta", "u8", "", "Reservado (0)"),
        Field("AntPower", "u8", "", "", {0: "Apagada", 1: "Interna", 2: "Externa (VANT)"}),
    ]),
    Register(57, "GPS Antenna A Offset", "GPS", True, _xyz("Position", "m"),
             "Brazo de palanca de la antena GPS A respecto al sensor (marco vehiculo)."),
    Register(93, "GPS Compass Baseline", "GPS", True,
             _xyz("Position", "m") + _xyz("Uncertainty", "m"),
             "Posicion de la antena B respecto a la A. Error rumbo ~ 0.57*err[cm]/L[m]."),
    Register(98, "GPS Compass Startup Status", "GPS", False, [
        Field("PercentComplete", "u8", "%"), Field("CurrentHeading", "f", "deg"),
    ]),
    Register(86, "GPS Compass Signal Health Status", "GPS", False, [
        Field("NumSatsPVT_1", "f"), Field("NumSatsRTK_1", "f"), Field("HighestCN0_1", "f", "dBHz"),
        Field("NumSatsPVT_2", "f"), Field("NumSatsRTK_2", "f"), Field("HighestCN0_2", "f", "dBHz"),
        Field("NumComSatsPVT", "f"), Field("NumComSatsRTK", "f"),
    ]),

    # ---------------- Actitud --------------------------------------------- #
    Register(8, "Yaw Pitch Roll", "Actitud", False, _ypr(), async_header="YPR"),
    Register(9, "Attitude Quaternion", "Actitud", False,
             [Field(f"Quat[{i}]", "f") for i in range(4)], async_header="QTN"),
    Register(27, "YPR, Mag, Accel, Gyro", "Actitud", False, _ypr() + MAG + ACC + GYR, async_header="YMR"),
    Register(15, "Quat, Mag, Accel, Gyro", "Actitud", False,
             [Field(f"Quat[{i}]", "f") for i in range(4)] + MAG + ACC + GYR, async_header="QMR"),
    Register(17, "Magnetic Measurements", "Actitud", False, MAG, async_header="MAG"),
    Register(18, "Acceleration Measurements", "Actitud", False, ACC, async_header="ACC"),
    Register(19, "Angular Rate Measurements", "Actitud", False, GYR, async_header="GYR"),
    Register(20, "Mag, Accel, Gyro", "Actitud", False, MAG + ACC + GYR, async_header="MAR"),

    # ---------------- INS ------------------------------------------------- #
    Register(63, "INS Solution - LLA", "INS", False, [
        Field("Time", "d", "s"), Field("Week", "u16"), Field("Status", "hex", "", "Bits INS (ver panel En vivo)"),
        *_ypr(), Field("Latitude", "d", "deg"), Field("Longitude", "d", "deg"), Field("Altitude", "d", "m"),
        *_xyz("NedVel", "m/s"), Field("AttUncertainty", "f", "deg"),
        Field("PosUncertainty", "f", "m"), Field("VelUncertainty", "f", "m/s"),
    ], async_header="INS"),
    Register(64, "INS Solution - ECEF", "INS", False, [
        Field("Time", "d", "s"), Field("Week", "u16"), Field("Status", "hex"),
        *_ypr(), *_xyz("Position", "m", "d"), *_xyz("Velocity", "m/s"),
        Field("AttUncertainty", "f", "deg"), Field("PosUncertainty", "f", "m"),
        Field("VelUncertainty", "f", "m/s"),
    ], async_header="INE"),
    Register(72, "INS State - LLA", "INS", False, [
        *_ypr(), Field("Latitude", "d", "deg"), Field("Longitude", "d", "deg"), Field("Altitude", "d", "m"),
        *_xyz("Velocity", "m/s"), *_xyz("Accel", "m/s2"), *_xyz("AngularRate", "rad/s"),
    ], async_header="ISL"),
    Register(73, "INS State - ECEF", "INS", False, [
        *_ypr(), *_xyz("Position", "m", "d"), *_xyz("Velocity", "m/s"),
        *_xyz("Accel", "m/s2"), *_xyz("AngularRate", "rad/s"),
    ], async_header="ISE"),
    Register(67, "INS Basic Configuration", "INS", True, [
        Field("Scenario", "u8", "", "", {1: "INS con barometro", 2: "INS sin barometro", 3: "GPS moving baseline (dinamico)"}),
        Field("AhrsAiding", "u8", "", "", {0: "Off", 1: "On"}),
        Field("EstBaseline", "u8", "", "", {0: "Off", 1: "On"}),
        Field("Resv2", "u8"),
    ]),
    Register(74, "Startup Filter Bias Estimate", "INS", True,
             _xyz("GyroBias", "rad/s") + _xyz("AccelBias", "m/s2") + [Field("PressureBias", "f", "m")]),

    # ---------------- Hard/Soft Iron & World ------------------------------ #
    Register(44, "Magnetometer Calibration Control", "Mag HSI", True, [
        Field("HSIMode", "u8", "", "", {0: "HSI_OFF", 1: "HSI_RUN", 2: "HSI_RESET"}),
        Field("HSIOutput", "u8", "", "", {1: "NO_ONBOARD", 3: "USE_ONBOARD"}),
        Field("ConvergeRate", "u8", "", "1 lento (60-90 s) .. 5 rapido (15-20 s)",
              {i: str(i) for i in range(1, 6)}),
    ], "Calibracion hard/soft iron en tiempo real."),
    Register(47, "Calculated Magnetometer Cal", "Mag HSI", False, _comp("Gauss"),
             "Solucion HSI calculada por el algoritmo."),
    Register(21, "Mag & Gravity Reference", "World", True,
             _xyz("MagRef", "Gauss") + _xyz("AccRef", "m/s2")),
    Register(83, "Reference Vector Configuration", "World", True, [
        Field("UseMagModel", "u8", "", "", {0: "No", 1: "Si"}),
        Field("UseGravityModel", "u8", "", "", {0: "No", 1: "Si"}),
        Field("Resv1", "u8"), Field("Resv2", "u8"),
        Field("RecalcThreshold", "u32", "m"), Field("Year", "f", "year"),
        Field("Latitude", "d", "deg"), Field("Longitude", "d", "deg"), Field("Altitude", "d", "m"),
    ]),
]

GROUP_ORDER = ["Sistema", "IMU", "GPS", "Actitud", "INS", "Mag HSI", "World"]

REGISTERS.sort(key=lambda r: (GROUP_ORDER.index(r.group) if r.group in GROUP_ORDER else 99, r.id))

BY_ID: Dict[int, Register] = {r.id: r for r in REGISTERS}
BY_HEADER: Dict[str, Register] = {"VN" + r.async_header: r for r in REGISTERS if r.async_header}
GROUPS: List[str] = list(dict.fromkeys(r.group for r in REGISTERS))


_UNKNOWN: Dict[int, Register] = {}


def get(rid: int) -> Register:
    if rid in BY_ID:
        return BY_ID[rid]
    # Registro sin definicion: se permite leerlo y escribirlo en crudo
    if rid not in _UNKNOWN:
        _UNKNOWN[rid] = Register(
            rid, f"Registro {rid} (sin documentar)", "Otros", True, [],
            "Registro no documentado. Los valores se muestran tal cual llegan del equipo.",
            variable=True,
            caution="No documentado: escribe solo si sabes exactamente lo que hace "
                    "(puede ser un registro interno o de fabrica).")
    return _UNKNOWN[rid]


def activity(rid: int, vals: Sequence[str]):
    """
    Para registros de configuracion con sentido de 'habilitado', devuelve
    (activo, detalle). None si el registro no tiene ese concepto.
    """
    def i(k: int, base: int = 10) -> int:
        try:
            return int(vals[k], base)
        except (IndexError, ValueError):
            return 0

    def fl(k: int) -> float:
        try:
            return float(vals[k])
        except (IndexError, ValueError):
            return 0.0

    if rid == 6:
        a = i(0)
        return (a != 0, f"$VN{ADOR.get(a, a)}" if a else "sin salida ASCII")
    if rid == 7:
        return (i(0) > 0, f"{i(0)} Hz")
    if rid in (75, 76, 77):
        mode, div = i(0), i(1)
        if not mode:
            return (False, "salida binaria apagada")
        port = {1: "puerto 1", 2: "puerto 2", 3: "ambos puertos"}.get(mode, str(mode))
        hz = f"{400 / div:g} Hz" if div else "solo sondeo"
        return (True, f"{port}, {hz}, grupos {vals[2] if len(vals) > 2 else '?'}")
    if rid in (101, 102):
        return (i(0) != 0, f"puerto {i(0)}, {i(1)} Hz" if i(0) else "NMEA apagado")
    if rid == 44:
        return (i(0) == 1, {0: "HSI parado", 1: "HSI calibrando", 2: "HSI reset"}.get(i(0), "?"))
    if rid == 30:
        on = [n for n, k in (("contador", 0), ("estado", 1)) if i(k)]
        return (bool(on), "anexa " + " y ".join(on) if on else "sin sufijos")
    if rid == 55:
        return (i(4) != 0, {0: "antena GPS sin alimentar", 1: "alim. interna", 2: "alim. externa"}.get(i(4), "?"))
    if rid == 67:
        return (True, f"escenario {i(0)}, AHRS aiding {'on' if i(1) else 'off'}, "
                      f"est. baseline {'on' if i(2) else 'off'}")
    if rid == 83:
        on = [n for n, k in (("modelo magnetico", 0), ("modelo gravedad", 1)) if i(k)]
        return (bool(on), ", ".join(on) if on else "modelos apagados")
    if rid == 32:
        return (i(4) != 0, f"SyncIn modo {i(0)}, SyncOut modo {i(4)}")
    return None


def label_value(f: Field, raw: str) -> str:
    """Texto amigable para un valor crudo (agrega el significado del enum)."""
    if f.choices:
        try:
            v = int(raw, 0) if f.kind != "hex" else int(raw, 16)
            if v in f.choices and f.choices[v] != str(v):
                return f"{raw}  ({f.choices[v]})"
        except ValueError:
            pass
    return raw


def validate_values(reg: Register, values: Sequence[str]) -> List[str]:
    """Valida los valores a escribir. Devuelve la lista normalizada."""
    out: List[str] = []
    vals = list(values)
    # quitar opcionales vacios del final
    while vals and vals[-1].strip() == "" and len(vals) <= len(reg.fields) \
            and reg.fields[len(vals) - 1].optional:
        vals.pop()
    required = [f for f in reg.fields if not f.optional]
    if len(vals) < len(required):
        raise ValueError(f"Se esperaban al menos {len(required)} valores, hay {len(vals)}")
    if not reg.variable and reg.fields and len(vals) > len(reg.fields):
        raise ValueError(f"Demasiados valores ({len(vals)} > {len(reg.fields)})")
    for i, v in enumerate(vals):
        if i < len(reg.fields):
            out.append(reg.fields[i].validate(v))
        else:
            # campos extra (75-77: OutputField en hex) o registro desconocido
            out.append(v.strip())
    return out


# --------------------------------------------------------------------------- #
#  INS status (registro 63, campo Status; tambien binario InsStatus)
# --------------------------------------------------------------------------- #

INS_MODE = {0: "No tracking (init compass)", 1: "Alineando", 2: "Tracking OK", 3: "Perdida GPS >45 s"}


def decode_ins_status(st: int) -> Dict[str, object]:
    err = (st >> 3) & 0xF
    return {
        "mode": st & 0x3,
        "mode_txt": INS_MODE.get(st & 0x3, "?"),
        "gnss_fix": bool(st & (1 << 2)),
        "imu_err": bool(err & 0x2),
        "magpres_err": bool(err & 0x4),
        "gnss_err": bool(err & 0x8),
        "gnss_heading_ins": bool(st & (1 << 8)),
        "gnss_compass": bool(st & (1 << 9)),
    }
