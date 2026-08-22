"""
Punto de entrada de la GUI. Usa qasync para fundir el bucle de eventos
de Qt con el de asyncio en uno solo: así los slots pueden lanzar
`asyncio.ensure_future(...)` directamente sobre los mismos backends
async ya validados por la CLI, sin hilos ni colas intermedias.
"""

import asyncio
import sys

import qasync
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import QApplication, QSplashScreen

from core.config import load_config
from gui import i18n, theme
from gui.main_window import MainWindow
from gui.resources import ICON_PATH, SPLASH_PATH


def run_gui() -> None:
    config = load_config()
    i18n.set_language(config.ui.language)

    app = QApplication(sys.argv)
    app.setApplicationName(i18n.t("app_title"))
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    theme.apply_theme(app, config.ui.theme)

    splash = QSplashScreen(QPixmap(str(SPLASH_PATH)))
    splash.show()
    app.processEvents()

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()
    splash.finish(window)

    with loop:
        loop.run_forever()
