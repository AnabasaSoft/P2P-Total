"""Fija `VERSION` en `core/version.py` a partir de un argumento de línea
de comandos. Usado por el workflow de CI
(.github/workflows/build-packages.yml) antes de compilar cada paquete, en
los 4 sistemas operativos - en Python en vez de `sed -i` porque su
sintaxis difiere entre GNU sed (Linux, y Windows vía el bash de MSYS) y
BSD sed (macOS), lo que rompió el build de macOS la primera vez que se
probó (ver DEVLOG.md)."""

import re
import sys
from pathlib import Path

version = sys.argv[1]
path = Path(__file__).resolve().parent.parent / "core" / "version.py"
text = path.read_text()
new_text = re.sub(r'(?m)^VERSION = ".*"', f'VERSION = "{version}"', text)
if new_text == text:
    raise SystemExit(f'No se encontró la línea VERSION = "..." en {path}')
path.write_text(new_text)
print(f'{path}: VERSION = "{version}"')
