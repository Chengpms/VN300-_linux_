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

# -----------------------------------------------------------------------------
#  Entorno
# -----------------------------------------------------------------------------
ensure_venv() {
    if [ ! -x "$PY" ]; then
        echo "Creando entorno virtual en $DIR/.venv ..."
        python3 -m venv "$DIR/.venv" || die "No se pudo crear el venv (instala python3-venv)"
        "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt" || die "Fallo instalando dependencias"
        ok "Dependencias instaladas"
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
    [ -n "$port" ] || die "No se encontro ningun puerto. Conecta el VN-300, usa VN300_PORT=... o el modo 'sim'."
    PORT_ARGS=(-p "$port")
    [ -n "${VN300_BAUD:-}" ] && PORT_ARGS+=(-b "$VN300_BAUD")
    echo "${C}→ puerto: $port${VN300_BAUD:+ @ $VN300_BAUD}${N}" >&2
}

# -----------------------------------------------------------------------------
#  Modos
# -----------------------------------------------------------------------------
mode_setup() {
    echo "${B}== Instalacion ==${N}"
    command -v python3 >/dev/null || die "Falta python3"
    ensure_venv
    "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
    ok "Entorno listo ($("$PY" --version))"
    mode_doctor
}

mode_doctor() {
    echo "${B}== Diagnostico ==${N}"
    if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
        ok "El usuario $USER esta en el grupo dialout"
    else
        warn "El usuario $USER NO esta en dialout:  sudo usermod -aG dialout $USER  (y reiniciar sesion)"
    fi
    local found=0 p
    for p in /dev/ttyUSB* /dev/ttyACM*; do
        [ -e "$p" ] || continue
        found=1
        if [ -r "$p" ] && [ -w "$p" ]; then ok "$p accesible"; else warn "$p sin permisos de lectura/escritura"; fi
        local dev; dev="$(basename "$p")"
        local lt="/sys/bus/usb-serial/devices/$dev/latency_timer"
        if [ -r "$lt" ]; then
            local v; v="$(cat "$lt")"
            if [ "$v" -le 2 ]; then ok "latency_timer de $dev = ${v} ms"
            else warn "latency_timer de $dev = ${v} ms (recomendado 1 ms a alta frecuencia: ./vn300.sh latencia)"; fi
        fi
    done
    [ $found -eq 1 ] || warn "No hay /dev/ttyUSB* ni /dev/ttyACM* (equipo desconectado?)"
    if ls /dev/serial/by-id/ >/dev/null 2>&1; then
        echo "  Por id:"; ls -1 /dev/serial/by-id/ | sed 's/^/    /'
    fi
    if systemctl is-active --quiet ModemManager 2>/dev/null; then
        warn "ModemManager esta activo: puede abrir el puerto y enviar comandos AT al conectar el equipo."
        echo "    Si hay problemas: sudo systemctl disable --now ModemManager"
    else
        ok "ModemManager no interfiere"
    fi
    local port; port="$(detect_port)"
    [ -n "$port" ] && echo "  Puerto que se usara: ${B}$port${N}"
}

