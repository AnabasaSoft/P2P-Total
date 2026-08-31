"""Punto 44 del backlog (v1): `G2Backend.download_range()` -mismo GET
/uri-res/N2R que `start_download`, pero con un `Range: bytes=a-b`
explícito para pedir un tramo concreto, escrito en el offset que le
corresponde de un fichero de destino ya existente (compartido con los
tramos de otras redes en la descarga agregada)."""

import asyncio
import base64
import os

import pytest

from backends.g2_backend import G2Backend, sha1_to_urn_base32
from core.models import Network, SearchResult
from core.sharing import SharedLibrary


async def _wait_shared(lib: SharedLibrary, timeout: float = 10.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if lib.list_files():
            return lib.list_files()[0]
        await asyncio.sleep(0.05)
    pytest.fail("El fichero compartido no se indexó a tiempo")


@pytest.mark.asyncio
async def test_download_range_writes_exact_slice_at_offset(tmp_path):
    shared_dir = tmp_path / "compartido"
    shared_dir.mkdir()
    content = os.urandom(200_000)
    (shared_dir / "a.bin").write_bytes(content)

    lib = SharedLibrary([str(shared_dir)])
    lib.rescan()
    shared = lib.list_files()[0]

    server = G2Backend(listen_port=0, shared_library=lib)
    await server.connect()
    try:
        port = server._share_server.sockets[0].getsockname()[1]
        sha1_b32 = sha1_to_urn_base32(shared.sha1)
        result = SearchResult(
            network=Network.GNUTELLA2, title="a.bin", size_bytes=shared.size,
            source_id=f"127.0.0.1:{port}:::{sha1_b32}:::{'0' * 32}:::a.bin",
            seeds_or_sources=1,
        )

        out_path = tmp_path / "destino.bin"
        out_path.touch()

        client = G2Backend(listen_port=0)
        progress: list[int] = []
        await client.download_range(result, str(out_path), 50_000, 120_000, on_progress=progress.append)

        written = out_path.read_bytes()
        assert written[50_000:120_000] == content[50_000:120_000]
        assert progress[-1] == 70_000
    finally:
        await server.disconnect()


@pytest.mark.asyncio
async def test_download_range_from_byte_zero_still_gets_206(tmp_path):
    """Bug real detectado al escribir esta función: el servidor
    confundía 'sin Range:' con 'Range: pidiendo desde el byte 0' y
    contestaba 200 en ambos casos -lo que hacía fallar cualquier tramo
    agregado al que le tocase justo el principio del fichero."""
    shared_dir = tmp_path / "compartido"
    shared_dir.mkdir()
    content = os.urandom(100_000)
    (shared_dir / "a.bin").write_bytes(content)

    lib = SharedLibrary([str(shared_dir)])
    lib.rescan()
    shared = lib.list_files()[0]

    server = G2Backend(listen_port=0, shared_library=lib)
    await server.connect()
    try:
        port = server._share_server.sockets[0].getsockname()[1]
        sha1_b32 = sha1_to_urn_base32(shared.sha1)
        result = SearchResult(
            network=Network.GNUTELLA2, title="a.bin", size_bytes=shared.size,
            source_id=f"127.0.0.1:{port}:::{sha1_b32}:::{'0' * 32}:::a.bin",
            seeds_or_sources=1,
        )

        out_path = tmp_path / "destino.bin"
        out_path.touch()

        client = G2Backend(listen_port=0)
        await client.download_range(result, str(out_path), 0, 40_000)

        assert out_path.read_bytes()[:40_000] == content[:40_000]
    finally:
        await server.disconnect()


@pytest.mark.asyncio
async def test_download_range_raises_when_origin_ignores_range(tmp_path):
    """Un servent que no soporta 'Range:' contesta siempre 200 con el
    fichero entero: en descarga agregada eso no vale (escribiría todo
    el fichero encima del tramo, pisando el trabajo de las otras
    redes), así que debe fallar con un error claro en vez de aceptar
    la respuesta como si fuera un 206 normal."""
    content = os.urandom(50_000)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(content)).encode() + b"\r\n\r\n" + content
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        result = SearchResult(
            network=Network.GNUTELLA2, title="a.bin", size_bytes=len(content),
            source_id=f"127.0.0.1:{port}:::{base64.b32encode(os.urandom(20)).decode().rstrip('=')}:::a.bin",
            seeds_or_sources=1,
        )
        out_path = tmp_path / "destino.bin"
        out_path.touch()

        client = G2Backend(listen_port=0)
        with pytest.raises(RuntimeError, match="Range"):
            await client.download_range(result, str(out_path), 10_000, 20_000)
