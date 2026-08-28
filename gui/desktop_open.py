"""Abrir una URL o una carpeta con la aplicación por defecto del sistema
(navegador, gestor de archivos...).

Bug real reportado por el usuario: el botón "Descargar" del diálogo de
"hay una versión nueva" abre bien el navegador ejecutando `python
main.py`, pero no hace nada instalado vía `.rpm` (mismo problema, aunque
no reportado explícitamente, en `.deb`/AppImage y en "abrir carpeta" de
Transferencias). Causa: el ejecutable empaquetado lo genera PyInstaller
(`packaging/p2p-total.spec`), y su bootloader en Linux modifica
`LD_LIBRARY_PATH` para que el propio proceso encuentre ahí las librerías
compartidas que trae incluidas en vez de las del sistema.
`QDesktopServices.openUrl()`, cuando no hay disponible un portal de
escritorio por DBus (`xdg-desktop-portal`), cae a lanzar `xdg-open` como
subproceso -que hereda ese `LD_LIBRARY_PATH` ya modificado y puede fallar
al cargar sus propias dependencias (u otras herramientas del sistema que
invoque por debajo, como el intérprete de Python del sistema), sin
ningún error visible en la GUI. PyInstaller guarda el valor original en
`LD_LIBRARY_PATH_ORIG` precisamente para este caso (ver
https://pyinstaller.org/en/stable/runtime-information.html); aquí se
restaura solo alrededor de la llamada, y se repone después para no
afectar al resto del proceso (el propio `p2p-total` sí necesita su
`LD_LIBRARY_PATH` modificado para seguir funcionando)."""

import os
import sys
from contextlib import contextmanager

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices


@contextmanager
def _system_library_path():
    if not (getattr(sys, "frozen", False) and sys.platform.startswith("linux")):
        yield
        return
    original = os.environ.get("LD_LIBRARY_PATH")
    clean = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if clean is not None:
        os.environ["LD_LIBRARY_PATH"] = clean
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)
    try:
        yield
    finally:
        if original is not None:
            os.environ["LD_LIBRARY_PATH"] = original
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)


def open_url(url: str) -> None:
    with _system_library_path():
        QDesktopServices.openUrl(QUrl(url))


def open_local_path(path: str) -> None:
    with _system_library_path():
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
