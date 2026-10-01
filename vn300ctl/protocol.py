"""
Protocolo serie del VN-300, firmware v0.5.0.0 (manual UM005 rev. 2.22, secciones 3.8, 5 y 6.1).

- ASCII:   $VN<CMD>,<campos>*<checksum>\r\n
           checksum = XOR de 8 bits (2 hex) o CRC16-CCITT (4 hex) de todo lo que
           hay entre '$' y '*'.
- Binario: 0xFA | groups | groupFields(u16 LE ...) | payload | CRC16 (big endian)
           El CRC calculado sobre todo lo que sigue al sync (incluido el CRC) da 0.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
#  Checksums
# --------------------------------------------------------------------------- #


def checksum8(data: bytes) -> int:
    cs = 0
    for b in data:
        cs ^= b
    return cs


def crc16(data: bytes, crc: int = 0) -> int:
    """CRC16-CCITT tal y como lo implementa VectorNav (manual 3.8.2)."""
    for b in data:
        crc = ((crc >> 8) | (crc << 8)) & 0xFFFF
        crc ^= b
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
    return crc


ERROR_CODES = {
    1: "Hard Fault",
    2: "Serial Buffer Overflow",
    3: "Checksum invalido",
    4: "Comando invalido",
    5: "Faltan parametros",
    6: "Demasiados parametros",
    7: "Parametro invalido",
    8: "Registro invalido",
    9: "Acceso no autorizado (registro de solo lectura)",
    10: "Watchdog Reset",
    11: "Output Buffer Overflow",
    12: "Insufficient Baud Rate",
    255: "Error Buffer Overflow",
}


def build_ascii(body: str, use_crc: bool = False) -> bytes:
    """body sin '$' ni '*': p.ej. 'VNRRG,8' -> b'$VNRRG,8*4B\\r\\n'."""
    raw = body.encode("ascii")
    if use_crc:
        tail = f"{crc16(raw):04X}"
    else:
        tail = f"{checksum8(raw):02X}"
    return b"$" + raw + b"*" + tail.encode() + b"\r\n"


@dataclass
class AsciiMsg:
    header: str               # 'VNRRG', 'VNYMR', 'VNERR', ...
    fields: List[str]         # campos tras la cabecera (sin T.../S... finales)
    raw: str                  # linea completa sin \r\n
    counter: Optional[str] = None   # sufijo T... (reg 30 SerialCount)
    status: Optional[str] = None    # sufijo S... (reg 30 SerialStatus)


def parse_ascii(line: bytes) -> Optional[AsciiMsg]:
    """Valida y separa una linea ASCII. Devuelve None si el checksum falla."""
    try:
        text = line.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not text.startswith("$VN"):
        return None
    star = text.rfind("*")
    if star < 0:
        return None
    body, cs = text[1:star], text[star + 1:]
    if cs.upper() != "XX":
        try:
            val = int(cs, 16)
        except ValueError:
            return None
        if len(cs) == 2:
            if checksum8(body.encode()) != val:
                return None
        elif len(cs) == 4:
            if crc16(body.encode()) != val:
                return None
        else:
            return None
    parts = body.split(",")
    msg = AsciiMsg(header=parts[0], fields=parts[1:], raw=text)
    # Sufijos opcionales que agrega el registro 30 (T=contador, S=estado)
    while msg.fields and msg.fields[-1][:1] in ("T", "S") and _is_suffix(msg.fields[-1]):
        last = msg.fields.pop()
        if last[0] == "T":
            msg.counter = last[1:]
        else:
            msg.status = last[1:]
    return msg


def _is_suffix(s: str) -> bool:
    if len(s) < 2:
        return False
    tail = s[1:]
    return all(c in "0123456789abcdefABCDEF" for c in tail)


# --------------------------------------------------------------------------- #
#  Binario
# --------------------------------------------------------------------------- #

# Tamano en bytes de cada campo por grupo (manual 5.3.6). None = longitud variable.
BIN_SIZES: Dict[int, List[Optional[int]]] = {
    1: [8, 8, 8, 12, 16, 12, 24, 12, 12, 24, 20, 28, 2, 4, 8],
    2: [8, 8, 8, 2, 8, 8, 8, 4, 4, 1],
    3: [2, 12, 12, 12, 4, 4, 16, 12, 12, 12, 12],
    4: [8, 8, 2, 1, 1, 24, 24, 12, 12, 12, 4, 4, 2, 28, None, None],
    5: [2, 12, 16, 36, 12, 12, 12, 12, 12],
    6: [2, 24, 24, 12, 12, 12, 12, 12, 12, 4, 4],
    7: [8, 8, 2, 1, 1, 24, 24, 12, 12, 12, 4, 4, 2, 28, None, None],
}

# Nombre de cada campo (bit) por grupo (manual 5.2.2).
BIN_NAMES: Dict[int, List[str]] = {
    1: ["TimeStartup", "TimeGps", "TimeSyncIn", "YawPitchRoll", "Quaternion",
        "AngularRate", "Position", "Velocity", "Accel", "Imu", "MagPres",
        "DeltaTheta", "InsStatus", "SyncInCnt", "TimeGpsPps"],
    2: ["TimeStartup", "TimeGps", "GpsTow", "GpsWeek", "TimeSyncIn",
        "TimeGpsPps", "TimeUTC", "SyncInCnt", "SyncOutCnt", "TimeStatus"],
    3: ["ImuStatus", "UncompMag", "UncompAccel", "UncompGyro", "Temp", "Pres",
        "DeltaTheta", "DeltaVel", "Mag", "Accel", "AngularRate"],
    4: ["UTC", "Tow", "Week", "NumSats", "Fix", "PosLla", "PosEcef", "VelNed",
        "VelEcef", "PosU", "VelU", "TimeU", "TimeInfo", "DOP", "SatInfo", "RawMeas"],
    5: ["Reserved", "YawPitchRoll", "Quaternion", "DCM", "MagNed", "AccelNed",
        "LinearAccelBody", "LinearAccelNed", "YprU"],
    6: ["InsStatus", "PosLla", "PosEcef", "VelBody", "VelNed", "VelEcef",
        "MagEcef", "AccelEcef", "LinearAccelEcef", "PosU", "VelU"],
    7: ["UTC", "Tow", "Week", "NumSats", "Fix", "PosLla", "PosEcef", "VelNed",
        "VelEcef", "PosU", "VelU", "TimeU", "TimeInfo", "DOP", "SatInfo", "RawMeas"],
}

GROUP_TITLES = {1: "Common", 2: "Time", 3: "IMU", 4: "GPS1", 5: "Attitude",
                6: "INS", 7: "GPS2"}

# Formato struct de los campos que decodificamos (little endian).
_FMT: Dict[Tuple[int, str], str] = {}
for _g in (1, 2):
    _FMT[(_g, "TimeStartup")] = "<Q"
    _FMT[(_g, "TimeGps")] = "<Q"
    _FMT[(_g, "TimeSyncIn")] = "<Q"
    _FMT[(_g, "TimeGpsPps")] = "<Q"
    _FMT[(_g, "SyncInCnt")] = "<I"
_FMT.update({
    (1, "YawPitchRoll"): "<3f", (1, "Quaternion"): "<4f", (1, "AngularRate"): "<3f",
    (1, "Position"): "<3d", (1, "Velocity"): "<3f", (1, "Accel"): "<3f",
    (1, "Imu"): "<6f", (1, "MagPres"): "<5f", (1, "DeltaTheta"): "<7f",
    (1, "InsStatus"): "<H",
    (2, "GpsTow"): "<Q", (2, "GpsWeek"): "<H", (2, "SyncOutCnt"): "<I",
    (2, "TimeStatus"): "<B",
    (3, "ImuStatus"): "<H", (3, "UncompMag"): "<3f", (3, "UncompAccel"): "<3f",
    (3, "UncompGyro"): "<3f", (3, "Temp"): "<f", (3, "Pres"): "<f",
    (3, "DeltaTheta"): "<4f", (3, "DeltaVel"): "<3f", (3, "Mag"): "<3f",
    (3, "Accel"): "<3f", (3, "AngularRate"): "<3f",
    (5, "Reserved"): "<H", (5, "YawPitchRoll"): "<3f", (5, "Quaternion"): "<4f",
    (5, "DCM"): "<9f", (5, "MagNed"): "<3f", (5, "AccelNed"): "<3f",
    (5, "LinearAccelBody"): "<3f", (5, "LinearAccelNed"): "<3f", (5, "YprU"): "<3f",
    (6, "InsStatus"): "<H", (6, "PosLla"): "<3d", (6, "PosEcef"): "<3d",
    (6, "VelBody"): "<3f", (6, "VelNed"): "<3f", (6, "VelEcef"): "<3f",
    (6, "MagEcef"): "<3f", (6, "AccelEcef"): "<3f", (6, "LinearAccelEcef"): "<3f",
    (6, "PosU"): "<f", (6, "VelU"): "<f",
})
for _g in (4, 7):
    _FMT.update({
        (_g, "Tow"): "<Q", (_g, "Week"): "<H", (_g, "NumSats"): "<B", (_g, "Fix"): "<B",
        (_g, "PosLla"): "<3d", (_g, "PosEcef"): "<3d", (_g, "VelNed"): "<3f",
        (_g, "VelEcef"): "<3f", (_g, "PosU"): "<3f", (_g, "VelU"): "<f",
        (_g, "TimeU"): "<f", (_g, "DOP"): "<7f",
    })


@dataclass
class BinaryMsg:
    groups: Dict[int, List[str]]                 # grupo -> campos presentes
    values: Dict[Tuple[int, str], tuple] = field(default_factory=dict)
    length: int = 0


def _iter_bits(value: int, nbits: int):
    for i in range(nbits):
        if value & (1 << i):
            yield i


def try_parse_binary(buf: bytes | bytearray, start: int = 0):
    """
    Intenta parsear un paquete binario que empieza en buf[start] (== 0xFA).

    Devuelve (BinaryMsg, longitud) si es valido, (None, 0) si faltan bytes
    y (None, -1) si no es un paquete valido.
    """
    n = len(buf)
    i = start + 1
    if i >= n:
        return None, 0
    group_byte = buf[i]
    if group_byte == 0 or group_byte & 0x80:
        return None, -1
    i += 1
    active = [g + 1 for g in _iter_bits(group_byte, 7)]
    fields_per_group: Dict[int, int] = {}
    for g in active:
        word = 0
        shift = 0
        while True:
            if i + 2 > n:
                return None, 0
            w = buf[i] | (buf[i + 1] << 8)
            i += 2
            word |= (w & 0x7FFF) << shift
            shift += 15
            if not (w & 0x8000):
                break
            if shift > 30:
                return None, -1
        fields_per_group[g] = word

    groups: Dict[int, List[str]] = {}
    layout: List[Tuple[int, str, int]] = []      # (grupo, nombre, offset)
    for g in active:
        names = BIN_NAMES[g]
        sizes = BIN_SIZES[g]
        groups[g] = []
        for bit in _iter_bits(fields_per_group[g], 30):
            if bit >= len(sizes):
                return None, -1
            size = sizes[bit]
            if size is None:
                # SatInfo: u8 numSats + u8 resv + N*8 ; RawMeas: 12 + N*28 (numSats en byte 10)
                if names[bit] == "SatInfo":
                    if i + 1 > n:
                        return None, 0
                    size = 2 + buf[i] * 8
                else:
                    if i + 11 > n:
                        return None, 0
                    size = 12 + buf[i + 10] * 28
            groups[g].append(names[bit])
            layout.append((g, names[bit], i))
            i += size
    end = i + 2
    if end > n:
        return None, 0
    if crc16(bytes(buf[start + 1:end])) != 0:
        return None, -1

    msg = BinaryMsg(groups=groups, length=end - start)
    for g, name, off in layout:
        fmt = _FMT.get((g, name))
        if fmt:
            try:
                msg.values[(g, name)] = struct.unpack_from(fmt, buf, off)
            except struct.error:
                pass
    return msg, end - start
