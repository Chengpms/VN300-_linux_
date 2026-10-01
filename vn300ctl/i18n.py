"""
Idioma de la interfaz (castellano / ingles).

Los textos se escriben en castellano en el codigo y se envuelven con _():
    _("Conectado a {0} @ {1} baud").format(port, baud)
En ingles se buscan en i18n_en.EN; si falta una traduccion se muestra el original.

El idioma se decide una sola vez, al importar este modulo (antes de que el resto
construya sus catalogos), por este orden:
    1. --lang es|en en la linea de comandos
    2. variable de entorno VN300_LANG
    3. eleccion guardada desde la interfaz (~/.config/vn300ctl/settings.json)
    4. idioma del sistema (LC_ALL / LC_MESSAGES / LANG): es_* -> castellano, si no ingles
"""

from __future__ import annotations

import json
import os
import sys

LANGS = {"es": "Español", "en": "English"}
SETTINGS = os.path.expanduser("~/.config/vn300ctl/settings.json")


def _from_argv() -> str:
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--lang" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    return ""


def _saved() -> str:
    try:
        with open(SETTINGS) as fh:
            return str(json.load(fh).get("lang", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def _system() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var, "")
        if val:
            return "es" if val.lower().startswith("es") else "en"
    return "en"


def _detect() -> str:
    for cand in (_from_argv(), os.environ.get("VN300_LANG", ""), _saved()):
        cand = cand.strip().lower()[:2]
        if cand in LANGS:
            return cand
    return _system()


LANG = _detect()

if LANG == "en":
    from .i18n_en import EN as _TABLE
else:
    _TABLE = {}


def _(text: str) -> str:
    return _TABLE.get(text, text)


def save_lang(lang: str) -> None:
    """Recuerda el idioma elegido en la interfaz para los siguientes arranques."""
    data = {}
    try:
        with open(SETTINGS) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        pass
    data["lang"] = lang
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w") as fh:
        json.dump(data, fh, indent=2)
