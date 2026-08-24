"""
Cliente HTTP(S) mínimo sobre `asyncio.open_connection`/TLS nativo, sin
depender de `requests` ni `aiohttp` (ninguna librería de terceros que
reimplemente el trabajo de red por nosotros), siguiendo la misma
restricción de diseño que `core/proxy.py`.

Pensado como punto único de peticiones GET puntuales contra un servicio
web desde cualquier backend (lista pública de hubs DC++, indexador de
torrents/magnet...), a diferencia de las conexiones "de protocolo" de
cada red (Soulseek, DC++, Gnutella2, eD2k), que abren y mantienen su
propio socket con `core.proxy.open_connection` directamente.
"""

import asyncio
import ssl
from urllib.parse import urlparse

from core.proxy import ProxyConfig, open_connection as proxy_open_connection


async def http_get(url: str, timeout: float = 20.0, proxy: ProxyConfig | None = None,
                    headers: dict[str, str] | None = None, _redirects: int = 5) -> bytes:
    """GET a mano (HTTP o HTTPS, según el esquema de `url`), devolviendo
    solo el cuerpo de la respuesta ya sin cabeceras. Sigue redirecciones
    301/302/303/307 y desenvuelve `Transfer-Encoding: chunked` (habitual
    en APIs HTTPS modernas detrás de un proxy inverso, a diferencia del
    XML plano con `Content-Length` de dchublist.com)."""
    parsed = urlparse(url)
    is_https = parsed.scheme == "https"
    host = parsed.hostname
    if host is None:
        raise ValueError(f"URL sin host: {url!r}")
    port = parsed.port or (443 if is_https else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    ssl_context = ssl.create_default_context() if is_https else None
    reader, writer = await proxy_open_connection(host, port, proxy=proxy, ssl=ssl_context, timeout=timeout)
    try:
        header_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "User-Agent: P2P-Total/0.1",
            "Accept: */*",
            "Connection: close",
        ]
        for key, value in (headers or {}).items():
            header_lines.append(f"{key}: {value}")
        request = "\r\n".join(header_lines) + "\r\n\r\n"
        writer.write(request.encode("ascii"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout=timeout)
    finally:
        writer.close()

    if b"\r\n\r\n" not in raw:
        return b""
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    header_block_lower = head.lower()

    if _redirects > 0 and (
        b" 301 " in status_line or b" 302 " in status_line
        or b" 303 " in status_line or b" 307 " in status_line
    ):
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"location:"):
                location = line.split(b":", 1)[1].strip().decode("utf-8", errors="replace")
                if location.startswith("/"):
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                return await http_get(
                    location, timeout=timeout, proxy=proxy, headers=headers, _redirects=_redirects - 1,
                )

    if b"transfer-encoding:" in header_block_lower and b"chunked" in header_block_lower:
        body = _dechunk(body)

    return body


async def http_download(url: str, dest_path, timeout: float = 60.0, proxy: ProxyConfig | None = None,
                         headers: dict[str, str] | None = None, progress_cb=None,
                         _redirects: int = 5) -> None:
    """Como `http_get`, pero escribiendo el cuerpo directamente a
    `dest_path` en streaming en vez de acumularlo entero en memoria
    (pensado para ficheros grandes, como el paquete de una
    actualización — ver `core/self_updater.py`). `progress_cb`, si se
    da, se llama con (bytes_descargados, bytes_totales_o_None) después
    de cada trozo escrito."""
    parsed = urlparse(url)
    is_https = parsed.scheme == "https"
    host = parsed.hostname
    if host is None:
        raise ValueError(f"URL sin host: {url!r}")
    port = parsed.port or (443 if is_https else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    ssl_context = ssl.create_default_context() if is_https else None
    reader, writer = await proxy_open_connection(host, port, proxy=proxy, ssl=ssl_context, timeout=timeout)
    try:
        header_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "User-Agent: P2P-Total/0.1",
            "Accept: */*",
            "Connection: close",
        ]
        for key, value in (headers or {}).items():
            header_lines.append(f"{key}: {value}")
        request = "\r\n".join(header_lines) + "\r\n\r\n"
        writer.write(request.encode("ascii"))
        await writer.drain()

        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        status_line = head.split(b"\r\n", 1)[0]
        header_block_lower = head.lower()

        if _redirects > 0 and (
            b" 301 " in status_line or b" 302 " in status_line
            or b" 303 " in status_line or b" 307 " in status_line
        ):
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"location:"):
                    location = line.split(b":", 1)[1].strip().decode("utf-8", errors="replace")
                    if location.startswith("/"):
                        location = f"{parsed.scheme}://{parsed.netloc}{location}"
                    writer.close()
                    return await http_download(
                        location, dest_path, timeout=timeout, proxy=proxy, headers=headers,
                        progress_cb=progress_cb, _redirects=_redirects - 1,
                    )

        content_length = None
        chunked = b"transfer-encoding:" in header_block_lower and b"chunked" in header_block_lower
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    content_length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    content_length = None

        downloaded = 0
        with open(dest_path, "wb") as fh:
            if chunked:
                while True:
                    size_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=timeout)
                    try:
                        size = int(size_line.strip(), 16)
                    except ValueError:
                        break
                    if size == 0:
                        break
                    chunk = await asyncio.wait_for(reader.readexactly(size), timeout=timeout)
                    await reader.readexactly(2)  # \r\n final del chunk
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, content_length)
            else:
                while True:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, content_length)
    finally:
        writer.close()


def _dechunk(data: bytes) -> bytes:
    out = bytearray()
    while data:
        size_line, sep, rest = data.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(size_line.strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        data = rest[size + 2:]  # +2 para saltar el \r\n final del chunk
    return bytes(out)
