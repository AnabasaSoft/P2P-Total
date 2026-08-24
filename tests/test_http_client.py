"""core/http_client.py contra un servidor HTTP síncrono local (nunca
contra la red real, siguiendo el mismo enfoque ya usado para validar
manualmente self_updater.py en el punto 34.1 del backlog): un
`asyncio.start_server` que devuelve una respuesta fija según la
petición recibida es suficiente para probar el parseo de cabeceras,
redirecciones, `Transfer-Encoding: chunked` y streaming a disco, sin
depender de ningún servicio externo."""

import asyncio

import pytest

from core.http_client import _dechunk, http_download, http_get


def test_dechunk_simple():
    data = b"5\r\nHello\r\n6\r\n World\r\n0\r\n\r\n"
    assert _dechunk(data) == b"Hello World"


def test_dechunk_empty_body():
    assert _dechunk(b"0\r\n\r\n") == b""


def test_dechunk_malformed_stops_gracefully():
    assert _dechunk(b"no es un tamano hex\r\nresto") == b""


async def _serve_once(response_bytes: bytes):
    """Levanta un servidor TCP local que contesta `response_bytes` a la
    primera petición que reciba y luego se cierra. Devuelve (host, port)."""
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(response_bytes)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


@pytest.mark.asyncio
async def test_http_get_plain_body_with_content_length():
    body = b"contenido de prueba"
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    server, port = await _serve_once(response)
    async with server:
        result = await http_get(f"http://127.0.0.1:{port}/", timeout=5.0)
    assert result == body


@pytest.mark.asyncio
async def test_http_get_chunked_body():
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
        b"5\r\nHello\r\n0\r\n\r\n"
    )
    server, port = await _serve_once(response)
    async with server:
        result = await http_get(f"http://127.0.0.1:{port}/", timeout=5.0)
    assert result == b"Hello"


@pytest.mark.asyncio
async def test_http_get_follows_redirect():
    body = b"destino final"
    final_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )

    async def handler(reader, writer):
        request_line = await reader.readuntil(b"\r\n")
        await reader.readuntil(b"\r\n\r\n")
        if b"/original" in request_line:
            writer.write(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: /destino\r\n"
                b"Connection: close\r\n\r\n"
            )
        else:
            writer.write(final_response)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        result = await http_get(f"http://127.0.0.1:{port}/original", timeout=5.0)
    assert result == body


@pytest.mark.asyncio
async def test_http_download_streams_to_disk_and_reports_progress(tmp_path):
    body = b"x" * 100_000
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    server, port = await _serve_once(response)
    dest = tmp_path / "descarga.bin"
    progress_calls = []
    async with server:
        await http_download(
            f"http://127.0.0.1:{port}/", dest, timeout=5.0,
            progress_cb=lambda done, total: progress_calls.append((done, total)),
        )
    assert dest.read_bytes() == body
    assert progress_calls
    assert progress_calls[-1][0] == len(body)
    assert progress_calls[-1][1] == len(body)


@pytest.mark.asyncio
async def test_http_download_chunked_streams_to_disk(tmp_path):
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
        b"5\r\nHello\r\n6\r\n World\r\n0\r\n\r\n"
    )
    server, port = await _serve_once(response)
    dest = tmp_path / "descarga_chunked.bin"
    async with server:
        await http_download(f"http://127.0.0.1:{port}/", dest, timeout=5.0)
    assert dest.read_bytes() == b"Hello World"
