"""SharedLibrary (core/sharing.py): escaneo de carpetas compartidas,
caché de hashes por (tamaño, mtime) y cálculo selectivo de SHA1/eD2k
según lo que pida cada red (bug real: rescan() sin estas dos cosas
podía tardar horas y congelar el event loop/GUI al conectar Soulseek/
DC++/Gnutella2/eMule con una carpeta compartida grande). También cubre
el punto 34.1 del backlog: la caché de hashes persistida en SQLite
(para no perder el trabajo entre reinicios de la app) y el escaneo en
segundo plano que no bloquea `connect()` (`ensure_scanning`)."""

import asyncio
import os
import time

import pytest

from core import sharing as sharing_module
from core.sharing import SharedLibrary


@pytest.fixture
def shared_file(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(os.urandom(64))
    return tmp_path, f


def test_rescan_without_hashes_leaves_them_empty(shared_file):
    root, _f = shared_file
    lib = SharedLibrary([str(root)])
    lib.rescan(need_sha1=False, need_ed2k=False)
    sf = lib.list_files()[0]
    assert sf.sha1 == b""
    assert sf.ed2k == b""
    assert sf.ed2k_parts == []


def test_rescan_sha1_only_skips_ed2k(shared_file):
    root, _f = shared_file
    lib = SharedLibrary([str(root)])
    lib.rescan(need_ed2k=False)
    sf = lib.list_files()[0]
    assert sf.sha1 != b""
    assert sf.ed2k == b""


def test_rescan_unchanged_file_reuses_cached_hash(shared_file):
    root, _f = shared_file
    lib = SharedLibrary([str(root)])
    lib.rescan()
    sha1_before = lib.list_files()[0].sha1

    lib.rescan()  # nada ha cambiado: no debería recalcular nada
    assert lib.list_files()[0].sha1 == sha1_before


def test_rescan_modified_file_rehashes(shared_file):
    root, f = shared_file
    lib = SharedLibrary([str(root)])
    lib.rescan()
    sha1_before = lib.list_files()[0].sha1

    time.sleep(0.01)
    f.write_bytes(os.urandom(64))  # mismo tamaño, contenido distinto -> cambia mtime
    lib.rescan()
    assert lib.list_files()[0].sha1 != sha1_before


def test_rescan_can_add_missing_hash_later_reusing_cache(shared_file):
    """Simula G2 (solo SHA1) conectando antes que eMule (SHA1 + eD2k)
    sobre la misma SharedLibrary compartida entre redes: el segundo
    rescan() debe añadir el eD2k que faltaba sin perder ni recalcular
    el SHA1 ya cacheado."""
    root, _f = shared_file
    lib = SharedLibrary([str(root)])
    lib.rescan(need_ed2k=False)
    sha1_only = lib.list_files()[0].sha1
    assert lib.list_files()[0].ed2k == b""

    lib.rescan(need_sha1=False, need_ed2k=True)
    sf = lib.list_files()[0]
    assert sf.sha1 == sha1_only
    assert sf.ed2k != b""


def test_hash_cache_persists_across_instances(shared_file, monkeypatch):
    """Simula un reinicio de la app: una segunda SharedLibrary (nueva
    instancia, como al arrancar de nuevo) sobre la misma carpeta debe
    reutilizar los hashes ya guardados en SQLite por la primera, sin
    volver a leer/hashear el fichero -aquí forzado con un `_hash_file`
    que falla si se le llama, para detectar cualquier rehash de más."""
    root, _f = shared_file
    lib1 = SharedLibrary([str(root)])
    lib1.rescan()
    sha1_before, ed2k_before = lib1.list_files()[0].sha1, lib1.list_files()[0].ed2k

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("no debería recalcular un hash ya cacheado en SQLite")

    monkeypatch.setattr(sharing_module, "_hash_file", _fail_if_called)
    lib2 = SharedLibrary([str(root)])  # "reinicio": instancia nueva, misma base de datos
    lib2.rescan()
    sf = lib2.list_files()[0]
    assert sf.sha1 == sha1_before
    assert sf.ed2k == ed2k_before


async def test_ensure_scanning_does_not_block_caller(shared_file):
    """`ensure_scanning` debe volver al instante -a diferencia de `await
    asyncio.to_thread(rescan, ...)`- dejando que el backend termine su
    propio `connect()` de inmediato mientras el índice se rellena en
    segundo plano."""
    root, _f = shared_file
    lib = SharedLibrary([str(root)])
    lib.ensure_scanning()
    assert lib.scanning
    assert lib.list_files() == []  # todavía no le ha tocado el turno al hilo de fondo

    await lib._scan_task
    assert not lib.scanning
    assert len(lib.list_files()) == 1


async def test_ensure_scanning_does_not_launch_duplicate_scan(shared_file):
    root, _f = shared_file
    lib = SharedLibrary([str(root)])
    lib.ensure_scanning()
    first_task = lib._scan_task
    lib.ensure_scanning()  # ya hay uno en marcha: no debe lanzar un segundo
    assert lib._scan_task is first_task
    await first_task


async def test_ensure_scanning_merges_requirements_from_several_networks(shared_file):
    """Simula G2 (solo SHA1) y eMule (SHA1 + eD2k) conectando casi a la
    vez sobre la misma SharedLibrary compartida entre redes: el
    resultado final debe tener ambos hashes, sin que el segundo
    `ensure_scanning` se quede esperando a que el primero termine."""
    root, _f = shared_file
    lib = SharedLibrary([str(root)])
    lib.ensure_scanning(need_ed2k=False)
    lib.ensure_scanning(need_sha1=False, need_ed2k=True)

    await lib._scan_task
    sf = lib.list_files()[0]
    assert sf.sha1 != b""
    assert sf.ed2k != b""
