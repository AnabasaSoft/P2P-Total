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
`LD_LIBRARY_PATH` modificado para seguir funcionando).

Segunda vuelta del mismo bug, esta vez con volcado de núcleo real:
en un escritorio KDE, `QDesktopServices.openUrl()` no cae a `xdg-open`
sino a `kde-open` -un binario Qt6 del propio sistema, ajeno a la app-,
que abortaba con SIGABRT en `QGuiApplicationPrivate::createEventDispatcher`
nada más arrancar. Causa: el *runtime hook* de PyInstaller para PyQt6
(`pyi_rth_pyqt6.py`) fija `QT_PLUGIN_PATH`/`QML2_IMPORT_PATH` a los
plugins de Qt6 empaquetados con p2p-total en cuanto arranca el proceso
-sin guardar ningún valor "original" como sí hace con
`LD_LIBRARY_PATH`-, y ese `kde-open` hijo los hereda: intenta cargar el
plugin de plataforma (`libqxcb.so`) de la versión de Qt6 empaquetada en
vez de la suya propia del sistema, y aborta por incompatibilidad justo
al crear el *event dispatcher*. Se limpian también estas dos variables
alrededor de la llamada, igual que `LD_LIBRARY_PATH`."""

import os
import sys
from contextlib import contextmanager

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices

# `LD_LIBRARY_PATH` tiene un valor "original" guardado por PyInstaller en
# `LD_LIBRARY_PATH_ORIG` al que restaurar; el resto no lo tienen -el
# propio proceso `kde-open`/`xdg-open` no las necesitaba antes de que
# p2p-total las contaminara, así que basta con quitarlas del todo.
_VARS_WITH_ORIG = {"LD_LIBRARY_PATH": "LD_LIBRARY_PATH_ORIG"}
_VARS_TO_CLEAR = ["QT_PLUGIN_PATH", "QML2_IMPORT_PATH"]


@contextmanager
def _system_library_path():
    if not (getattr(sys, "frozen", False) and sys.platform.startswith("linux")):
        yield
        return
    saved: dict[str, str | None] = {}
    for var, orig_var in _VARS_WITH_ORIG.items():
        saved[var] = os.environ.get(var)
        clean = os.environ.get(orig_var)
        if clean is not None:
            os.environ[var] = clean
        else:
            os.environ.pop(var, None)
    for var in _VARS_TO_CLEAR:
        saved[var] = os.environ.get(var)
        os.environ.pop(var, None)
    try:
        yield
    finally:
        for var, value in saved.items():
            if value is not None:
                os.environ[var] = value
            else:
                os.environ.pop(var, None)


def open_url(url: str) -> None:
    with _system_library_path():
        QDesktopServices.openUrl(QUrl(url))


def open_local_path(path: str) -> None:
    with _system_library_path():
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
