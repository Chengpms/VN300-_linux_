# vn300ctl — VN-300 Control para Linux

**Español** · [English](README.md)

Aplicación de consola para el **VectorNav VN-300**, inspirada en VectorNav Control Center:
visualización en vivo, lectura y escritura de registros, y flasheo de uno o varios registros.
Toda la configuración sigue el manual **UM005 para firmware v0.5.0.0 (Document Revision 2.22)**:
los 51 registros que documenta, sus campos y opciones, los comandos (`$VNRRG/$VNWRG/$VNWNV/$VNRFS/
$VNRST/$VNASY/$VNBOM/$VNSGB/$VNSFB`), checksum de 8 bits o CRC16 y la salida binaria `0xFA`.
Si el equipo tiene otro firmware, la app lo avisa al conectar. Los IDs que responda y no estén en
ese manual aparecen como "sin documentar" (lectura y escritura en crudo).

## Arranque

```bash
./vn300                          # abre la interfaz; elige el puerto en F1
./vn300 -p /dev/ttyUSB0          # conecta directamente (baudrate autodetectado)
./vn300 -p sim                   # simulador, para probar sin el equipo
./vn300 --lang en                # interfaz en ingles (o VN300_LANG=en)
```

La primera vez, `./vn300` crea `.venv/` e instala `textual` y `pyserial`.
Si no tienes permiso sobre el puerto: `sudo usermod -aG dialout $USER` y vuelve a iniciar sesión.

## Capturas

Hechas con el simulador integrado (`./vn300 -p sim`), sin hardware conectado.

**F2 En vivo**: actitud, IMU, INS, GPS A/B y vista 3D

![F2 En vivo](docs/img/live.png)

**F2 En vivo**: gráficas (Pitch/Roll, Gyro, Accel, Mag, Velocidad, Yaw)

![F2 gráficas](docs/img/charts.png)

|                                                    |                                                            |
| -------------------------------------------------- | ---------------------------------------------------------- |
| ![F1 Conexión](docs/img/conn.png) **F1 Conexión**  | ![F3 Registros](docs/img/regs.png) **F3 Registros**        |
| ![F4 Consola](docs/img/console.png) **F4 Consola** | ![F5 Herramientas](docs/img/tools.png) **F5 Herramientas** |

**F6 Vista 3D**

![F6 Vista 3D](docs/img/3d.png)

## Interfaz (teclas F1–F6)

| Pestaña             | Qué hace                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **F1 Conexión**     | Puerto, baudrate (auto), info del equipo, salida ASCII asíncrona (reg 6/7), pausar/reanudar, guardar en flash, reset y restaurar fábrica. Selector de **idioma**                                                                                                                                                                                                                                                                                                                       |
| **F2 En vivo**      | Actitud (horizonte, cinta de rumbo y **vista 3D**), IMU, estado INS, GPS A/B, compás GPS. Gráficas braille de alta resolución (Pitch/Roll, Yaw, Gyro, Accel, Mag, Velocidad) con ventana de 2–60 s y congelar (Ctrl+F). Lee el flujo ASCII o binario que ya emita el equipo (si solo manda cuaternión, calcula YPR) y consulta lo que falte. **Grabar CSV** (Ctrl+R) en `~/vn300_logs/`                                                                                                |
| **F3 Registros**    | Escaneo de los 256 IDs al conectar: columna **Estado** (● ACTIVO / ○ inactivo / ✓ responde / ✕ no existe) con detalle (p.ej. salida binaria a 100 Hz, velocity aiding activo). Filtro: documentados + detectados, solo los que responden, solo activos, todos. Formulario por campo, **Leer**, **Escribir** (RAM), **Escribir + flash**, **+ Lote** para flashear varios, exportar/importar JSON (`~/vn300_configs/`). Los registros sin documentar se pueden leer y escribir en crudo |
| **F4 Consola**      | Terminal ASCII: escribe `RRG,8` o `WRG,7,40`; `$VN` y el checksum se añaden solos. Los errores `VNERR` se traducen                                                                                                                                                                                                                                                                                                                                                                     |
| **F5 Herramientas** | Calibración hard/soft iron (reg 44/47 → 23), bias de arranque con `$VNSGB`/`$VNSFB` (→ reg 74), matriz de montaje a partir de yaw/pitch/roll (reg 26) y asistente de salida binaria (reg 75–77) con casillas                                                                                                                                                                                                                                                                           |
| **F6 Vista 3D**     | El VN-300 en 3D a pantalla completa, girando con yaw/pitch/roll en tiempo real: caja con su cara frontal (+X) en naranja, flecha de avance, ejes X/Y/Z del cuerpo y suelo NED con el Norte marcado. Arrastra con el ratón para girar la cámara, rueda para el zoom, doble clic para reiniciar (o flechas, `+`/`-` y `r`)                                                                                                                                                               |

