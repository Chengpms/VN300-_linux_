"""
Linea de comandos.

  vn300ctl                                  abre la interfaz (TUI)
  vn300ctl tui   -p /dev/ttyUSB0 -b 115200  idem, conectando directamente
  vn300ctl ports                            lista puertos serie
  vn300ctl info  -p /dev/ttyUSB0            modelo, N/S, firmware
  vn300ctl read  -p ... 8 63 98             lee uno o varios registros
  vn300ctl write -p ... 7 40 [--flash]      escribe un registro
  vn300ctl write -p ... 6 14 -- 7 40 --flash   varios registros de una vez
  vn300ctl flash -p ... config.json         escribe un JSON (export de la TUI) y guarda en flash
  vn300ctl dump  -p ... [-o config.json]    exporta todos los registros configurables
  vn300ctl monitor -p ...                   imprime actitud/INS en vivo (texto plano)
  vn300ctl log -p ... -o datos.csv [-t 60]  graba CSV sin interfaz (Ctrl+C para parar)
  vn300ctl cmd   -p ... "RRG,8"             comando ASCII crudo
  vn300ctl regs                             lista el catalogo de registros
  vn300ctl sgb | sfb -p ... [--flash]       $VNSGB / $VNSFB: bias estimado -> registro 74

Catalogo y comandos segun el manual UM005 para firmware v0.5.0.0 (rev. 2.22).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Dict, List

from . import registers as R
from .device import VN300, VNError, VNTimeout, list_ports


def _connect(args) -> VN300:
    if not args.port:
        sys.exit("Falta -p/--port (p.ej. -p /dev/ttyUSB0, o -p sim para el simulador)")
    dev = VN300()
    try:
        if args.baud:
            dev.open(args.port, args.baud)
            dev.read_register(1, timeout=0.6)
        else:
            b = dev.autodetect(args.port)
            print(f"# baudrate detectado: {b}", file=sys.stderr)
    except Exception as exc:
        dev.close()
        sys.exit(f"No se pudo conectar a {args.port}: {exc}")
    return dev


def _print_reg(rid: int, vals: List[str]) -> None:
    reg = R.get(rid)
    print(f"[{rid}] {reg.name}")
    for i, v in enumerate(vals):
        if i < len(reg.fields):
            f = reg.fields[i]
            unit = f" {f.unit}" if f.unit else ""
            print(f"    {f.name:<22} {R.label_value(f, v)}{unit}")
        else:
            print(f"    extra[{i - len(reg.fields)}]{'':<14} {v}")


def _split_groups(tokens: List[str]) -> Dict[int, List[str]]:
    """'6 14 -- 7 40' -> {6:['14'], 7:['40']}. Tambien acepta '7,40'."""
    items: Dict[int, List[str]] = {}
    cur: List[str] = []
    groups = []
    for t in tokens:
        if t == "--":
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.extend(x for x in t.split(",") if x != "")
    if cur:
        groups.append(cur)
    for g in groups:
        items[int(g[0])] = g[1:]
    return items


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="vn300ctl", description="Control del VectorNav VN-300 desde Linux",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("-p", "--port", help="/dev/ttyUSB0, /dev/serial/by-id/..., o 'sim'")
    ap.add_argument("-b", "--baud", type=int, help="baudrate (por defecto se autodetecta)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("tui")
    sub.add_parser("ports")
    sub.add_parser("info")
    sub.add_parser("regs")
    p = sub.add_parser("read")
    p.add_argument("ids", nargs="+", type=int)
    p = sub.add_parser("write")
    p.add_argument("--flash", action="store_true", help="guardar en flash ($VNWNV) al final")
    p.add_argument("--yes", "-y", action="store_true", help="no pedir confirmacion")
    p.add_argument("spec", nargs=argparse.REMAINDER, help="ID valores... [-- ID valores...]")
    p = sub.add_parser("flash")
    p.add_argument("file")
    p.add_argument("--no-save", action="store_true", help="no ejecutar $VNWNV")
    p.add_argument("--yes", "-y", action="store_true")
    p = sub.add_parser("dump")
    p.add_argument("-o", "--output")
    sub.add_parser("monitor")
    p = sub.add_parser("log")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("-t", "--seconds", type=float, default=0, help="duracion (0 = hasta Ctrl+C)")
    p = sub.add_parser("cmd")
    p.add_argument("text")
    for name in ("save", "reset", "factory"):
        sub.add_parser(name)
    for name in ("sgb", "sfb"):
        p = sub.add_parser(name)
        p.add_argument("--flash", action="store_true", help="guardar en flash ($VNWNV) despues")

    args = ap.parse_args(argv)

    if args.cmd in (None, "tui"):
        from .tui import VN300App
        VN300App(args.port, args.baud).run()
        return

    if args.cmd == "ports":
        for p in list_ports():
            print(p)
        return

    if args.cmd == "regs":
        for r in R.REGISTERS:
            print(f"{r.id:>4}  {r.access:<3}  {r.group:<8}  {r.name}"
                  + (f"   ($VN{r.async_header})" if r.async_header else ""))
        return

    dev = _connect(args)
    try:
        if args.cmd == "info":
            info = dev.device_info()
            for k, v in info.items():
                print(f"{k:<8} {v}")
            print(f"{'baud':<8} {dev.baud}")
            if not dev.firmware_matches(info.get("fw", "")):
                print(f"AVISO: el equipo tiene firmware v{info.get('fw')}; el catalogo es el de v{R.FIRMWARE}")

        elif args.cmd == "read":
            for rid in args.ids:
                try:
                    _print_reg(rid, dev.read_register(rid))
                except (VNTimeout, VNError) as exc:
                    print(f"[{rid}] ERROR: {exc}")

        elif args.cmd == "write":
            spec = [s for s in args.spec if s != "--flash" and s not in ("-y", "--yes")]
            flash = args.flash or "--flash" in args.spec
            yes = args.yes or "-y" in args.spec or "--yes" in args.spec
            items = _split_groups(spec)
            if not items:
                sys.exit("Nada que escribir. Ej: write 7 40   o   write 6 14 -- 7 40 --flash")
            for rid, vals in items.items():
                items[rid] = R.validate_values(R.get(rid), vals)
                print(f"  $VNWRG,{rid},{','.join(items[rid])}")
            if flash:
                print("  $VNWNV")
            if not yes and input("¿Enviar? [s/N] ").strip().lower() not in ("s", "si", "y", "yes"):
                return
            res = dev.flash_many(items, save=flash)
            for rid, r in res.items():
                print(f"[{rid}] " + (f"ERROR {r}" if isinstance(r, Exception) else "OK " + ",".join(r)))

        elif args.cmd == "flash":
            with open(args.file) as fh:
                data = json.load(fh)
            regs = data.get("registros", data)
            items = {}
            for rid, entry in regs.items():
                vals = entry["valores"] if isinstance(entry, dict) else entry
                reg = R.get(int(rid))
                if reg.writable:
                    items[int(rid)] = R.validate_values(reg, [str(x) for x in vals])
            for rid, vals in sorted(items.items()):
                print(f"  $VNWRG,{rid},{','.join(vals)}")
            if not args.no_save:
                print("  $VNWNV")
            if not args.yes and input(f"¿Flashear {len(items)} registros? [s/N] ").strip().lower() not in ("s", "si", "y"):
                return
            res = dev.flash_many(items, save=not args.no_save,
                                 progress=lambda k, n, m: print(f"  [{k}/{n}] {m}"))
            bad = [rid for rid, r in res.items() if isinstance(r, Exception)]
            for rid in bad:
                print(f"[{rid}] ERROR {res[rid]}")
            print(f"Listo: {len(res) - len(bad)} OK, {len(bad)} con error")

        elif args.cmd == "dump":
            ids = [r.id for r in R.REGISTERS if r.writable and r.id != 33]
            res = dev.read_many(ids)
            info = dev.device_info()
            data = {"tipo": "config", "equipo": info,
                    "registros": {str(k): {"nombre": R.get(k).name, "valores": v}
                                  for k, v in res.items() if not isinstance(v, Exception)}}
            txt = json.dumps(data, indent=2, ensure_ascii=False)
            if args.output:
                with open(args.output, "w") as fh:
                    fh.write(txt)
                print(f"Guardado {args.output} ({len(data['registros'])} registros)")
            else:
                print(txt)

        elif args.cmd == "monitor":
            from .livestate import LiveState
            st = LiveState()
            dev.on_ascii = st.ingest_ascii
            dev.on_binary = st.ingest_binary
            print("Ctrl+C para salir")
            while True:
                if st.age("ypr") > 0.5:
                    try:
                        st.ingest_register(27, dev.read_register(27, timeout=0.3, retries=0), "RRG")
                    except (VNTimeout, VNError):
                        pass
                if st.age("ins_status") > 1.0:
                    try:
                        st.ingest_register(63, dev.read_register(63, timeout=0.3, retries=0), "RRG")
                    except (VNTimeout, VNError):
                        pass
                ypr = st.get("ypr") or [float("nan")] * 3
                g = st.get("gyro") or [float("nan")] * 3
                ins = st.get("ins_status")
                mode = R.decode_ins_status(ins)["mode_txt"] if ins is not None else "-"
                print(f"\rYPR {ypr[0]:+8.2f} {ypr[1]:+7.2f} {ypr[2]:+8.2f}  "
                      f"gyro {g[0]*57.2958:+7.2f} {g[1]*57.2958:+7.2f} {g[2]*57.2958:+7.2f} deg/s  "
                      f"INS: {mode:<28}", end="", flush=True)
                time.sleep(0.1)

        elif args.cmd == "log":
            from .livestate import LiveState
            st = LiveState()
            dev.on_ascii = st.ingest_ascii
            dev.on_binary = st.ingest_binary
            st.start_csv(args.output)
            t0 = time.monotonic()
            print(f"Grabando en {args.output} (Ctrl+C para parar)")
            try:
                while not args.seconds or time.monotonic() - t0 < args.seconds:
                    # Sin salida asincrona: consultar actitud/IMU e INS
                    if st.age("ypr") > 0.3:
                        try:
                            st.ingest_register(27, dev.read_register(27, timeout=0.3, retries=0), "RRG")
                        except (VNTimeout, VNError):
                            pass
                    if st.age("ins_status") > 1.0:
                        try:
                            st.ingest_register(63, dev.read_register(63, timeout=0.3, retries=0), "RRG")
                        except (VNTimeout, VNError):
                            pass
                    print(f"\r  {st.csv_rows} filas  {time.monotonic() - t0:6.1f} s", end="", flush=True)
                    time.sleep(0.05)
            finally:
                st.stop_csv()
                print(f"\nGuardado {args.output} ({st.csv_rows} filas)")

        elif args.cmd == "cmd":
            got = []
            dev.on_raw = lambda d, t: got.append((d, t))
            dev.send_raw(args.text)
            time.sleep(0.5)
            for d, t in got:
                print(f"{d} {t}")

        elif args.cmd in ("sgb", "sfb"):
            (dev.set_gyro_bias if args.cmd == "sgb" else dev.set_filter_bias)()
            if args.flash:
                dev.write_settings()
            _print_reg(74, dev.read_register(74))
            print("OK" + (" (guardado en flash)" if args.flash else " (en RAM; usa 'save' para guardarlo)"))

        elif args.cmd == "save":
            dev.write_settings()
            print("OK: configuracion guardada en flash")
        elif args.cmd == "reset":
            dev.reset()
            print("OK: equipo reiniciado")
        elif args.cmd == "factory":
            if input("Restaurar configuracion de fabrica? [s/N] ").strip().lower() in ("s", "si", "y"):
                dev.restore_factory()
                print("OK: configuracion de fabrica restaurada")
    except KeyboardInterrupt:
        print()
    finally:
        dev.close()


if __name__ == "__main__":
    main()
