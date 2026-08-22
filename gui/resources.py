"""Rutas a los recursos gráficos de la aplicación (icono de ventana,
logo grande para el diálogo "Acerca de..." y splash de arranque).

Cada ruta se busca en varias ubicaciones posibles, en vez de asumir
siempre `Path(__file__).resolve().parent.parent` (el único sitio donde
se buscaba antes, válido solo ejecutando desde el árbol de fuentes con
`python main.py gui`). En un ejecutable empaquetado con PyInstaller
(Windows/macOS) los PNG viven dentro de `sys._MEIPASS`; instalado vía
`.deb`/`.rpm`/AppImage en Linux, viven en una ruta de sistema tipo
`/usr/share/p2p-total` — sin esto, el icono de ventana, el de la
bandeja del sistema y el splash de AnabasaSoft se quedarían sin cargar
en cualquier build empaquetado, sin ningún error visible (`QIcon`/
`QPixmap` degradan en silencio si el fichero no existe).
"""

import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parent.parent

# Candidatos, en el orden en que se comprueban:
# 1. PyInstaller (onefile y onedir, Windows/macOS/Linux): `sys._MEIPASS`
#    es la carpeta donde el propio PyInstaller coloca en tiempo de
#    ejecución los ficheros de datos añadidos con `--add-data`.
# 2. Instalación de sistema en Linux vía `.deb`/`.rpm`/AppImage (FHS:
#    `/usr/share/<paquete>`, con `/usr/local/share` como alternativa
#    para instalaciones manuales tipo `make install`).
# 3. Árbol de fuentes (modo desarrollo, `python main.py gui`): el
#    comportamiento que ya había antes, como último fallback.
_CANDIDATE_DIRS = [
    Path(sys._MEIPASS) if getattr(sys, "_MEIPASS", None) else None,
    Path("/usr/share/p2p-total"),
    Path("/usr/local/share/p2p-total"),
    _SOURCE_ROOT,
]


def _resource_path(filename: str) -> Path:
    for directory in _CANDIDATE_DIRS:
        if directory is None:
            continue
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    # No se encontró en ningún candidato: se devuelve la ruta del árbol
    # de fuentes igual que antes, para que el fallo sea el mismo que ya
    # había (QIcon/QPixmap degradan con normalidad si no existe).
    return _SOURCE_ROOT / filename


ICON_PATH = _resource_path("IconoCuadrado.png")
LOGO_PATH = _resource_path("Logo.png")
SPLASH_PATH = _resource_path("AnabasaSoft.png")
