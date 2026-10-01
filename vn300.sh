#!/usr/bin/env bash
# =============================================================================
#  vn300.sh — modos de funcionamiento de VN-300 Control (Linux)
#
#  Uso:  ./vn300.sh                 menu interactivo
#        ./vn300.sh <modo> [args]   modo directo (ver ./vn300.sh ayuda)
#
#  Variables de entorno:
#        VN300_PORT   puerto (por defecto: autodetecta /dev/serial/by-id o ttyUSB)
#        VN300_BAUD   baudrate fijo (por defecto: autodetectado)
#        VN300_LANG   es | en  (por defecto: el elegido en la interfaz, o el del sistema)
#
#  Bilingue: los mensajes salen en castellano o en ingles (L "castellano" "english").
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PY="$DIR/.venv/bin/python"
CONFIG_DIR="$HOME/vn300_configs"
LOG_DIR="$HOME/vn300_logs"

if [ -t 1 ]; then
    B=$'\e[1m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; C=$'\e[36m'; N=$'\e[0m'
else
    B=""; R=""; G=""; Y=""; C=""; N=""
fi
ok()   { echo "${G}✔${N} $*"; }
warn() { echo "${Y}!${N} $*"; }
err()  { echo "${R}✘${N} $*" >&2; }
die()  { err "$*"; exit 1; }

# Idioma: VN300_LANG > eleccion guardada en la interfaz > idioma del sistema
detect_lang() {
    local l="${VN300_LANG:-}"
    if [ -z "$l" ] && [ -r "$HOME/.config/vn300ctl/settings.json" ]; then
        l="$(sed -n 's/.*"lang": *"\([a-z]*\)".*/\1/p' "$HOME/.config/vn300ctl/settings.json" | head -1)"
    fi
    if [ -z "$l" ]; then
        case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in es*) l=es ;; *) l=en ;; esac
    fi
    case "$l" in en*) echo en ;; *) echo es ;; esac
}
VLANG="$(detect_lang)"
export VN300_LANG="$VLANG"
L() { if [ "$VLANG" = en ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }

# -----------------------------------------------------------------------------
#  Entorno
# -----------------------------------------------------------------------------
ensure_venv() {
    if [ ! -x "$PY" ]; then
        echo "$(L "Creando entorno virtual en" "Creating virtual environment in") $DIR/.venv ..."
        python3 -m venv "$DIR/.venv" || die "$(L "No se pudo crear el venv (instala python3-venv)" "Could not create the venv (install python3-venv)")"
        "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt" || die "$(L "Fallo instalando dependencias" "Failed to install dependencies")"
        ok "$(L "Dependencias instaladas" "Dependencies installed")"
    fi
}

ctl() {
    ensure_venv
    cd "$DIR" && "$PY" -m vn300ctl "$@"
}

# Puerto: VN300_PORT > /dev/serial/by-id (FTDI/VectorNav) > primer ttyUSB/ttyACM
detect_port() {
    if [ -n "${VN300_PORT:-}" ]; then echo "$VN300_PORT"; return; fi
    local p
    for p in /dev/serial/by-id/*; do
        [ -e "$p" ] || continue
        case "$p" in *FTDI*|*VectorNav*|*VN*|*FT232*) echo "$p"; return ;; esac
    done
    for p in /dev/ttyUSB* /dev/ttyACM*; do
        [ -e "$p" ] && { echo "$p"; return; }
    done
    echo ""
}

port_args() {
    local port="${1:-$(detect_port)}"
    [ -n "$port" ] || die "$(L "No se encontro ningun puerto. Conecta el VN-300, usa VN300_PORT=... o el modo 'sim'." "No port found. Connect the VN-300, use VN300_PORT=... or the 'sim' mode.")"
    PORT_ARGS=(-p "$port")
    [ -n "${VN300_BAUD:-}" ] && PORT_ARGS+=(-b "$VN300_BAUD")
    echo "${C}→ $(L puerto port): $port${VN300_BAUD:+ @ $VN300_BAUD}${N}" >&2
}

# -----------------------------------------------------------------------------
#  Modos
# -----------------------------------------------------------------------------
mode_setup() {
    echo "${B}== $(L Instalacion Setup) ==${N}"
    command -v python3 >/dev/null || die "$(L "Falta python3" "python3 is missing")"
    ensure_venv
    "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
    ok "$(L "Entorno listo" "Environment ready") ($("$PY" --version))"
    mode_doctor
}

mode_doctor() {
    echo "${B}== $(L Diagnostico Diagnostics) ==${N}"
    if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
        ok "$(L "El usuario $USER esta en el grupo dialout" "User $USER is in the dialout group")"
    else
        warn "$(L "El usuario $USER NO esta en dialout:  sudo usermod -aG dialout $USER  (y reiniciar sesion)" "User $USER is NOT in dialout:  sudo usermod -aG dialout $USER  (then log in again)")"
    fi
    local found=0 p
    for p in /dev/ttyUSB* /dev/ttyACM*; do
        [ -e "$p" ] || continue
        found=1
        if [ -r "$p" ] && [ -w "$p" ]; then ok "$p $(L accesible accessible)"; else warn "$p $(L "sin permisos de lectura/escritura" "has no read/write permission")"; fi
        local dev; dev="$(basename "$p")"
        local lt="/sys/bus/usb-serial/devices/$dev/latency_timer"
        if [ -r "$lt" ]; then
            local v; v="$(cat "$lt")"
            if [ "$v" -le 2 ]; then ok "latency_timer $(L de of) $dev = ${v} ms"
            else warn "latency_timer $(L de of) $dev = ${v} ms ($(L "recomendado 1 ms a alta frecuencia" "1 ms recommended at high rates"): ./vn300.sh $(L latencia latency))"; fi
        fi
    done
    [ $found -eq 1 ] || warn "$(L "No hay /dev/ttyUSB* ni /dev/ttyACM* (equipo desconectado?)" "No /dev/ttyUSB* or /dev/ttyACM* (device unplugged?)")"
    if ls /dev/serial/by-id/ >/dev/null 2>&1; then
        echo "  $(L "Por id" "By id"):"; ls -1 /dev/serial/by-id/ | sed 's/^/    /'
    fi
    if systemctl is-active --quiet ModemManager 2>/dev/null; then
        warn "$(L "ModemManager esta activo: puede abrir el puerto y enviar comandos AT al conectar el equipo." "ModemManager is active: it may open the port and send AT commands when the device is plugged in.")"
        echo "    $(L "Si hay problemas" "If there are problems"): sudo systemctl disable --now ModemManager"
    else
        ok "$(L "ModemManager no interfiere" "ModemManager is not interfering")"
    fi
    local port; port="$(detect_port)"
    [ -n "$port" ] && echo "  $(L "Puerto que se usara" "Port to be used"): ${B}$port${N}"
}

mode_latencia() {
    local port="${1:-$(detect_port)}"
    [ -n "$port" ] || die "$(L "No hay puerto" "No port")"
    local dev; dev="$(basename "$(readlink -f "$port")")"
    local lt="/sys/bus/usb-serial/devices/$dev/latency_timer"
    [ -e "$lt" ] || die "$dev $(L "no es un adaptador FTDI/usb-serial con latency_timer" "is not an FTDI/usb-serial adapter with latency_timer")"
    echo "latency_timer $(L "actual de" "of") $dev: $(cat "$lt") ms → 1 ms ($(L "requiere sudo; se pierde al desconectar" "needs sudo; lost when unplugged"))"
    echo 1 | sudo tee "$lt" >/dev/null && ok "latency_timer = $(cat "$lt") ms"
}

mode_ui() {
    # Sin puerto detectado se abre igual: se elige en la pestana F1
    local a=()
    if [ $# -gt 0 ] || [ -n "$(detect_port)" ]; then
        port_args "${1:-}"; a=("${PORT_ARGS[@]}")
    fi
    ctl "${a[@]}" tui
}
mode_sim()      { ctl -p sim tui; }
mode_monitor()  { port_args "${1:-}"; ctl "${PORT_ARGS[@]}" monitor; }
mode_info()     { port_args "${1:-}"; ctl "${PORT_ARGS[@]}" info; }
mode_ports()    { ctl ports; }
mode_regs()     { ctl regs; }

mode_leer() {
    [ $# -gt 0 ] || die "$(L "Uso: ./vn300.sh leer <ID> [ID ...]     p.ej. leer 8 63 98" "Usage: ./vn300.sh read <ID> [ID ...]     e.g. read 8 63 98")"
    port_args; ctl "${PORT_ARGS[@]}" read "$@"
}

mode_escribir() {
    [ $# -gt 0 ] || die "$(L "Uso: ./vn300.sh escribir <ID> <valores...> [-- <ID> <valores...>] [--flash]" "Usage: ./vn300.sh write <ID> <values...> [-- <ID> <values...>] [--flash]")"
    port_args; ctl "${PORT_ARGS[@]}" write "$@"
}

mode_flashear() {
    local f="${1:-}"
    [ -n "$f" ] || die "$(L "Uso: ./vn300.sh flashear <config.json>" "Usage: ./vn300.sh flash <config.json>")"
    [ -f "$f" ] || die "$(L "No existe" "Not found:") $f"
    port_args; ctl "${PORT_ARGS[@]}" flash "$f"
}

mode_respaldo() {
    mkdir -p "$CONFIG_DIR"
    local out="${1:-$CONFIG_DIR/respaldo_$(date +%Y%m%d_%H%M%S).json}"
    port_args; ctl "${PORT_ARGS[@]}" dump -o "$out"
    ok "$(L "Respaldo en" "Backup saved to") $out"
}

mode_restaurar() {
    mkdir -p "$CONFIG_DIR"
    local f="${1:-}"
    if [ -z "$f" ]; then
        f="$(ls -1t "$CONFIG_DIR"/*.json 2>/dev/null | head -1 || true)"
        [ -n "$f" ] || die "$(L "No hay respaldos en" "No backups in") $CONFIG_DIR"
        echo "$(L "Ultimo respaldo" "Latest backup"): $f"
    fi
    mode_flashear "$f"
}

mode_grabar() {
    mkdir -p "$LOG_DIR"
    local secs="${1:-0}"
    local out="${2:-$LOG_DIR/vn300_$(date +%Y%m%d_%H%M%S).csv}"
    port_args; ctl "${PORT_ARGS[@]}" log -o "$out" -t "$secs"
}

mode_salida() {
    # Preset rapido de salida ASCII asincrona: ./vn300.sh salida <ADOR> <Hz> [--flash]
    local ador="${1:-}" hz="${2:-}"; shift 2 2>/dev/null || true
    if [ -z "$ador" ] || [ -z "$hz" ]; then
        if [ "$VLANG" = en ]; then cat <<EOF
Usage: ./vn300.sh output <type> <Hz> [--flash]
  type: off ypr qtn qmr mag acc gyr mar ymr yba yia imu gps gpe ins ine isl ise dtv g2s g2e (or the number)
  Hz:   1 2 4 5 10 20 25 40 50 100 200
EOF
        else cat <<EOF
Uso: ./vn300.sh salida <tipo> <Hz> [--flash]
  tipo: off ypr qtn qmr mag acc gyr mar ymr yba yia imu gps gpe ins ine isl ise dtv g2s g2e (o el numero)
  Hz:   1 2 4 5 10 20 25 40 50 100 200
EOF
        fi
        exit 1
    fi
    case "${ador,,}" in
        off) ador=0;; ypr) ador=1;; qtn) ador=2;; qmr) ador=8;; dcm) ador=9;; mag) ador=10;;
        acc) ador=11;; gyr) ador=12;; mar) ador=13;; ymr) ador=14;; yba) ador=16;; yia) ador=17;;
        imu) ador=19;; gps) ador=20;; gpe) ador=21;; ins) ador=22;; ine) ador=23;; isl) ador=28;;
        ise) ador=29;; dtv) ador=30;; g2s) ador=32;; g2e) ador=33;;
    esac
    port_args; ctl "${PORT_ARGS[@]}" write 6 "$ador" -- 7 "$hz" "$@"
}

mode_cmd()      { [ $# -gt 0 ] || die "$(L Uso Usage): ./vn300.sh cmd \"RRG,8\""; port_args; ctl "${PORT_ARGS[@]}" cmd "$1"; }
mode_bias() {
    # ./vn300.sh bias gyro|filtro  -> $VNSGB / $VNSFB + guardar en flash
    case "${1:-}" in
        gyro|giro|sgb)  port_args; ctl "${PORT_ARGS[@]}" sgb --flash ;;
        filtro|filter|ins|sfb) port_args; ctl "${PORT_ARGS[@]}" sfb --flash ;;
        *) die "$(L "Uso: ./vn300.sh bias gyro|filtro   (equipo quieto y filtro convergido)" "Usage: ./vn300.sh bias gyro|filter   (device still, filter converged)")" ;;
    esac
}
mode_guardar()  { port_args "${1:-}"; ctl "${PORT_ARGS[@]}" save; }
mode_reset()    { port_args "${1:-}"; ctl "${PORT_ARGS[@]}" reset; }
mode_fabrica()  { port_args "${1:-}"; ctl "${PORT_ARGS[@]}" factory; }

mode_ayuda() {
    if [ "$VLANG" = en ]; then
    cat <<EOF
${B}vn300.sh — VN-300 Control${N}  (firmware v0.5.0.0 configuration, UM005 manual rev. 2.22)

${B}Interface${N}
  ui [port]                   full interface (F1..F6, with 3D view). No port: auto-detect
  sim                         interface with a simulated VN-300 (no hardware)
  monitor [port]              attitude + INS on one line, plain text

${B}Registers${N}
  info [port]                 model, S/N, firmware, baudrate
  regs                        register catalog
  read <ID...>                reads registers             e.g.: read 8 63 98
  write <ID> <v...> [-- <ID> <v...>] [--flash] [-y]
                              writes one or several       e.g.: write 6 14 -- 7 40 --flash
  output <type> <Hz> [--flash] ASCII async output         e.g.: output ymr 50 --flash
  flash <config.json>         writes a JSON + saves to flash
  backup [file.json]          exports the configuration to ~/vn300_configs/
  restore [file.json]         flashes a backup (the latest by default)
  cmd "<command>"             raw ASCII command           e.g.: cmd "RRG,5"

${B}Device${N}
  save                        \$VNWNV  (save registers to flash)
  reset                       \$VNRST  (restart)
  factory                     \$VNRFS  (factory reset, asks for confirmation)
  bias gyro|filter            \$VNSGB / \$VNSFB: estimated bias -> reg 74 and save to flash

${B}Data${N}
  log [seconds] [file]        records CSV to ~/vn300_logs/ (0 = until Ctrl+C)

${B}System${N}
  setup                       creates the environment and installs dependencies
  doctor                      diagnostics: permissions, ports, ModemManager, latency
  latency [port]              sets the FTDI latency_timer to 1 ms (sudo)
  ports                       lists serial ports

Spanish mode names also work (leer, escribir, salida, respaldo, ...).
Variables: VN300_PORT=/dev/ttyUSB0  VN300_BAUD=921600  VN300_LANG=es|en
EOF
    else
    cat <<EOF
${B}vn300.sh — VN-300 Control${N}  (configuracion firmware v0.5.0.0, manual UM005 rev. 2.22)

${B}Interfaz${N}
  ui [puerto]                 interfaz completa (F1..F6, con vista 3D). Sin puerto: autodetecta
  sim                         interfaz con VN-300 simulado (sin hardware)
  monitor [puerto]            actitud + INS en una linea, texto plano

${B}Registros${N}
  info [puerto]               modelo, N/S, firmware, baudrate
  regs                        catalogo de registros
  leer <ID...>                lee registros               ej: leer 8 63 98
  escribir <ID> <v...> [-- <ID> <v...>] [--flash] [-y]
                              escribe uno o varios        ej: escribir 6 14 -- 7 40 --flash
  salida <tipo> <Hz> [--flash] salida ASCII asincrona     ej: salida ymr 50 --flash
  flashear <config.json>      escribe un JSON + guarda en flash
  respaldo [archivo.json]     exporta la configuracion a ~/vn300_configs/
  restaurar [archivo.json]    flashea un respaldo (por defecto el ultimo)
  cmd "<comando>"             comando ASCII crudo          ej: cmd "RRG,5"

${B}Equipo${N}
  guardar                     \$VNWNV  (guardar registros en flash)
  reset                       \$VNRST  (reiniciar)
  fabrica                     \$VNRFS  (restaurar fabrica, pide confirmacion)
  bias gyro|filtro            \$VNSGB / \$VNSFB: bias estimado -> reg 74 y guardar en flash

${B}Datos${N}
  grabar [segundos] [archivo] graba CSV en ~/vn300_logs/ (0 = hasta Ctrl+C)

${B}Sistema${N}
  setup                       crea el entorno e instala dependencias
  doctor                      diagnostico: permisos, puertos, ModemManager, latencia
  latencia [puerto]           pone latency_timer FTDI a 1 ms (sudo)
  ports                       lista puertos serie

Variables: VN300_PORT=/dev/ttyUSB0  VN300_BAUD=921600  VN300_LANG=es|en
EOF
    fi
}

# -----------------------------------------------------------------------------
#  Menu interactivo
# -----------------------------------------------------------------------------
pause() { echo; read -rp "$(L "Enter para volver al menu..." "Press Enter to return to the menu...")" _ || true; }

menu() {
    while true; do
        clear 2>/dev/null || true
        local port; port="$(detect_port)"
        echo "${B}╔══════════════════════════════════════╗${N}"
        echo "${B}║          VN-300 Control (Linux)      ║${N}"
        echo "${B}╚══════════════════════════════════════╝${N}"
        if [ -n "$port" ]; then echo " $(L Puerto Port): ${G}$port${N}"; else echo " $(L Puerto Port): ${R}$(L "no detectado" "not detected")${N}"; fi
        if [ "$VLANG" = en ]; then cat <<EOF

  1) Full interface (live + registers + 3D)
  2) Interface with the simulator
  3) Quick monitor (text)
  4) Device info
  5) Read registers
  6) Write registers
  7) Configure ASCII async output
  8) Back up configuration
  9) Restore / flash configuration
 10) Record CSV
 11) Save to flash  /  12) Reset  /  13) Factory reset
 14) Diagnostics (doctor)
 15) Install / repair environment
  0) Quit
EOF
        else cat <<EOF

  1) Interfaz completa (en vivo + registros + 3D)
  2) Interfaz con simulador
  3) Monitor rapido (texto)
  4) Info del equipo
  5) Leer registros
  6) Escribir registros
  7) Configurar salida ASCII asincrona
  8) Respaldo de configuracion
  9) Restaurar / flashear configuracion
 10) Grabar CSV
 11) Guardar en flash  /  12) Reset  /  13) Restaurar fabrica
 14) Diagnostico (doctor)
 15) Instalar / reparar entorno
  0) Salir
EOF
        fi
        read -rp "$(L Opcion Option): " op || exit 0
        case "$op" in
            1)  ( mode_ui ) || true ;;
            2)  ( mode_sim ) || true ;;
            3)  ( mode_monitor ) || true; pause ;;
            4)  ( mode_info ) || true; pause ;;
            5)  read -rp "IDs ($(L ej e.g.): 8 63 98): " ids; ( mode_leer $ids ) || true; pause ;;
            6)  echo "$(L "Formato: ID valores...  (varios: 6 14 -- 7 40)" "Format: ID values...  (several: 6 14 -- 7 40)")"
                read -rp "> " spec
                read -rp "$(L "¿Guardar en flash al terminar? [s/N] " "Save to flash afterwards? [y/N] ")" f
                if [[ "${f,,}" == s* || "${f,,}" == y* ]]; then ( mode_escribir $spec --flash ) || true
                else ( mode_escribir $spec ) || true; fi
                pause ;;
            7)  read -rp "$(L Tipo Type) (ymr, ins, imu, gps, off...): " t
                read -rp "$(L "Frecuencia Hz" "Rate Hz") (1..200): " hz
                read -rp "$(L "¿Guardar en flash? [s/N] " "Save to flash? [y/N] ")" f
                if [[ "${f,,}" == s* || "${f,,}" == y* ]]; then ( mode_salida "$t" "$hz" --flash ) || true
                else ( mode_salida "$t" "$hz" ) || true; fi
                pause ;;
            8)  ( mode_respaldo ) || true; pause ;;
            9)  ls -1t "$CONFIG_DIR"/*.json 2>/dev/null | head -10 | nl || echo "($(L "sin respaldos en" "no backups in") $CONFIG_DIR)"
                read -rp "$(L "Archivo (Enter = el mas reciente): " "File (Enter = the latest): ")" f
                ( mode_restaurar "$f" ) || true; pause ;;
            10) read -rp "$(L "Segundos (0 = hasta Ctrl+C): " "Seconds (0 = until Ctrl+C): ")" s
                ( mode_grabar "${s:-0}" ) || true; pause ;;
            11) ( mode_guardar ) || true; pause ;;
            12) ( mode_reset ) || true; pause ;;
            13) ( mode_fabrica ) || true; pause ;;
            14) mode_doctor; pause ;;
            15) mode_setup; pause ;;
            0|q|Q) exit 0 ;;
            *) ;;
        esac
    done
}

# -----------------------------------------------------------------------------
main() {
    local m="${1:-menu}"; shift || true
    case "$m" in
        menu)                 menu ;;
        ui|tui|interfaz)      mode_ui "$@" ;;
        sim|simulador)        mode_sim ;;
        monitor)              mode_monitor "$@" ;;
        info)                 mode_info "$@" ;;
        regs|registros)       mode_regs ;;
        leer|read)            mode_leer "$@" ;;
        escribir|write)       mode_escribir "$@" ;;
        salida|output|async)  mode_salida "$@" ;;
        flashear|flash)       mode_flashear "$@" ;;
        respaldo|backup|dump) mode_respaldo "$@" ;;
        restaurar|restore)    mode_restaurar "$@" ;;
        grabar|log)           mode_grabar "$@" ;;
        cmd)                  mode_cmd "$@" ;;
        guardar|save)         mode_guardar "$@" ;;
        reset)                mode_reset "$@" ;;
        fabrica|factory)      mode_fabrica "$@" ;;
        bias)                 mode_bias "$@" ;;
        setup|instalar)       mode_setup ;;
        doctor|diag)          mode_doctor ;;
        latencia|latency)     mode_latencia "$@" ;;
        ports|puertos)        mode_ports ;;
        ayuda|help|-h|--help) mode_ayuda ;;
        *) err "$(L "Modo desconocido" "Unknown mode"): $m"; mode_ayuda; exit 1 ;;
    esac
}

main "$@"
