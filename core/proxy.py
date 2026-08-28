"""
Soporte de proxy saliente (SOCKS5 y HTTP CONNECT) para los backends que no
traen soporte de proxy nativo: Soulseek, DC++, Gnutella2 y eMule/eD2k.
BitTorrent queda fuera de este módulo porque `libtorrent` ya soporta proxy
de fábrica (`settings_pack.proxy_type`/`proxy_hostname`/`proxy_port`/
`proxy_username`/`proxy_password`, configurados directamente al crear la
`lt.session` en `backends/torrent_backend.py`).

Implementado a mano sobre sockets/asyncio crudos (handshake SOCKS5 según
RFC 1928/1929, y `CONNECT` de HTTP/1.1 para el proxy HTTP), sin ninguna
librería de terceros (p.ej. PySocks), siguiendo la restricción de diseño
del proyecto de no depender de nada que reimplemente el trabajo de red por
nosotros.

Limitación honesta: solo cubre conexiones TCP salientes. SOCKS5 también
define `UDP ASSOCIATE` para tráfico UDP, pero ningún backend lo usa aquí
—igual que el eMule/aMule real, cuyo soporte de proxy tampoco cubre el
tráfico UDP de Kad—: las conexiones UDP de Kad (eMule) y de hub
(Gnutella2) siguen yendo directas sin proxear, y así se documenta también
en la GUI (Preferencias > Proxy).
"""

import asyncio
import base64
import socket
import struct
from dataclasses import dataclass

from core.ip_filter import ip_filter


@dataclass
class ProxyConfig:
    enabled: bool = False
    kind: str = "socks5"   # "socks5" | "http"
    host: str = ""
    port: int = 1080
    username: str = ""
    password: str = ""


async def open_connection(host: str, port: int, *, proxy: "ProxyConfig | None" = None,
                           ssl=None, timeout: float | None = None):
    """Sustituto directo de `asyncio.open_connection(host, port, ssl=ssl)`:
    si `proxy` está configurado y activo, la conexión al destino se
    establece a través de él; si no, se conecta directamente igual que
    antes. Pensado para reemplazar uno a uno los `asyncio.open_connection`
    salientes ya existentes en cada backend sin cambiar su forma de uso.

    Punto 39 del backlog: si `host` es una IP bloqueada por el filtro
    configurado (`core.ip_filter.ip_filter`), la conexión se rechaza antes
    de intentar nada -mismo criterio tanto si va directa como a través de
    proxy. Los hosts que no son una IPv4 literal (dominios de hub/tracker/
    servidor) nunca coinciden con ningún rango, así que pasan de largo."""
    if ip_filter.is_blocked(host):
        raise ConnectionRefusedError(f"IP bloqueada por el filtro de IPs: {host}")
    if proxy is None or not proxy.enabled or not proxy.host:
        coro = asyncio.open_connection(host, port, ssl=ssl)
    elif proxy.kind == "socks5":
        coro = _connect_socks5(host, port, proxy, ssl=ssl)
    elif proxy.kind == "http":
        coro = _connect_http_proxy(host, port, proxy, ssl=ssl)
    else:
        raise ValueError(f"Tipo de proxy desconocido: {proxy.kind!r}")

    if timeout is not None:
        return await asyncio.wait_for(coro, timeout=timeout)
    return await coro


async def _connect_socks5(host: str, port: int, proxy: ProxyConfig, *, ssl=None):
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
    try:
        # 1. Saludo: versión 5, métodos de autenticación ofrecidos.
        methods = bytes([0x00, 0x02]) if proxy.username else bytes([0x00])
        writer.write(bytes([0x05, len(methods)]) + methods)
        await writer.drain()
        greeting_resp = await reader.readexactly(2)
        if greeting_resp[0] != 0x05:
            raise ConnectionError("Proxy SOCKS5: versión de respuesta inesperada")
        chosen_method = greeting_resp[1]
        if chosen_method == 0xFF:
            raise ConnectionError("Proxy SOCKS5: ningún método de autenticación aceptado por el proxy")
        if chosen_method == 0x02:
            # Sub-negociación usuario/contraseña (RFC 1929).
            user_b = proxy.username.encode("utf-8")
            pass_b = proxy.password.encode("utf-8")
            writer.write(bytes([0x01, len(user_b)]) + user_b + bytes([len(pass_b)]) + pass_b)
            await writer.drain()
            auth_resp = await reader.readexactly(2)
            if auth_resp[1] != 0x00:
                raise ConnectionError("Proxy SOCKS5: usuario/contraseña rechazados")

        # 2. Petición CONNECT.
        req = bytes([0x05, 0x01, 0x00]) + _encode_socks5_address(host) + struct.pack(">H", port)
        writer.write(req)
        await writer.drain()

        # 3. Respuesta: VER REP RSV ATYP + dirección + puerto (longitud
        # variable según ATYP), hay que consumir toda la dirección aunque
        # no nos interese su valor para no desalinear el resto del stream.
        head = await reader.readexactly(4)
        if head[0] != 0x05:
            raise ConnectionError("Proxy SOCKS5: versión de respuesta inesperada")
        if head[1] != 0x00:
            raise ConnectionError(f"Proxy SOCKS5: CONNECT rechazado (código de error {head[1]})")
        atyp = head[3]
        if atyp == 0x01:      # IPv4
            await reader.readexactly(4 + 2)
        elif atyp == 0x04:    # IPv6
            await reader.readexactly(16 + 2)
        elif atyp == 0x03:    # dominio
            name_len = (await reader.readexactly(1))[0]
            await reader.readexactly(name_len + 2)
        else:
            raise ConnectionError("Proxy SOCKS5: tipo de dirección desconocido en la respuesta")
    except BaseException:
        writer.close()
        raise

    if ssl is not None:
        reader, writer = await _upgrade_to_tls(reader, writer, host, ssl)
    return reader, writer


