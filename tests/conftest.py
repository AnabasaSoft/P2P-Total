"""Fixtures comunes a toda la suite.

`_isolated_db` es autouse: ningún test debe tocar la base de datos real del
usuario (`~/.local/share/p2p-manager/downloads.db`) — sin esto, cualquier
test que ejercite `core/database.py` (directamente, o indirectamente a
través de `SharedLibrary`, que persiste ahí su caché de hashes desde el
punto 34.1 del backlog) escribiría datos de prueba en la base de datos de
producción del usuario que ejecute la suite."""

import pytest

from core import database


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path_factory, monkeypatch):
    # OJO: usa `tmp_path_factory` (un directorio propio, ajeno al del
    # test) y no el `tmp_path` del propio test -si se anidara ahí
    # dentro, un test que además comparta ese mismo `tmp_path` como
    # carpeta compartida (p.ej. tests/test_sharing.py) se encontraría
    # el fichero de la base de datos como si fuese un fichero compartido
    # más, y como ese fichero va cambiando de contenido en cada
    # escritura, rompería cualquier aserción sobre "el primer fichero
    # encontrado" o sobre que un fichero no tocado no cambia de hash.
    db_dir = tmp_path_factory.mktemp("db")
    monkeypatch.setattr(database, "DB_PATH", db_dir / "test.db")
