"""Bug real reportado por el usuario: la app se cerraba de golpe
(SIGABRT) al hacer búsquedas. Traza real capturada en el journal del
sistema:

    Traceback (most recent call last):
      File "gui/widgets/search_tab.py", line 267, in _on_context_menu
    KeyError: 'username'

Causa: `browse_action` solo se crea en el menú contextual de resultados
cuando el resultado es de Soulseek y trae un usuario en `extra`. Si el
usuario cierra el menú sin elegir nada (Escape o clic fuera de él),
`QMenu.exec()` devuelve `None`; sin comprobar ese caso, el código
comparaba `action == browse_action` -y si `browse_action` tampoco se
había creado (seguía valiendo `None`), la comparación se cumplía por
accidente (`None == None`), entrando en la rama de "examinar usuario"
con un resultado que nunca tuvo esa clave -de ahí el `KeyError` real, no
capturado por PyQt6, que aborta el proceso entero con SIGABRT."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QMenu

from core.download_manager import DownloadManager
from core.models import Network, SearchResult
from gui.widgets.search_tab import SearchResultsPanel


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


def test_closing_menu_without_choosing_does_not_crash_on_missing_username(monkeypatch):
    panel = SearchResultsPanel(DownloadManager())
    panel.resize(800, 600)  # sin tamaño real, indexAt(pos) no da una fila válida
    result = SearchResult(
        network=Network.SOULSEEK, title="cancion.mp3", size_bytes=1_000, source_id="user\\ruta",
    )
    panel._add_or_merge(result)
    panel._table.selectRow(0)
    pos = panel._table.visualRect(panel._table.model().index(0, 0)).center()
    assert panel._table.indexAt(pos).isValid()

    monkeypatch.setattr(QMenu, "exec", lambda self, *a, **k: None)

    panel._on_context_menu(pos)