def _encode_socks5_address(host: str) -> bytes:
    """ATYP + dirección en el formato que espera SOCKS5: IPv4 (0x01),
    IPv6 (0x04) o nombre de dominio (0x03) — para un dominio se deja que
    sea el propio proxy quien resuelva el DNS, en vez de resolverlo
    nosotros y filtrar así la consulta fuera del túnel del proxy."""
    try:
        return b"\x01" + socket.inet_pton(socket.AF_INET, host)
    except OSError:
        pass
    try:
        return b"\x04" + socket.inet_pton(socket.AF_INET6, host)
    except OSError:
        pass
    domain = host.encode("ascii")
    return bytes([0x03, len(domain)]) + domain


async def _connect_http_proxy(host: str, port: int, proxy: ProxyConfig, *, ssl=None):
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
    try:
        target = f"{host}:{port}"
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if proxy.username:
            creds = base64.b64encode(f"{proxy.username}:{proxy.password}".encode("utf-8")).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {creds}")
        lines.append("")
        lines.append("")
        writer.write("\r\n".join(lines).encode("ascii"))
        await writer.drain()

        status_line = await reader.readline()
        if not status_line:
            raise ConnectionError("Proxy HTTP: la conexión se cerró durante el CONNECT")
        parts = status_line.decode("latin-1", errors="replace").split(None, 2)
        if len(parts) < 2 or not parts[1].startswith("2"):
            raise ConnectionError(f"Proxy HTTP: CONNECT rechazado ({status_line!r})")
        # Consumir el resto de las cabeceras de la respuesta hasta la línea
        # en blanco que las separa del túnel ya establecido.
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
    except BaseException:
        writer.close()
        raise

    if ssl is not None:
        reader, writer = await _upgrade_to_tls(reader, writer, host, ssl)
    return reader, writer


async def _upgrade_to_tls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           server_hostname: str, ssl_context) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Envuelve en TLS una conexión ya establecida a través de un proxy.

    `asyncio.open_connection(..., ssl=...)` no sirve aquí porque el socket
    ya está conectado al proxy, no al destino final: el TLS solo puede
    empezar después de completado el `CONNECT`/handshake SOCKS5. Se apoya
    en `loop.start_tls()` (disponible desde Python 3.11) reutilizando el
    mismo `StreamReaderProtocol`/`StreamReader` ya en uso — al volver a
    invocar `connection_made()` sobre el nuevo transporte TLS, el
    protocolo actualiza automáticamente el `reader` original, así que solo
    hace falta reconstruir el `writer` para que escriba sobre el nuevo
    transporte.

    Cuidado: el `writer` original (pre-TLS) se queda sin referencias en
    cuanto se descarta, y su `__del__` cierra el transporte TCP subyacente
    —el mismo que el transporte TLS envuelve por debajo— en cuanto el
    recolector de basura pasa por él, tumbando la conexión a mitad de
    handshake o de transferencia. Para evitarlo, se cuelga una referencia
    fuerte al `writer` viejo del nuevo, de forma que siga vivo (sin que
    nadie llame a `close()` sobre él) mientras el nuevo esté en uso."""
    loop = asyncio.get_event_loop()
    transport = writer.transport
    protocol = transport.get_protocol()
    new_transport = await loop.start_tls(
        transport, protocol, ssl_context, server_side=False, server_hostname=server_hostname,
    )
    new_writer = asyncio.StreamWriter(new_transport, protocol, reader, loop)
    new_writer._pre_tls_writer = writer
    return reader, new_writer
