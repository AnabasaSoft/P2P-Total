"""
Cliente UPnP IGD (Internet Gateway Device) minimalista, implementado a
mano sobre sockets/asyncio crudos (sin librerías externas ni procesos
externos, igual que el resto del proyecto): descubre el router por
SSDP, localiza el `controlURL` del servicio WANIPConnection/
WANPPPConnection en su XML de descripción, y llama `AddPortMapping`/
`DeletePortMapping` por SOAP sobre HTTP.

Pensado como best-effort: cualquier fallo (sin UPnP en el router, sin
respuesta, red sin NAT...) se traduce en `False`/`None`, nunca en una
excepción, para que un backend pueda llamarlo al conectar sin arriesgar
romper la conexión real por un router que no coopera.
"""

import asyncio
import re
import socket
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

_SSDP_ADDR = "239.255.255.250"
_SSDP_PORT = 1900
_DEVICE_NS = "urn:schemas-upnp-org:device-1-0"
_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


class _SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.locations: list[str] = []

    def datagram_received(self, data: bytes, addr) -> None:
        text = data.decode("utf-8", errors="ignore")
        match = re.search(r"(?im)^location:\s*(\S+)", text)
        if match:
            self.locations.append(match.group(1))


async def _discover_locations(timeout: float = 2.0) -> list[str]:
    loop = asyncio.get_event_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _SSDPProtocol, local_addr=("0.0.0.0", 0), allow_broadcast=True
    )
    try:
        search = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {_SSDP_ADDR}:{_SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 2\r\n"
            "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
            "\r\n"
        ).encode()
        transport.sendto(search, (_SSDP_ADDR, _SSDP_PORT))
        await asyncio.sleep(timeout)
        return list(dict.fromkeys(protocol.locations))
    finally:
        transport.close()


async def _http_get(url: str, timeout: float = 3.0) -> str:
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        request = f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()
    _, _, body = data.partition(b"\r\n\r\n")
    return body.decode("utf-8", errors="ignore")


def _find_control_url(xml_text: str, base_url: str) -> tuple[str, str] | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for service in root.iter(f"{{{_DEVICE_NS}}}service"):
        service_type = service.findtext(f"{{{_DEVICE_NS}}}serviceType", "")
        if service_type in _SERVICE_TYPES:
            control_url = service.findtext(f"{{{_DEVICE_NS}}}controlURL", "")
            if control_url:
                return urljoin(base_url, control_url), service_type
    return None


async def _soap_request(control_url: str, service_type: str, action: str,
                         args: dict[str, str], timeout: float = 3.0) -> str:
    parts = urlsplit(control_url)
    host, port = parts.hostname, parts.port or 80
    path = parts.path or "/"
    body_args = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service_type}">{body_args}</u:{action}>'
        "</s:Body></s:Envelope>"
    )
    body_bytes = body.encode("utf-8")
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        'Content-Type: text/xml; charset="utf-8"\r\n'
        f'SOAPAction: "{service_type}#{action}"\r\n'
        f"Content-Length: {len(body_bytes)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body_bytes
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        writer.write(request)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()
    header, _, _ = data.partition(b"\r\n\r\n")
    status_line = header.split(b"\r\n", 1)[0].decode(errors="ignore")
    return status_line


def _guess_local_ip() -> str:
    """Truco estándar: no llega a enviar ningún paquete (UDP sin
    `connect()` real a nivel de red), solo hace que el kernel elija la
    interfaz/IP local que usaría para alcanzar esa IP externa."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


async def _for_each_igd_control_url(action_coro, *, discover_timeout: float = 2.0,
                                     http_timeout: float = 3.0):
    locations = await _discover_locations(discover_timeout)
    for location in locations:
        try:
            xml_text = await _http_get(location, http_timeout)
        except (OSError, asyncio.TimeoutError):
            continue
        found = _find_control_url(xml_text, location)
        if not found:
            continue
        control_url, service_type = found
        result = await action_coro(control_url, service_type)
        if result:
            return result
    return False


async def add_port_mapping(port: int, protocol: str = "TCP",
                            description: str = "P2P Total", overall_timeout: float = 8.0) -> bool:
    """Best-effort: abre `port` (mismo puerto externo e interno) en el
    router hacia esta máquina. Devuelve False ante cualquier fallo
    (nunca lanza), incluido que no haya ningún router UPnP accesible."""
    local_ip = _guess_local_ip()

    async def _add(control_url: str, service_type: str) -> bool:
        status = await _soap_request(
            control_url, service_type, "AddPortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": str(port),
                "NewProtocol": protocol,
                "NewInternalPort": str(port),
                "NewInternalClient": local_ip,
                "NewEnabled": "1",
                "NewPortMappingDescription": description,
                "NewLeaseDuration": "0",
            },
        )
        return " 200 " in status or status.endswith("200 OK")

    try:
        return bool(await asyncio.wait_for(_for_each_igd_control_url(_add), overall_timeout))
    except (OSError, asyncio.TimeoutError, ET.ParseError):
        return False


async def delete_port_mapping(port: int, protocol: str = "TCP", overall_timeout: float = 8.0) -> bool:
    """Best-effort: retira un mapeo añadido antes con `add_port_mapping`."""

    async def _delete(control_url: str, service_type: str) -> bool:
        status = await _soap_request(
            control_url, service_type, "DeletePortMapping",
            {"NewRemoteHost": "", "NewExternalPort": str(port), "NewProtocol": protocol},
        )
        return " 200 " in status or status.endswith("200 OK")

    try:
        return bool(await asyncio.wait_for(_for_each_igd_control_url(_delete), overall_timeout))
    except (OSError, asyncio.TimeoutError, ET.ParseError):
        return False
