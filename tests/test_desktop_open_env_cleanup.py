"""Bug real reportado por el usuario: instalada como `.rpm`, la app
lanzaba `kde-open` (en vez de `xdg-open`) para abrir el navegador desde
el diálogo de "hay una versión nueva", y `kde-open` abortaba con SIGABRT
(volcado de núcleo real capturado por systemd-coredump) al arrancar.
Causa: el *runtime hook* de PyInstaller para PyQt6 fija `QT_PLUGIN_PATH`/
`QML2_IMPORT_PATH` a los plugins de Qt6 empaquetados con p2p-total, y
`kde-open` -un Qt6 del propio sistema, ajeno a la app- los heredaba al
lanzarse como subproceso: intentaba cargar el plugin de plataforma
(`libqxcb.so`) de la versión de Qt6 empaquetada, incompatible con la
suya, y abortaba al crear el *event dispatcher*.

`_system_library_path()` debe limpiar también estas dos variables
-además de restaurar `LD_LIBRARY_PATH` a su valor original- solo
mientras dure la llamada a `QDesktopServices.openUrl()`, y reponerlas
después para no afectar al resto del proceso empaquetado."""

import os
import sys
from unittest.mock import patch

from gui.desktop_open import _system_library_path


def test_noop_outside_frozen_linux_build():
    with patch.object(sys, "frozen", False, create=True), patch.object(sys, "platform", "linux"):
        os.environ["QT_PLUGIN_PATH"] = "/bundled/qt/plugins"
        try:
            with _system_library_path():
                assert os.environ["QT_PLUGIN_PATH"] == "/bundled/qt/plugins"
        finally:
            del os.environ["QT_PLUGIN_PATH"]


def test_clears_qt_plugin_and_qml_paths_during_call_and_restores_after():
    env = {
        "LD_LIBRARY_PATH": "/bundled/lib",
        "LD_LIBRARY_PATH_ORIG": "/usr/lib64",
        "QT_PLUGIN_PATH": "/bundled/qt/plugins",
        "QML2_IMPORT_PATH": "/bundled/qt/qml",
    }
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "platform", "linux"), \
            patch.dict(os.environ, env, clear=False):
        with _system_library_path():
            assert os.environ["LD_LIBRARY_PATH"] == "/usr/lib64"
            assert "QT_PLUGIN_PATH" not in os.environ
            assert "QML2_IMPORT_PATH" not in os.environ

        assert os.environ["LD_LIBRARY_PATH"] == "/bundled/lib"
        assert os.environ["QT_PLUGIN_PATH"] == "/bundled/qt/plugins"
        assert os.environ["QML2_IMPORT_PATH"] == "/bundled/qt/qml"


def test_no_ld_library_path_orig_just_unsets_during_call():
    env = {"LD_LIBRARY_PATH": "/bundled/lib", "QT_PLUGIN_PATH": "/bundled/qt/plugins"}
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "platform", "linux"), \
            patch.dict(os.environ, env, clear=False):
        os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
        with _system_library_path():
            assert "LD_LIBRARY_PATH" not in os.environ
            assert "QT_PLUGIN_PATH" not in os.environ

        assert os.environ["LD_LIBRARY_PATH"] == "/bundled/lib"
        assert os.environ["QT_PLUGIN_PATH"] == "/bundled/qt/plugins"
