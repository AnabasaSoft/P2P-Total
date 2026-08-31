"""Punto 44 del backlog (v1): `EMuleBackend.download_range()`/
`_download_range_from_source()` -reutiliza el handshake cliente-cliente
existente (OP_HELLO/OP_SETREQFILEID/OP_STARTUPLOADREQ) y acota el
bucle de OP_REQUESTPARTS/OP_SENDINGPART a un tramo [start, end) en vez
de al fichero completo desde 0, escribiendo en el offset que le
corresponde de un fichero de destino ya existente (compartido con los
tramos de otras redes en la descarga agregada)."""

import asyncio
import os

import pytest

from backends.emule_backend import EMuleBackend
from core.models import Network, SearchResult
from core.sharing import SharedLibrary


@pytest.mark.asyncio
async def test_download_range_from_source_writes_exact_slice(tmp_path):
    shared_dir = tmp_path / "compartido"
    shared_dir.mkdir()
    content = os.urandom(500_000)
    (shared_dir / "a.bin").write_bytes(content)

    lib = SharedLibrary([str(shared_dir)])
    lib.rescan()
    shared = lib.list_files()[0]

    server = EMuleBackend(listen_port=0, kad_udp_port=0, shared_library=lib, obfuscation="disabled")
    await server.connect()
    try:
        server_port = server._tcp_server.sockets[0].getsockname()[1]

        client = EMuleBackend(listen_port=0, kad_udp_port=0, obfuscation="disabled")
        out_path = tmp_path / "destino.bin"
        out_path.touch()

        progress: list[int] = []
        ok = await client._download_range_from_source(
            "127.0.0.1", server_port, shared.ed2k, str(out_path), 100_000, 300_000,
            on_progress=progress.append,
        )

        assert ok is True
        written = out_path.read_bytes()
        assert written[100_000:300_000] == content[100_000:300_000]
        assert progress[-1] == 200_000
    finally:
        await server.disconnect()


@pytest.mark.asyncio
async def test_download_range_raises_without_reachable_sources(tmp_path):
    """Sin servidor eD2k ni contactos Kad configurados, no hay forma de
    descubrir fuentes: debe fallar con un mensaje claro en vez de
    quedarse colgado o descargar 0 bytes en silencio (semántica
    todo-o-nada de la descarga agregada)."""
    client = EMuleBackend(listen_port=0, kad_udp_port=0, obfuscation="disabled")
    out_path = tmp_path / "destino.bin"
    out_path.touch()

    result = SearchResult(
        network=Network.EMULE, title="a.bin", size_bytes=500_000,
        source_id=f"{os.urandom(16).hex()}:::500000:::a.bin",
        seeds_or_sources=0,
    )
    with pytest.raises(RuntimeError, match="no se encontraron fuentes"):
        await client.download_range(result, str(out_path), 0, 100_000)