`Ctrl+S` guarda en flash (`$VNWNV`); `Ctrl+Q` sale.

## Idioma

La interfaz, la línea de comandos y `vn300.sh` funcionan en castellano y en inglés. Se elige así,
por orden de prioridad: `--lang es|en`, la variable `VN300_LANG`, el selector de F1 (se recuerda en
`~/.config/vn300ctl/settings.json`) y, si no hay nada, el idioma del sistema. Cambiarlo en F1 reinicia
la interfaz y vuelve a conectar al mismo puerto.

**Escribir y flashear no es lo mismo:** _Escribir_ cambia el registro en RAM, y el cambio se pierde
al reiniciar. _Flash_ ejecuta además `$VNWNV`, que guarda **todos** los registros en memoria no volátil.
El equipo debe estar quieto mientras guarda.

## Script con modos: `vn300.sh`

```bash
./vn300.sh                       # menu interactivo
./vn300.sh ayuda                 # todos los modos
./vn300.sh ui | sim | monitor | info | leer 8 63 | escribir 6 14 -- 7 40 --flash
./vn300.sh salida ymr 50 --flash | respaldo | restaurar | grabar 60 | doctor | latencia
```

`doctor` revisa permisos, ModemManager y el `latency_timer` del FTDI; `latencia` lo pone a 1 ms (sudo).
Puerto por defecto: `VN300_PORT` o el FTDI en `/dev/serial/by-id`.

## Registros 50/51 (velocity aiding)

No están en el manual de firmware v0.5.0.0, así que la app no los define: si el equipo los
responde, salen en F3 como "sin documentar" y se pueden leer o escribir en crudo. En los manuales
VN-100 son _Velocity Compensation Measurement_ (50) y _Control_ (51), y solo compensan la aceleración
centrípeta del filtro de actitud. No son una entrada de odometría para el INS.

## Línea de comandos (para scripts)

```bash
./vn300 ports
./vn300 -p /dev/ttyUSB0 info
./vn300 -p /dev/ttyUSB0 read 8 63 98
./vn300 -p /dev/ttyUSB0 write 7 40                       # un registro, solo RAM
./vn300 -p /dev/ttyUSB0 write 6 14 -- 7 40 --flash       # varios + guardar en flash
./vn300 -p /dev/ttyUSB0 write 57 0.12 0 -0.30 --flash -y # sin confirmación
./vn300 -p /dev/ttyUSB0 dump -o mi_config.json           # respaldo de la configuración
./vn300 -p /dev/ttyUSB0 flash mi_config.json             # restaura o clona la configuración
./vn300 -p /dev/ttyUSB0 monitor                          # actitud e INS en texto plano
./vn300 -p /dev/ttyUSB0 cmd "RRG,5"
./vn300 -p /dev/ttyUSB0 save | reset | factory
./vn300 -p /dev/ttyUSB0 sgb --flash                      # $VNSGB: bias del giroscopo -> reg 74
./vn300 -p /dev/ttyUSB0 sfb --flash                      # $VNSFB: bias del filtro INS -> reg 74
./vn300 regs                                             # catálogo de registros
```

## Notas

- Al cambiar el baudrate (reg 5), la app comprueba a qué velocidad quedó el puerto y se reconecta.
  En un lote, el reg 5 se escribe al final para no cortar la secuencia.
- Si el equipo usa CRC16 (reg 30, `SerialChecksum=3`), se detecta en la primera respuesta y se usa CRC.
- No incluye la actualización de firmware (`$VNFWU`, protocolo AN013).

## Copyright

Copyright © 2026 Cheng Marquet. Todos los derechos reservados.

## Estructura

```
vn300ctl/protocol.py   checksum/CRC, tramas ASCII, parser binario (tablas del manual §5)
vn300ctl/registers.py  catálogo de los 51 registros del firmware v0.5.0.0: campos, opciones, validación
vn300ctl/device.py     puerto serie, hilo lector, comandos con reintentos, autobaud, flash por lotes
vn300ctl/livestate.py  estado en vivo unificado (ASCII, binario o consulta) y CSV
vn300ctl/sim.py        VN-300 simulado
vn300ctl/tui.py        interfaz Textual
vn300ctl/view3d.py     vista 3D de la actitud (rasterizado en la terminal)
vn300ctl/charts.py     graficas braille
vn300ctl/i18n.py       idioma; traducciones al ingles en i18n_en.py
vn300ctl/cli.py        subcomandos
```