mode_latencia() {
    local port="${1:-$(detect_port)}"
    [ -n "$port" ] || die "No hay puerto"
    local dev; dev="$(basename "$(readlink -f "$port")")"
    local lt="/sys/bus/usb-serial/devices/$dev/latency_timer"
    [ -e "$lt" ] || die "$dev no es un adaptador FTDI/usb-serial con latency_timer"
    echo "latency_timer actual de $dev: $(cat "$lt") ms → 1 ms (requiere sudo; se pierde al desconectar)"
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
    [ $# -gt 0 ] || die "Uso: ./vn300.sh leer <ID> [ID ...]     p.ej. leer 8 63 98"
    port_args; ctl "${PORT_ARGS[@]}" read "$@"
}

mode_escribir() {
    [ $# -gt 0 ] || die "Uso: ./vn300.sh escribir <ID> <valores...> [-- <ID> <valores...>] [--flash]"
    port_args; ctl "${PORT_ARGS[@]}" write "$@"
}

mode_flashear() {
    local f="${1:-}"
    [ -n "$f" ] || die "Uso: ./vn300.sh flashear <config.json>"
    [ -f "$f" ] || die "No existe $f"
    port_args; ctl "${PORT_ARGS[@]}" flash "$f"
}

mode_respaldo() {
    mkdir -p "$CONFIG_DIR"
    local out="${1:-$CONFIG_DIR/respaldo_$(date +%Y%m%d_%H%M%S).json}"
    port_args; ctl "${PORT_ARGS[@]}" dump -o "$out"
    ok "Respaldo en $out"
}

mode_restaurar() {
    mkdir -p "$CONFIG_DIR"
    local f="${1:-}"
    if [ -z "$f" ]; then
        f="$(ls -1t "$CONFIG_DIR"/*.json 2>/dev/null | head -1 || true)"
        [ -n "$f" ] || die "No hay respaldos en $CONFIG_DIR"
        echo "Ultimo respaldo: $f"
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
        cat <<EOF
Uso: ./vn300.sh salida <tipo> <Hz> [--flash]
  tipo: off ypr qtn qmr mag acc gyr mar ymr yba yia imu gps gpe ins ine isl ise dtv g2s g2e (o el numero)
  Hz:   1 2 4 5 10 20 25 40 50 100 200
EOF
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

mode_cmd()      { [ $# -gt 0 ] || die "Uso: ./vn300.sh cmd \"RRG,8\""; port_args; ctl "${PORT_ARGS[@]}" cmd "$1"; }
mode_bias() {
    # ./vn300.sh bias gyro|filtro  -> $VNSGB / $VNSFB + guardar en flash
    case "${1:-}" in
        gyro|giro|sgb)  port_args; ctl "${PORT_ARGS[@]}" sgb --flash ;;
        filtro|ins|sfb) port_args; ctl "${PORT_ARGS[@]}" sfb --flash ;;
        *) die "Uso: ./vn300.sh bias gyro|filtro   (equipo quieto y filtro convergido)" ;;
    esac
}
mode_guardar()  { port_args "${1:-}"; ctl "${PORT_ARGS[@]}" save; }
mode_reset()    { port_args "${1:-}"; ctl "${PORT_ARGS[@]}" reset; }
mode_fabrica()  { port_args "${1:-}"; ctl "${PORT_ARGS[@]}" factory; }

mode_ayuda() {
    cat <<EOF
${B}vn300.sh — VN-300 Control${N}  (configuracion firmware v0.5.0.0, manual UM005 rev. 2.22)

${B}Interfaz${N}
  ui [puerto]                 interfaz completa (F1..F5). Sin puerto: autodetecta
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

Variables: VN300_PORT=/dev/ttyUSB0  VN300_BAUD=921600
EOF
}

# -----------------------------------------------------------------------------
#  Menu interactivo
# -----------------------------------------------------------------------------
pause() { echo; read -rp "Enter para volver al menu..." _ || true; }

menu() {
    while true; do
        clear 2>/dev/null || true
        local port; port="$(detect_port)"
        echo "${B}╔══════════════════════════════════════╗${N}"
        echo "${B}║          VN-300 Control (Linux)      ║${N}"
        echo "${B}╚══════════════════════════════════════╝${N}"
        if [ -n "$port" ]; then echo " Puerto: ${G}$port${N}"; else echo " Puerto: ${R}no detectado${N}"; fi
        cat <<EOF

  1) Interfaz completa (en vivo + registros)
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
        read -rp "Opcion: " op || exit 0
        case "$op" in
            1)  ( mode_ui ) || true ;;
            2)  ( mode_sim ) || true ;;
            3)  ( mode_monitor ) || true; pause ;;
            4)  ( mode_info ) || true; pause ;;
            5)  read -rp "IDs (ej: 8 63 98): " ids; ( mode_leer $ids ) || true; pause ;;
            6)  echo "Formato: ID valores...  (varios: 6 14 -- 7 40)"
                read -rp "> " spec
                read -rp "¿Guardar en flash al terminar? [s/N] " f
                if [[ "${f,,}" == s* ]]; then ( mode_escribir $spec --flash ) || true
                else ( mode_escribir $spec ) || true; fi
                pause ;;
            7)  read -rp "Tipo (ymr, ins, imu, gps, off...): " t
                read -rp "Frecuencia Hz (1..200): " hz
                read -rp "¿Guardar en flash? [s/N] " f
                if [[ "${f,,}" == s* ]]; then ( mode_salida "$t" "$hz" --flash ) || true
                else ( mode_salida "$t" "$hz" ) || true; fi
                pause ;;
            8)  ( mode_respaldo ) || true; pause ;;
            9)  ls -1t "$CONFIG_DIR"/*.json 2>/dev/null | head -10 | nl || echo "(sin respaldos en $CONFIG_DIR)"
                read -rp "Archivo (Enter = el mas reciente): " f
                ( mode_restaurar "$f" ) || true; pause ;;
            10) read -rp "Segundos (0 = hasta Ctrl+C): " s
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
        salida|async)         mode_salida "$@" ;;
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
        *) err "Modo desconocido: $m"; mode_ayuda; exit 1 ;;
    esac
}

main "$@"
