"""
Backend Gnutella2 (G2) implementando el protocolo directamente sobre
sockets asyncio, sin librerías externas.

G2 es un protocolo DISTINTO e INCOMPATIBLE con la Gnutella "clásica"
(G1, v0.4/v0.6), pese a compartir nombre y parte de la historia.
Confirmado con pruebas reales:
gtk-gnutella, el cliente "de Gnutella" de referencia, arranca por
defecto contra G2 en 2026 (`net=gnutella2` en sus peticiones GWebCache),
que es la variante con actividad real hoy en día — G1 está, en la
práctica, muerta.

Referencias usadas (estudiadas directamente del código fuente real de
gtk-gnutella, github.com/gtk-gnutella/gtk-gnutella, clonado el 11 de
agosto de 2026, más la especificación de la comunidad citada en sus
comentarios, g2.doxu.org):
- src/core/g2/frame.c — formato de trama binaria (el "framing" de los
  paquetes G2, estructurados como árboles, no como los descriptores
  planos de G1).
- src/core/g2/msg.c — nombres literales de los paquetes (PI, PO, Q2,
  QH2, LNI, KHL...).
- src/core/nodes.c — handshake de conexión (cabeceras Accept/
  Content-Type: application/x-gnutella2 para diferenciarse de G1).
- src/core/g2/build.c — estructura exacta de un paquete /Q2 (búsqueda).
- src/core/search.c — estructura exacta de un paquete /QH2 (resultado).
- src/core/downloads.c — descarga vía HTTP por hash
  ("/uri-res/N2R?urn:sha1:...", no por índice de archivo como en G1).
- src/core/g2/gwc.c — descubrimiento automático de hubs sin tener
  ninguno conocido de antemano (bootstrap vía GWebCache; a diferencia
  de G1, G2 no tiene ningún UHC equivalente por UDP).
- src/core/g2/node.c (g2_node_handle_khl) — descubrimiento de MÁS hubs
  una vez ya conectados a uno, a partir del paquete /KHL que manda el
  propio hub periódicamente.

Diferencias clave frente a G1:
- Topología de hubs y hojas (parecida a DC++), no una malla plana pura.
- Formato de paquete binario tipo árbol (control byte + longitud +
  nombre + hijos/payload), no la cabecera fija de 23 bytes de G1.
- El handshake usa las mismas tres cabeceras iniciales "GNUTELLA
  CONNECT/0.6" que G1, pero con Accept/Content-Type distintos para
  identificarse como G2 en vez de G1.
"""

import asyncio
import base64
import hashlib
import json
import os
import struct
import time
from typing import Callable
from urllib.parse import urlparse

from core import upnp
from core.async_utils import run_in_daemon_thread
from core.backend_base import NetworkBackend
from core.config import _config_dir
from core.ip_filter import ip_filter, peer_ip_from_writer as _peer_ip_from_writer
from core.models import Download, DownloadState, Network, SearchResult
from core.proxy import ProxyConfig
from core.proxy import open_connection as proxy_open_connection
from core.rate_limiter import RateLimiter, global_download_limiter, global_upload_limiter
from core.sharing import SharedLibrary
from core.stats_tracker import stats_tracker


# --- Caché local de hubs conocidos (entre sesiones) ---
#
# gtk-gnutella "conecta en segundos" sobre todo porque, salvo la
# primerísima vez, NUNCA arranca desde cero contra el GWebCache: guarda
# su propio host_cache.c en disco entre sesiones y prueba primero esos
# hosts, que ya sabe (por experiencia reciente) que son alcanzables.
# Sin esto, cada ejecución nuestra volvía a pagar el coste completo de
# "consultar GWebCache + probar decenas de candidatos casi todos
# muertos o ya no-hub" desde cero. Replicamos la idea con un fichero
# JSON sencillo junto al config.json del programa.

_HUB_CACHE_PATH = _config_dir() / "g2_hub_cache.json"
_HUB_CACHE_MAX = 200
_HUB_CACHE_MAX_FAILS = 5  # a la 5ª conexión fallida seguida, se borra de la caché


def _load_hub_cache_raw() -> list[dict]:
    """Formato en disco: lista de {"host","port","fails"}. También
    admite el formato viejo (lista de [host, port], sin contador de
    fallos) para no perder la caché ya escrita por versiones previas."""
    try:
        with open(_HUB_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = []
        for e in data:
            if isinstance(e, dict):
                h, p, fails = e.get("host"), e.get("port"), e.get("fails", 0)
            else:
                h, p, fails = e[0], e[1], 0
            if isinstance(h, str) and isinstance(p, int):
                out.append({"host": h, "port": p, "fails": fails})
        return out
    except (OSError, ValueError, TypeError, IndexError, KeyError):
        return []


def _save_hub_cache_raw(entries: list[dict]) -> None:
    try:
        _HUB_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_HUB_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(entries[:_HUB_CACHE_MAX], f)
    except OSError:
        pass


def load_hub_cache() -> list[tuple[str, int]]:
    """Hubs con los que nos conectamos o que la propia red nos indicó
    como activos (vía /KHL) en sesiones anteriores, más recientes
    primero. Lista vacía si no hay caché todavía o está corrupta."""
    return [(e["host"], e["port"]) for e in _load_hub_cache_raw()]


def save_hub_cache(hubs: list[tuple[str, int]]) -> None:
    """Guarda hasta _HUB_CACHE_MAX hubs (sin duplicados, se conserva el
    orden recibido) para la próxima sesión. Los que ya estaban en la
    caché conservan su contador de fallos; los nuevos empiezan en 0.
    Falla en silencio si no se puede escribir (p.ej. disco lleno): no
    es crítico, solo una optimización de arranque."""
    old_fails = {(e["host"], e["port"]): e["fails"] for e in _load_hub_cache_raw()}
    seen: set[tuple[str, int]] = set()
    deduped: list[dict] = []
    for h, p in hubs:
        if (h, p) not in seen:
            seen.add((h, p))
            deduped.append({"host": h, "port": p, "fails": old_fails.get((h, p), 0)})
    _save_hub_cache_raw(deduped)


def _record_hub_failures_batch(pairs: list[tuple[str, int]]) -> None:
    """Cuenta un intento de conexión fallido contra cada hub de `pairs`
    que estuviera en la caché local, con una sola lectura y una sola
    escritura del caché en disco para todo el lote -pensada para
    _connect_race(), que prueba hasta 60 candidatos en paralelo: llamar
    a esto por cada fallo individual (en vez de una vez al final)
    generaría una ráfaga de E/S de disco síncrona dentro del propio
    bucle de eventos justo cuando muchos candidatos fallan casi a la
    vez, notándose como microbloqueos reales de la GUI. Al llegar a
    _HUB_CACHE_MAX_FAILS fallos acumulados se borra de la caché, para no
    seguir perdiendo tiempo con un hub muerto en cada connect_auto(). No
    hace nada con los hubs de `pairs` que no estuvieran ya en la caché
    (p.ej. candidatos que venían de GWebCache/X-Try-Hubs). Un único
    éxito (_remember_hub) resetea el contador de un hub a 0."""
    if not pairs:
        return
    fails_by_key: dict[tuple[str, int], int] = {}
    for host, port in pairs:
        fails_by_key[(host, port)] = fails_by_key.get((host, port), 0) + 1
    raw = _load_hub_cache_raw()
    new_raw = []
    changed = False
    for e in raw:
        key = (e["host"], e["port"])
        if key in fails_by_key:
            changed = True
            fails = e["fails"] + fails_by_key[key]
            if fails >= _HUB_CACHE_MAX_FAILS:
                continue
            e = {"host": e["host"], "port": e["port"], "fails": fails}
        new_raw.append(e)
    if changed:
        _save_hub_cache_raw(new_raw)


# --- Códec de trama binaria (sección "FRAMING" de frame.c) ---
#
# Cada paquete G2 se codifica como:
#   [byte de control] [longitud, 0-3 bytes] [nombre, 1-8 bytes] [hijos y/o payload]
#
# Byte de control (bits, de más a menos significativo):
#   7-6: Len_Len   -> nº de bytes del campo de longitud (0-3)
#   5-3: Name_Len-1 -> nº de bytes del nombre MENOS UNO (nombre: 1-8 bytes)
#   2:   CF        -> "compound flag", el paquete tiene hijos
#   1:   BE        -> big-endian (SIEMPRE debe ir a 0; solo se acepta
#                     little-endian real en la práctica, según el propio
#                     comentario del código fuente de gtk-gnutella)
#   0:   reservado
#
# Un byte de control == 0x00 marca el final de un flujo de paquetes
# (p.ej. el final de la lista de hijos de un paquete compuesto).

_CTRL_BE = 1 << 1
_CTRL_CF = 1 << 2


class G2Packet:
    """Un paquete G2: nombre, hijos (otros G2Packet) y/o payload (bytes)."""

    __slots__ = ("name", "children", "payload")

    def __init__(self, name: str, payload: bytes = b"", children: list["G2Packet"] | None = None) -> None:
        if not (1 <= len(name) <= 8):
            raise ValueError(f"nombre de paquete G2 debe medir 1-8 bytes, '{name}' mide {len(name)}")
        self.name = name
        self.payload = payload
        self.children = children or []

    def __repr__(self) -> str:
        return f"G2Packet({self.name!r}, payload={self.payload!r}, children={self.children!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, G2Packet):
            return NotImplemented
        return self.name == other.name and self.payload == other.payload and self.children == other.children

    def find(self, name: str) -> "G2Packet | None":
        """Primer hijo directo con ese nombre, o None."""
        for c in self.children:
            if c.name == name:
                return c
        return None

    def find_all(self, name: str) -> list["G2Packet"]:
        return [c for c in self.children if c.name == name]


def _encode_length(length: int) -> bytes:
    """Longitud en little-endian, con el menor número de bytes posible
    (0 si length==0, si no 1-3 bytes según haga falta)."""
    if length == 0:
        return b""
    if length <= 0xFF:
        return struct.pack("<B", length)
    if length <= 0xFFFF:
        return struct.pack("<H", length)
    if length <= 0xFFFFFF:
        return struct.pack("<I", length)[:3]
    raise ValueError(f"paquete G2 demasiado grande para su campo de longitud: {length} bytes")


def encode_packet(pkt: G2Packet) -> bytes:
    """Serializa un G2Packet (con sus hijos, recursivamente) a bytes."""
    body = b""
    for child in pkt.children:
        body += encode_packet(child)
    if pkt.children and pkt.payload:
        # Terminador 0x00 obligatorio cuando hay hijos Y payload a la
        # vez (p.ej. nuestro propio /Q2, que lleva el MUID como payload
        # del paquete raíz además de los hijos /DN e /I): sin esto, el
        # decodificador no tiene forma de saber dónde acaban los hijos
        # y empieza el payload (sección "Child Packets" de frame.c).
        # Si no hay payload tras los hijos, el terminador es opcional
        # y lo omitimos (el propio spec dice que así es "innecesario").
        body += b"\x00"
    body += pkt.payload

    length_bytes = _encode_length(len(body))
    name_bytes = pkt.name.encode("ascii")

    control = (len(length_bytes) << 6) | ((len(name_bytes) - 1) << 3)
    if pkt.children:
        control |= _CTRL_CF
    # BE (bit 1) se deja a 0 siempre: solo generamos little-endian.

    if control == 0:
        # El byte de control 0x00 está reservado como marcador de "fin
        # de flujo" y nunca puede ser el de un paquete real. Esto solo
        # pasaría con un paquete de nombre 1 byte, longitud 0 y sin
        # hijos — forzamos el flag CF para evitarlo, tal como hace el
        # propio gtk-gnutella (ver comentario "Notes on the Control Byte").
        control |= _CTRL_CF

    return bytes([control]) + length_bytes + name_bytes + body


def decode_packet(data: bytes, offset: int = 0) -> tuple[G2Packet | None, int]:
    """Deserializa UN paquete G2 a partir de `data[offset:]`.

    Devuelve (paquete, nueva_posición). Si el byte de control es 0x00
    (marcador de fin de flujo), devuelve (None, offset+1)."""
    control = data[offset]
    pos = offset + 1

    if control == 0x00:
        return None, pos

    len_len = (control & 0xC0) >> 6
    name_len = ((control & 0x38) >> 3) + 1
    has_children = bool(control & _CTRL_CF)
    is_big_endian = bool(control & _CTRL_BE)

    length = 0
    if len_len > 0:
        length_bytes = data[pos:pos + len_len]
        pos += len_len
        # Longitud en el orden de bytes del paquete raíz; nosotros solo
        # generamos/aceptamos little-endian (ver nota BE en frame.c).
        if is_big_endian:
            length = int.from_bytes(length_bytes, "big")
        else:
            length = int.from_bytes(length_bytes, "little")

    name = data[pos:pos + name_len].decode("ascii", errors="replace")
    pos += name_len

    body_end = pos + length
    body = data[pos:body_end]

    children: list[G2Packet] = []
    body_pos = 0
    if has_children:
        while body_pos < len(body):
            if body[body_pos] == 0x00:
                body_pos += 1  # marcador de fin de la lista de hijos
                break
            child, body_pos = decode_packet(body, body_pos)
            if child is not None:
                children.append(child)

    payload = body[body_pos:]

    pkt = G2Packet(name, payload=payload, children=children)
    return pkt, body_end


def whole_packet_length(data: bytes) -> int | None:
    """Cuántos bytes ocupa el próximo paquete completo en `data`
    (cabecera + hijos + payload), o None si `data` no llega a
    contener ni siquiera la cabecera todavía (hace falta leer más)."""
    if not data:
        return None
    control = data[0]
    if control == 0x00:
        return 1
    len_len = (control & 0xC0) >> 6
    name_len = ((control & 0x38) >> 3) + 1
    header_size = 1 + len_len + name_len
    if len(data) < header_size:
        return None
    length = 0
    if len_len > 0:
        length_bytes = data[1:1 + len_len]
        is_big_endian = bool(control & _CTRL_BE)
        length = int.from_bytes(length_bytes, "big" if is_big_endian else "little")
    return header_size + length


# --- Conexión: envoltorio de lectura/escritura de paquetes G2 ---

class _G2Connection:
    """Envoltorio fino sobre StreamReader/Writer para leer/escribir
    paquetes G2 usando el códec de trama de arriba. A diferencia de G1
    (cabecera fija de 23 bytes), aquí la cabecera tiene longitud
    variable, así que hay que ir acumulando bytes hasta saber cuántos
    hacen falta (whole_packet_length se encarga de decirlo)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 remote_ip: str | None = None) -> None:
        self.reader = reader
        self.writer = writer
        self._buffer = b""
        # IP con la que el hub nos ve a NOSOTROS (cabecera Remote-IP de
        # su respuesta al handshake — confirmado en nodes.c, sección
        # "Remote-IP -- IP address of this node as seen from remote
        # node"). Es justo la dirección que hace falta para ofrecerse
        # como destino de un /PUSH (ver _download_via_push): sin esto
        # no hay forma fiable de saber qué IP externa anunciar.
        self.remote_ip = remote_ip

    async def send_packet(self, pkt: G2Packet) -> None:
        self.writer.write(encode_packet(pkt))
        await self.writer.drain()

    async def read_packet(self, timeout: float | None = None) -> G2Packet | None:
        """Lee UN paquete completo, acumulando bytes del socket hasta
        tener lo necesario. Devuelve None si lo leído es el marcador de
        fin de flujo (byte de control 0x00, poco habitual a nivel raíz
        pero posible)."""
        deadline = None if timeout is None else asyncio.get_event_loop().time() + timeout

        while True:
            needed = whole_packet_length(self._buffer)
            if needed is not None and len(self._buffer) >= needed:
                break
            remaining = None if deadline is None else max(0.0, deadline - asyncio.get_event_loop().time())
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError("timeout esperando un paquete G2 completo")
            chunk = await asyncio.wait_for(self.reader.read(4096), timeout=remaining)
            if not chunk:
                raise asyncio.IncompleteReadError(self._buffer, None)
            self._buffer += chunk

        needed = whole_packet_length(self._buffer)
        packet_bytes, self._buffer = self._buffer[:needed], self._buffer[needed:]
        pkt, _ = decode_packet(packet_bytes)
        return pkt

    def close(self) -> None:
        self.writer.close()


# --- Descubrimiento de hubs (bootstrap vía GWebCache) ---
#
# G2 no tiene ningún mecanismo UDP equivalente al UHC de G1 (confirmado:
# no existe ningún src/core/g2/uhc.c en gtk-gnutella) — el único sistema
# de arranque sin conocer ya un hub es GWebCache por HTTP. La lista de
# abajo es literalmente `boot_url[]` de src/core/g2/gwc.c, cuyo propio
# comentario dice "used only for bootstrapping purposes on the G2
# network". Solo hay dos, y nada garantiza que sigan vivas en 2026 (la
# propia gwc.c ya contempla ese caso: si un cache no da resultado
# probamos el siguiente, sin más).

_GWEBCACHES = [
    "http://cache.trillinux.org/g2/bazooka.php",
    "http://cache.ce3c.be/",
]


async def _http_get(url: str, timeout: float = 10.0, proxy: ProxyConfig | None = None) -> bytes:
    """Cliente HTTP mínimo sobre sockets crudos (mismo patrón que el
    resto del proyecto, sin librerías externas). Devuelve solo el
    cuerpo de la respuesta, sin las cabeceras."""
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        raise ValueError("Este cliente HTTP mínimo no soporta HTTPS (los GWebCache G2 conocidos usan HTTP)")

    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    reader, writer = await proxy_open_connection(host, port, proxy=proxy, timeout=timeout)
    try:
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: P2P-Total/0.1\r\n"
            f"Connection: close\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout=timeout)
    finally:
        writer.close()

    if b"\r\n\r\n" not in raw:
        return b""
    _headers, _, body = raw.partition(b"\r\n\r\n")
    return body


def parse_gwc2_hosts(body: bytes) -> list[tuple[str, int]]:
    """Parsea la respuesta de un GWebCache G2 a ?get=1&net=gnutella2:
    líneas 'h|host:puerto|timestamp_expiración' (un hub — el campo de
    letra es case-insensitive, un cache real de verdad manda 'h'
    minúscula, no 'H'), 'u|url|timestamp' (otro GWebCache conocido, lo
    ignoramos: nos basta con la lista fija de arriba) e 'i|...'
    (informativa) — formato base confirmado en gwc_host_line de
    src/core/g2/gwc.c, con el tercer campo (timestamp) verificado
    probando contra cache.trillinux.org, un cache real y vivo en 2026,
    que NO aparecía en el código fuente (gwc_host_line ignora con
    seguridad cualquier campo extra tras el segundo '|', así que
    hacemos lo mismo). DISTINTO del '?hostfile=1' de texto plano que
    usa el GWebCache de G1 — no son intercambiables."""
    hosts: list[tuple[str, int]] = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if len(line) <= 2 or line[1] != "|" or line[0].upper() != "H":
            continue
        host_port = line[2:].split("|", 1)[0]
        if ":" not in host_port:
            continue
        host, _, port_str = host_port.rpartition(":")
        try:
            hosts.append((host, int(port_str)))
        except ValueError:
            continue
    return hosts



# Los GWebCache reales filtran por una lista blanca de identificadores
# de cliente conocidos, para frenar abuso/spam de caches falsos —
# comprobado contra cache.trillinux.org (vivo en 2026): con un
# identificador propio inventado responde "ERROR Rejecting - Invalid
# Client." y cero hosts. El propio formato "client=" + vendor + versión
# es el que manda gtk-gnutella real (macro CLIENT_INFO de gwc.c,
# vendor="GTKG" de gtk-gnutella.h) — lo reutilizamos tal cual para que
# el cache nos acepte, ya que no tenemos (ni tiene sentido registrar)
# un código de vendor propio solo para esto.
_GWC_CLIENT_ID = "GTKG1.3.1"


async def discover_hubs(timeout_per_cache: float = 8.0, debug: bool = False,
                         proxy: ProxyConfig | None = None) -> list[tuple[str, int]]:
    """Descubre hubs G2 candidatos consultando los dos GWebCache
    conocidos (único mecanismo de bootstrap real de G2, sin UHC)."""
    candidates: list[tuple[str, int]] = []
    for cache_url in _GWEBCACHES:
        try:
            url = cache_url + ("&" if "?" in cache_url else "?") + f"get=1&net=gnutella2&client={_GWC_CLIENT_ID}"
            body = await _http_get(url, timeout=timeout_per_cache, proxy=proxy)
            hosts = parse_gwc2_hosts(body)
            if debug:
                print(f"  [debug] GWebCache G2 {cache_url}: {len(hosts)} hub(s)")
            candidates.extend(hosts)
        except (asyncio.TimeoutError, OSError, ValueError) as e:
            if debug:
                print(f"  [debug] GWebCache G2 {cache_url}: no disponible ({e})")
            continue
    return candidates


# --- Handshake de conexión (0.6, con cabeceras Accept/Content-Type
#     application/x-gnutella2 — sección relevante de nodes.c) ---

_G2_ACCEPT_HEADER = "application/x-gnutella2"


async def perform_handshake(host: str, port: int, timeout: float = 15.0,
                             debug: bool = False, proxy: ProxyConfig | None = None) -> _G2Connection:
    """Handshake 0.6 completo contra un hub G2: petición → 200 OK con
    cabeceras → confirmación final. A partir de aquí la conexión lleva
    paquetes binarios G2 (códec de arriba), no más texto. Lanza
    ConnectionError si el hub rechaza o si cierra sin completar."""
    reader, writer = await proxy_open_connection(host, port, proxy=proxy, timeout=timeout)

    request = (
        "GNUTELLA CONNECT/0.6\r\n"
        "User-Agent: P2P-Total/0.1\r\n"
        f"Accept: {_G2_ACCEPT_HEADER}\r\n"
        "X-Hub: False\r\n"
        "X-Hub-Needed: True\r\n"
        "\r\n"
    )
    writer.write(request.encode("ascii"))
    await writer.drain()

    try:
        header_block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
    except asyncio.IncompleteReadError:
        writer.close()
        raise ConnectionError(f"{host}:{port} cerró la conexión sin completar el handshake")

    if debug:
        print(f"  [debug] hub -> {header_block!r}")

    lines = header_block.decode("ascii", errors="replace").split("\r\n")
    status_line = lines[0] if lines else ""

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()

    # Aunque el hub rechace la conexión (típicamente "503 Maximum
    # connections reached", el caso más común en la práctica: un hub
    # concreto casi siempre tiene el cupo lleno), muchos igualmente
    # regalan una lista de hubs alternativos en X-Try-Hubs. Formato
    # real observado: "host:puerto 2026-08-11T09:06Z,host:puerto ...",
    # es decir host:puerto + timestamp separados por espacio, entradas
    # separadas por coma — nos quedamos solo con host:puerto.
    candidates: list[tuple[str, int]] = []
    if "x-try-hubs" in headers:
        for entry in headers["x-try-hubs"].split(","):
            host_port = entry.strip().split(" ", 1)[0].strip()
            if ":" in host_port:
                h, _, p = host_port.rpartition(":")
                try:
                    candidates.append((h, int(p)))
                except ValueError:
                    continue

    if " 200 " not in status_line:
        writer.close()
        err = ConnectionError(f"El hub rechazó la conexión: {status_line!r}")
        # Adjuntamos los candidatos al propio error para que quien
        # llame (connect_to_hub_with_fallback) pueda reintentar con
        # ellos sin tener que volver a parsear cabeceras.
        err.candidates = candidates
        raise err

    content_type = headers.get("content-type", "")
    if _G2_ACCEPT_HEADER not in content_type:
        # No es necesariamente fatal (algunos hubs pueden omitirlo),
        # pero avisamos: si esto pasa, lo más probable es que hayamos
        # conectado sin querer a un nodo G1, no G2.
        if debug:
            print(f"  [debug] aviso: el hub no confirmó Content-Type G2 (llegó: {content_type!r})")

    # Confirmación final del handshake a tres bandas.
    final_ack = (
        "GNUTELLA/0.6 200 OK\r\n"
        "X-Hub: False\r\n"
        f"Content-Type: {_G2_ACCEPT_HEADER}\r\n"
        "\r\n"
    )
    writer.write(final_ack.encode("ascii"))
    await writer.drain()

    return _G2Connection(reader, writer, remote_ip=headers.get("remote-ip"))


# --- Búsqueda: construcción de /Q2 y parseo de /QH2 ---
#
# Referencias: src/core/g2/build.c (g2_build_q2) y src/core/search.c
# (get_g2_results_record, enum g2_qh2_children / g2_qh2_h_children).

_SHA1_RAW_SIZE = 20  # 160 bits, tamaño fijo de cualquier hash SHA1
_TTH_RAW_SIZE = 24   # 192 bits, tamaño fijo de un Tiger Tree Hash

# "Interest" fijo que manda todo cliente real en cada /Q2 (sección
# g2_build_q2 de build.c) — incluye el NUL final a propósito, por un
# bug de parseo conocido en versiones viejas de Shareaza que si no se
# incluye se queda colgado.
_Q2_INTEREST = b"URL\x00PFS\x00DN\x00A"


def new_muid() -> bytes:
    """16 bytes aleatorios para identificar una búsqueda (MUID)."""
    return os.urandom(16)


def build_query(query: str, muid: bytes | None = None) -> G2Packet:
    """Construye un paquete /Q2 (búsqueda) mínimo pero válido: el MUID
    va como payload del propio paquete raíz (no como hijo), con /DN
    (el texto buscado) e /I (intereses fijos) como hijos. No incluimos
    /UDP (eso es para búsquedas GUESS fuera de banda por UDP directo a
    hojas, no hace falta para una búsqueda normal enrutada por el hub
    al que estamos conectados por TCP)."""
    muid = muid or new_muid()
    if len(muid) != 16:
        raise ValueError("el MUID de una búsqueda G2 debe medir 16 bytes")

    dn = G2Packet("DN", payload=query.encode("utf-8", errors="replace"))
    interest = G2Packet("I", payload=_Q2_INTEREST)
    return G2Packet("Q2", payload=muid, children=[dn, interest])


def _decode_vlint(data: bytes) -> int:
    """Entero de longitud variable: little-endian con ceros finales
    omitidos (sección vlint_decode de misc.h) — mismo esquema que ya
    usamos para el campo de longitud de la propia trama G2."""
    return int.from_bytes(data, "little")


def _encode_vlint(value: int) -> bytes:
    """Inverso de _decode_vlint: little-endian, sin los ceros finales
    (hace falta para construir el /SZ de un /H propio al contestar una
    búsqueda ajena con nuestro propio contenido, ver
    _handle_incoming_query)."""
    if value == 0:
        return b""
    return value.to_bytes(8, "little").rstrip(b"\x00")


def _parse_address(payload: bytes) -> tuple[str, int] | None:
    """6 bytes: IPv4 en big-endian (4) + puerto en little-endian (2) —
    sección g2_node_parse_address de g2/node.c. G2 no soporta IPv6: es
    un límite real del protocolo (todos los hubs/nodos reales, Shareaza
    incluido, usan este mismo campo fijo de 6 bytes para /NA, /CH y
    /PUSH), no una carencia de esta implementación — no se puede
    "arreglar" sin dejar de hablar G2 de verdad con la red real. Aparte
    queda la conexión TCP en sí al hub (`connect_to_hub`), que si el
    hub tiene una dirección IPv6 ya funciona sin cambios (asyncio
    resuelve/conecta con normalidad); lo que no puede pasar por este
    campo de 6 bytes es la dirección en sí de un nodo/push dentro del
    propio protocolo."""
    if len(payload) != 6:
        return None
    ip = ".".join(str(b) for b in payload[:4])
    port = struct.unpack("<H", payload[4:6])[0]
    return ip, port


def _encode_g2_address(ip: str, port: int) -> bytes:
    """Inverso de _parse_address: 4 bytes de IPv4 en big-endian + 2
    bytes de puerto en little-endian, el mismo formato de 6 bytes que
    usa G2 en /NA, /CH, etc. Hace falta para anunciar nuestra propia
    dirección en el payload de un /PUSH (ver g2_build_push en
    build.c: 'host_ip_port_poke(payload, addr, port, &plen)')."""
    return bytes(int(o) for o in ip.split(".")) + struct.pack("<H", port)


def _parse_urn(payload: bytes) -> tuple[str, bytes] | None:
    """El payload de /URN es un híbrido texto+binario: cadena ASCII
    NUL-terminada con el tipo de hash ("sha1", "bitprint", "tree:tiger/",
    "bp", "ttr"), seguida directamente del hash en crudo (sin
    codificar) — sección G2_QH2_H_URN de search.c."""
    if b"\x00" not in payload:
        return None
    type_name, _, hash_bytes = payload.partition(b"\x00")
    type_name = type_name.decode("ascii", errors="replace")

    if type_name in ("sha1",) and len(hash_bytes) == _SHA1_RAW_SIZE:
        return "sha1", hash_bytes
    if type_name in ("tree:tiger/", "ttr") and len(hash_bytes) == _TTH_RAW_SIZE:
        return "tth", hash_bytes
    if type_name in ("bitprint", "bp") and len(hash_bytes) == _SHA1_RAW_SIZE + _TTH_RAW_SIZE:
        # bitprint = SHA1 + TTH concatenados; nos quedamos con el SHA1
        # (es el que hace falta para construir la URL de descarga N2R).
        return "sha1", hash_bytes[:_SHA1_RAW_SIZE]
    return None


def _sanitize_filename(name: str) -> str:
    """El nombre de archivo de un resultado viene tal cual del /DN que
    mandó un peer remoto no confiable: puede traer bytes nulos (que
    revientan open() con ValueError: embedded null byte, visto en
    pruebas reales) o separadores de ruta / '..' con los que un origen
    malicioso podría escribir fuera de dest_path (path traversal). Nos
    quedamos solo con el nombre base, sin nulos ni separadores."""
    name = name.replace("\x00", "").replace("/", "_").replace("\\", "_")
    name = os.path.basename(name).strip()
    return name or "descarga_sin_nombre"


def sha1_to_urn_base32(sha1_raw: bytes) -> str:
    """El hash crudo de 20 bytes se codifica en Base32 (RFC 4648, sin
    relleno '=') para construir la URN de descarga
    ('urn:sha1:<base32>'), el formato de texto estándar de Gnutella
    para identificar contenido por SHA1."""
    return base64.b32encode(sha1_raw).decode("ascii").rstrip("=")


def _sha1_of_file(path: str) -> bytes:
    """Recorre el fichero ya descargado en bloques (no se carga entero
    en memoria) para verificar su SHA1 contra el que traía el
    resultado de búsqueda original — a diferencia de BitTorrent, G2 no
    tiene ningún mecanismo de verificación por-pieza durante la propia
    transferencia HTTP, así que esto es la única comprobación posible
    y se hace entera al final."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.digest()


def _find_all_recursive(pkt: G2Packet, name: str) -> list[G2Packet]:
    """Busca TODOS los descendientes (no solo hijos directos) con ese
    nombre. Hace falta para /KHL (ver _read_loop): a diferencia de
    /QH2, donde /H sí es siempre hijo directo, gtk-gnutella recorre
    /KHL con un barrido completo del subárbol, no solo su primer
    nivel."""
    found = []
    for child in pkt.children:
        if child.name == name:
            found.append(child)
        found.extend(_find_all_recursive(child, name))
    return found


def parse_query_hit(pkt: G2Packet) -> dict | None:
    """Parsea un paquete /QH2 (resultado de búsqueda) según la
    sección get_g2_results_record + el switch de children de search.c.
    Devuelve None si el paquete no es reconocible con seguridad."""
    if pkt.name != "QH2":
        return None

    # OJO: a diferencia de /Q2 (payload = MUID puro, 16 bytes), el
    # payload raíz de /QH2 lleva un byte de "hop count" delante del
    # MUID (17 bytes en total) — confirmado en g2_msg_get_muid de
    # src/core/g2/msg.c ("First payload byte is the hop count").
    # Probado contra un hub G2 real: todos los /QH2 que devuelve
    # tienen payload de exactamente 17 bytes, coincidiendo con esto.
    if len(pkt.payload) < 17:
        return None
    hop_count = pkt.payload[0]
    muid = pkt.payload[1:17]

    na = pkt.find("NA")
    address = _parse_address(na.payload) if na else None

    gu = pkt.find("GU")
    servent_guid = gu.payload if gu else None

    hits = []
    for h in pkt.find_all("H"):
        filename = None
        size = None
        sha1_hash = None

        dn = h.find("DN")
        if dn:
            filename = dn.payload.decode("utf-8", errors="replace")

        sz = h.find("SZ")
        if sz:
            size = _decode_vlint(sz.payload)

        urn = h.find("URN")
        if urn:
            parsed_urn = _parse_urn(urn.payload)
            if parsed_urn and parsed_urn[0] == "sha1":
                sha1_hash = parsed_urn[1]

        if filename is not None:
            hits.append({"filename": filename, "size": size or 0, "sha1": sha1_hash})

    return {"muid": muid, "hop_count": hop_count, "address": address, "servent_guid": servent_guid, "hits": hits}


# --- Backend: une handshake + búsqueda + descarga bajo la interfaz común ---

DEFAULT_HUB_PORT = 6346
DEFAULT_LISTEN_PORT = 6346  # puerto fijo para recibir conexiones /PUSH entrantes (ver _try_push_download)


class _HttpRejected(Exception):
    """El origen respondió con una línea de estado HTTP real (aunque no
    fuera 200/206): está claramente alcanzable desde aquí, así que
    reintentar por /PUSH no tiene ningún sentido — eso solo sirve para
    saltarse un fallo a nivel de RED (timeout, conexión rechazada o
    reseteada antes de llegar a hablar HTTP), no un rechazo a nivel de
    aplicación como un 404 o un 503."""


class G2Backend(NetworkBackend):
    network = Network.GNUTELLA2

    def __init__(self, listen_port: int = DEFAULT_LISTEN_PORT,
                 shared_library: SharedLibrary | None = None,
                 proxy: ProxyConfig | None = None) -> None:
        self._proxy = proxy
        self._conn: _G2Connection | None = None
        self._read_task: asyncio.Task | None = None
        self._progress_callback: Callable[[Download], None] | None = None
        self._active: dict[str, dict] = {}
        self._pending_hits: list[dict] = []
        self._collecting_muid: bytes | None = None
        self._collecting_max_results: int | None = None
        self._limit_reached: asyncio.Event | None = None
        self._discovered_hubs: set[tuple[str, int]] = set()
        self._listen_port = listen_port
        self._debug = False
        # Servent GUID propio (16 bytes aleatorios, generado una vez
        # por sesión): se anuncia en /GU dentro de cada /QH2 que
        # contestamos, para que quien recibe nuestro resultado pueda
        # dirigirnos un /PUSH si no puede conectar directamente (ver
        # _handle_incoming_push más abajo).
        self._guid = os.urandom(16)
        self._shared_library = shared_library
        self._share_server: asyncio.base_events.Server | None = None
        self._active_uploads = 0

    # ---- ciclo de vida ----

    async def connect(self) -> None:
        # La conexión al hub se hace con connect_to_hub() (necesitamos
        # saber a qué hub conectar; G2 tampoco tiene un único punto de
        # entrada, igual que G1/DC++). Aquí solo arrancamos, si toca,
        # el servidor que sirve nuestra carpeta compartida a otros
        # peers (independiente de a qué hub nos conectemos después).
        if self._shared_library is not None:
            # need_ed2k=False: a diferencia de Soulseek/DC++, G2 sí
            # necesita el SHA1 de cada fichero para anunciarlo y
            # responder búsquedas "urn:sha1:" (need_sha1=True, el valor
            # por defecto), pero no el eD2k -con diferencia el hash más
            # caro de calcular, ver SharedLibrary.rescan-, que no usa
            # para nada. En segundo plano sin esperar (ver
            # SharedLibrary.ensure_scanning), así connect() no se queda
            # esperando a que termine de hashear toda la biblioteca.
            self._shared_library.ensure_scanning(need_ed2k=False)
            # Antes se comprobaba `self._shared_library.enabled` (es
            # decir, que el escaneo ya hubiera indexado algo) para
            # decidir si merecía la pena escuchar; ahora el escaneo es
            # en segundo plano y puede tardar, así que se decide con
            # `roots` (hay carpetas compartidas configuradas, aunque
            # todavía no se hayan terminado de indexar) para no dejar de
            # escuchar peticiones mientras el índice se va rellenando.
            if self._shared_library.roots:
                try:
                    self._share_server = await asyncio.start_server(
                        self._handle_incoming_connection, "0.0.0.0", self._listen_port
                    )
                except OSError:
                    self._share_server = None

    async def connect_to_hub(self, host: str, port: int = DEFAULT_HUB_PORT,
                              timeout: float = 15.0, debug: bool = False) -> None:
        self._conn = await perform_handshake(host, port, timeout=timeout, debug=debug, proxy=self._proxy)
        self._debug = debug
        self._read_task = asyncio.create_task(self._read_loop())
        self._remember_hub(host, port)
        # Best-effort: abre en el router el puerto fijo que se usa para
        # las conexiones /PUSH entrantes (ver _handle_push más abajo), si
        # tiene UPnP. Nunca bloquea ni falla la conexión al hub.
        asyncio.ensure_future(upnp.add_port_mapping(self._listen_port, "TCP", "P2P Total - Gnutella2"))

    async def connect_to_hub_with_fallback(self, host: str, port: int = DEFAULT_HUB_PORT,
                                            timeout: float = 15.0,
                                            debug: bool = False) -> tuple[str, int]:
        """Igual que connect_to_hub(), pero si el hub indicado rechaza la
        conexión (típicamente "503 Maximum connections reached" — un hub
        concreto lleno es el caso normal, no la excepción) y de regalo nos
        da candidatos vía X-Try-Hubs, los probamos uno a uno en vez de
        fallar directamente. Devuelve (host, port) del hub al que
        finalmente se ha conseguido conectar. Lanza ConnectionError solo
        si el hub original Y todos sus candidatos rechazan la conexión."""
        try:
            await self.connect_to_hub(host, port, timeout=timeout, debug=debug)
            return host, port
        except ConnectionError as e:
            candidates: list[tuple[str, int]] = getattr(e, "candidates", [])
            if not candidates:
                raise

            if debug:
                print(f"  [debug] {host}:{port} rechazó la conexión; probando "
                      f"{len(candidates)} hub(s) alternativo(s) de X-Try-Hubs...")

            tried = {(host, port)}
            last_error: Exception = e
            for cand_host, cand_port in candidates:
                if (cand_host, cand_port) in tried:
                    continue
                tried.add((cand_host, cand_port))
                if debug:
                    print(f"  [debug] probando hub alternativo {cand_host}:{cand_port}...")
                try:
                    await self.connect_to_hub(cand_host, cand_port, timeout=timeout, debug=debug)
                    return cand_host, cand_port
                except (OSError, asyncio.TimeoutError, ConnectionError, EOFError) as e2:
                    last_error = e2
                    if debug:
                        print(f"  [debug] {cand_host}:{cand_port} tampoco aceptó ({e2})")
                    continue

            raise ConnectionError(
                f"El hub {host}:{port} rechazó la conexión y ninguno de los "
                f"{len(tried) - 1} hub(s) alternativo(s) recibidos vía X-Try-Hubs "
                f"aceptó tampoco (último error: {last_error})"
            ) from last_error

    async def connect_auto(self, timeout_per_hub: float = 4.0, debug: bool = False,
                            max_concurrent: int = 60) -> tuple[str, int]:
        """Descubre hubs vía GWebCache y prueba a conectar con MUCHOS
        candidatos A LA VEZ (no uno a uno). Devuelve (host, port) del
        primero que acepte el handshake; el resto de intentos en curso
        se cancelan. Lanza ConnectionError si ningún GWebCache responde
        o si se agotan todos los candidatos sin que ninguno acepte.

        Probar uno a uno (con un timeout de varios segundos cada uno)
        es la razón real de que la conexión automática pareciera
        colgada: en la práctica, una fracción grande de las direcciones
        que reparte un GWebCache (o el propio X-Try-Hubs de un hub
        lleno) ya no son hubs activos — llevan tiempo caídas, o ahora
        mismo operan como hoja, no como hub, y rechazan con "403 Need a
        G2 Hub" (confirmado a mano contra hubs reales: gtk-gnutella
        devuelve justo ese mensaje si respondemos a su propio intento
        de handshake sin identificarnos como hub). gtk-gnutella no
        "acierta a la primera": mantiene un cupo de intentos SALIENTES
        simultáneos contra su caché de hosts (node_missing() +
        node_spawn() de src/core/nodes.c) y se queda con el primero que
        completa, así que en la práctica parece instantáneo aunque la
        mayoría de intentos individuales fallen o tarden. Replicamos
        exactamente eso aquí con un pool de tareas asyncio.

        Además, antes de tocar siquiera el GWebCache, se prueban primero
        (también en paralelo) los hubs de la caché LOCAL de sesiones
        anteriores (ver load_hub_cache): son hosts que ya sabemos que
        aceptaron nuestro handshake alguna vez recientemente, así que
        tienen muchas más papeletas de seguir siendo hubs vivos que una
        dirección genérica sacada a ciegas del GWebCache. Solo si
        ninguno de ellos responde se cae al descubrimiento vía
        GWebCache — el camino lento, pero el único posible la primera
        vez que se usa el programa (caché vacía)."""
        cached = load_hub_cache()
        if cached:
            if debug:
                print(f"  [debug] probando {len(cached)} hub(s) de la caché local "
                      f"(sesiones anteriores) antes de consultar GWebCache...")
            try:
                host, port = await self._connect_race(cached, timeout_per_hub, debug, max_concurrent)
                return host, port
            except ConnectionError:
                if debug:
                    print("  [debug] ningún hub de la caché local respondió; "
                          "recurriendo a GWebCache...")

        if debug:
            print("  [debug] consultando GWebCaches G2 para descubrir hubs...")
        candidates = await discover_hubs(timeout_per_cache=timeout_per_hub, debug=debug, proxy=self._proxy)
        if not candidates:
            raise ConnectionError(
                "Ningún GWebCache G2 conocido respondió (son solo dos, sin "
                "garantía de seguir vivos). Prueba con --hub host:puerto si "
                "conoces uno."
            )

        if debug:
            print(f"  [debug] {len(candidates)} hub(s) candidato(s), probando hasta "
                  f"{max_concurrent} a la vez...")

        return await self._connect_race(candidates, timeout_per_hub, debug, max_concurrent)

    async def _connect_race(self, candidates: list[tuple[str, int]], timeout_per_hub: float,
                             debug: bool, max_concurrent: int) -> tuple[str, int]:
        """Motor de la conexión en paralelo usado por connect_auto(): un
        pool de tareas va sacando candidatos de una cola compartida y
        probando el handshake; si un candidato rechaza pero regala
        alternativas por X-Try-Hubs, esas alternativas se añaden a la
        MISMA cola para que cualquier tarea libre las recoja (así no
        hace falta un connect_to_hub_with_fallback anidado). La primera
        tarea que complete el handshake gana; el resto se cancela."""
        queue = list(candidates)
        tried: set[tuple[str, int]] = set()
        winner: tuple[_G2Connection, str, int] | None = None
        last_error: Exception | None = None
        # Los fallos se acumulan aquí en memoria mientras dura la carrera
        # (hasta `max_concurrent` intentos a la vez) y se vuelcan al
        # caché en disco de una sola vez al final, en vez de llamar a
        # record_hub_failure() -una lectura + una escritura del JSON- por
        # cada candidato que falla: con la caché local llena (hasta 200
        # hubs) y una fracción grande de ellos ya muertos, eso suponía
        # decenas de E/S de disco síncronas seguidas dentro del propio
        # bucle de eventos justo cuando más candidatos fallan casi a la
        # vez, y se notaba como microbloqueos reales de la GUI mientras
        # se conecta a Gnutella2 (barra de menú oscurecida y clics que no
        # abrían el menú hasta que la ráfaga terminaba).
        failed_hubs: list[tuple[str, int]] = []

        async def worker() -> None:
            nonlocal winner, last_error
            while winner is None and queue:
                host, port = queue.pop(0)
                if (host, port) in tried:
                    continue
                tried.add((host, port))
                try:
                    conn = await perform_handshake(host, port, timeout=timeout_per_hub, debug=debug, proxy=self._proxy)
                except ConnectionError as e:
                    last_error = e
                    if debug:
                        print(f"  [debug] {host}:{port} no aceptó ({e})")
                    failed_hubs.append((host, port))
                    for cand in getattr(e, "candidates", []):
                        if cand not in tried:
                            queue.append(cand)
                    continue
                except (OSError, asyncio.TimeoutError, EOFError) as e:
                    last_error = e
                    if debug:
                        print(f"  [debug] {host}:{port} no aceptó ({e})")
                    failed_hubs.append((host, port))
                    continue

                if winner is None:
                    winner = (conn, host, port)
                else:
                    conn.close()
                return

        workers = [asyncio.create_task(worker()) for _ in range(min(max_concurrent, len(queue)))]
        await asyncio.gather(*workers)
        _record_hub_failures_batch(failed_hubs)

        if winner is None:
            raise ConnectionError(
                f"Se probaron {len(tried)} hub(s) candidato(s) (GWebCache + "
                f"X-Try-Hubs), pero ninguno aceptó la conexión (último error: "
                f"{last_error}). Prueba de nuevo más tarde o indica un hub "
                "conocido con --hub host:puerto."
            )

        conn, host, port = winner
        self._conn = conn
        self._debug = debug
        self._read_task = asyncio.create_task(self._read_loop())
        self._remember_hub(host, port)
        return host, port

    def _remember_hub(self, host: str, port: int) -> None:
        """Guarda host:puerto al principio de la caché local de hubs
        (ver load_hub_cache), para que connect_auto() lo pruebe primero
        la próxima vez en vez de volver a arrancar desde el GWebCache.
        Una conexión lograda resetea a 0 su contador de fallos."""
        raw = [e for e in _load_hub_cache_raw() if (e["host"], e["port"]) != (host, port)]
        _save_hub_cache_raw([{"host": host, "port": port, "fails": 0}] + raw)

    async def disconnect(self) -> None:
        if self._discovered_hubs:
            # Los hubs que la propia red nos ha ido soplando por /KHL
            # durante la sesión (vecinos y caché del hub al que estamos
            # conectados) son, igual que el hub actual, candidatos con
            # muchas más papeletas de seguir vivos que uno sacado a
            # ciegas del GWebCache — se suman a la caché local para la
            # próxima vez.
            existing = [hp for hp in load_hub_cache() if hp not in self._discovered_hubs]
            save_hub_cache(list(self._discovered_hubs) + existing)
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        if self._conn:
            self._conn.close()
            self._conn = None
            asyncio.ensure_future(upnp.delete_port_mapping(self._listen_port, "TCP"))
        self._active.clear()
        if self._share_server is not None:
            self._share_server.close()
            self._share_server = None

    async def is_connected(self) -> bool:
        return self._conn is not None

    # ---- búsqueda ----

    async def search(self, query: str, timeout: float = 15.0, debug: bool = False,
                      max_results: int | None = None) -> list[SearchResult]:
        if self._conn is None:
            raise RuntimeError("Backend de Gnutella2 no conectado a ningún hub")

        self._pending_hits = []
        self._debug = debug
        muid = new_muid()
        self._collecting_muid = muid
        self._collecting_max_results = max_results
        self._limit_reached = asyncio.Event() if max_results is not None else None

        q2 = build_query(query, muid=muid)
        if debug:
            print(f"  [debug] enviando /Q2 (muid={muid.hex()}) para '{query}'")
        await self._conn.send_packet(q2)

        if max_results is not None:
            try:
                await asyncio.wait_for(self._limit_reached.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(timeout)
        self._collecting_muid = None
        self._collecting_max_results = None
        self._limit_reached = None

        return self._hits_to_results(self._pending_hits, max_results=max_results)

    def _hits_to_results(self, hits_data: list[dict], max_results: int | None = None) -> list[SearchResult]:
        """Convierte una lista de dicts de parse_query_hit() (uno por
        cada /QH2 recibido, ya sea de una búsqueda normal o de un
        Browse Host) en SearchResult. Común a search() y browse_host()."""
        results = []
        for hit_data in hits_data:
            address = hit_data["address"]
            if address is None:
                continue
            host, port = address
            # GUID del servent de origen: hace falta para poder pedir
            # un /PUSH (ver _download_via_push) si la conexión directa
            # a host:port falla por estar detrás de un NAT/firewall.
            # Va metido en el propio source_id (campo extra separado
            # por ':::') porque cada invocación de 'download' es un
            # proceso aparte que no tiene ya en memoria el /QH2
            # original — no hay otro sitio donde guardarlo.
            guid = hit_data["servent_guid"]
            guid_hex = guid.hex() if guid else ""
            for f in hit_data["hits"]:
                if f["sha1"] is None:
                    continue  # sin hash no hay forma de construir la URL de descarga (N2R)
                sha1_b32 = sha1_to_urn_base32(f["sha1"])
                results.append(SearchResult(
                    network=self.network,
                    title=f["filename"],
                    size_bytes=f["size"],
                    source_id=f"{host}:{port}:::{sha1_b32}:::{guid_hex}:::{f['filename']}",
                    seeds_or_sources=1,
                    extra={},
                ))
                if max_results is not None and len(results) >= max_results:
                    return results
        return results

    _BROWSE_HOST_MAX_BYTES = 16 * 1024 * 1024  # límite defensivo por si el origen no cierra la conexión

    async def browse_host(self, host: str, port: int, timeout: float = 20.0) -> list[SearchResult]:
        """Browse Host (/BH, punto 10 del backlog): pide a un servent G2
        concreto (típicamente el hub al que estamos conectados, pero
        vale cualquier host:port sacado de un /QH2) la lista COMPLETA
        de lo que comparte, no solo lo que hiciera match con una
        búsqueda. A nivel de wire no hay ningún paquete G2 nuevo que
        aprender: es una petición HTTP normal a la raíz ('GET /') del
        puerto de escucha del servent, con 'Accept:
        application/x-gnutella2' para pedir la respuesta en formato de
        paquetes /QH2 en vez de HTML (confirmado en
        src/core/uploads.c, sección que rellena 'flags' a partir de la
        cabecera Accept: cuando u->browse_host es TRUE, y en
        src/core/bh_upload.c, que construye esos /QH2 con
        g2_build_qh2_results a partir de TODA la carpeta compartida, no
        de un resultado de búsqueda). Cada /QH2 de la respuesta tiene
        la misma forma que uno de búsqueda normal (mismo
        parse_query_hit), solo que con MUID en blanco (no responde a
        ninguna búsqueda concreta) y uno o más /H (uno por archivo
        compartido) en vez de solo los que hicieran match.

        Se pide explícitamente HTTP/1.0: así el servidor no activa
        'Transfer-Encoding: chunked' (ver supports_chunked() en
        uploads.c, que solo se activa con HTTP/1.1 o superior) y
        simplemente vuelca los paquetes uno detrás de otro hasta cerrar
        la conexión — mucho más simple de leer que tener que
        desenvolver framing chunked por encima del framing ya binario
        de los propios paquetes G2. Tampoco se manda 'Accept-Encoding',
        así que el servidor no comprime la respuesta (select_encoding()
        en uploads.c solo activa deflate/gzip si el cliente lo pide)."""
        try:
            reader, writer = await proxy_open_connection(
                host, port, proxy=self._proxy, timeout=self._DOWNLOAD_CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError) as e:
            raise ConnectionError(f"no se pudo conectar a {host}:{port} ({e})") from e

        try:
            request = (
                "GET / HTTP/1.0\r\n"
                f"Host: {host}\r\n"
                "User-Agent: P2P-Total/0.1\r\n"
                "Accept: application/x-gnutella2\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()

            status_line = await asyncio.wait_for(reader.readline(), timeout=self._DOWNLOAD_CONNECT_TIMEOUT)
            if b"200" not in status_line:
                raise ConnectionError(
                    f"{host}:{port} rechazó la petición de Browse Host: "
                    f"{status_line.decode(errors='replace').strip()}"
                )

            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=self._DOWNLOAD_STALL_TIMEOUT)
                if line in (b"\r\n", b""):
                    break

            body = bytearray()
            while len(body) < self._BROWSE_HOST_MAX_BYTES:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
                if not chunk:
                    break
                body.extend(chunk)
        finally:
            writer.close()

        hits_data = []
        pos = 0
        body = bytes(body)
        while pos < len(body):
            pkt, pos = decode_packet(body, pos)
            if pkt is None or pkt.name != "QH2":
                continue
            hit_data = parse_query_hit(pkt)
            if hit_data is not None:
                hits_data.append(hit_data)
        return self._hits_to_results(hits_data)

    # ---- descarga ----

    async def start_download(self, result: SearchResult, dest_path: str) -> Download:
        # Formato nuevo: host:puerto:::sha1_b32:::guid_hex:::nombre (el
        # GUID hace falta para poder pedir un /PUSH si falla la
        # conexión directa — ver _try_push_download). Se acepta también
        # el formato viejo sin GUID (3 campos) por compatibilidad con
        # source_id ya copiados de una búsqueda anterior a este cambio.
        parts = result.source_id.split(":::", 3)
        if len(parts) == 4:
            host_port, sha1_b32, guid_hex, _filename = parts
        else:
            host_port, sha1_b32, _filename = parts
            guid_hex = ""
        host, port_str = host_port.rsplit(":", 1)
        port = int(port_str)
        guid = bytes.fromhex(guid_hex) if guid_hex else None

        download = Download(
            id=None,
            network=self.network,
            title=result.title,
            source_id=result.source_id,
            dest_path=dest_path,
            size_bytes=result.size_bytes,
            state=DownloadState.SEARCHING_SOURCES,
        )
        entry = {
            "download": download,
            "host": host,
            "port": port,
            "sha1_b32": sha1_b32,
            "guid": guid,
            "dest_path": dest_path,
            "paused": False,
            "cancelled": False,
            "writer": None,
            "limiter": RateLimiter(),
        }
        self._active[result.source_id] = entry

        asyncio.create_task(self._download_via_http(entry))
        return download

    _DOWNLOAD_CONNECT_TIMEOUT = 15.0
    _DOWNLOAD_STALL_TIMEOUT = 20.0  # sin datos nuevos durante esto = origen colgado, no solo lento
    _PUSH_WAIT_TIMEOUT = 30.0  # cuánto esperamos a que el origen empujado conecte de vuelta

    async def _download_via_http(self, entry: dict) -> None:
        """Descarga por hash: GET /uri-res/N2R?urn:sha1:<base32> — el
        propio spec de G2 lo define así (sección downloads.c), no por
        índice de archivo como en G1.

        La dirección que trae un /QH2 es la que el propio origen dijo
        tener (vía /NA), no una garantía de que sea alcanzable desde
        aquí: en la práctica muchísimos servents comparten detrás de
        un NAT/firewall sin tener abierto ese puerto. Si la conexión
        directa falla Y tenemos el GUID del origen (ver start_download)
        Y sabemos nuestra propia IP externa (cabecera Remote-IP del
        hub), probamos el plan B real de Gnutella: pedirle al hub, vía
        un paquete /PUSH, que el propio origen nos llame a nosotros en
        vez de al revés (_try_push_download). Si eso también falla —o
        no es aplicable—, se informa con un mensaje claro en vez de
        quedarse colgado, que es justo lo que pasaba antes de meter
        timeouts explícitos en cada punto de espera de red."""
        download = entry["download"]
        host, port, sha1_b32, guid = entry["host"], entry["port"], entry["sha1_b32"], entry["guid"]
        try:
            direct_error = await self._try_direct_download(entry)
        except _HttpRejected as e:
            # El origen respondió con un HTTP real (404, 503...): está
            # claramente alcanzable, así que un /PUSH no serviría de nada.
            download.state = DownloadState.ERROR
            download.error_message = str(e)
            if self._progress_callback:
                self._progress_callback(download)
            return
        if direct_error is None:
            return  # éxito (o pausada/cancelada a mitad, ya resuelto dentro)

        if guid is None or self._conn is None or self._conn.remote_ip is None:
            download.state = DownloadState.ERROR
            if guid is None:
                extra = " (este resultado no traía el GUID del origen: repite la búsqueda para conseguirlo)"
            else:
                extra = " (no se pudo saber tu propia IP externa a través del hub, así que no se puede pedir /PUSH)"
            download.error_message = f"{direct_error}{extra}"
            if self._progress_callback:
                self._progress_callback(download)
            return

        if self._debug:
            print(f"  [debug] conexión directa a {host}:{port} falló ({direct_error}); "
                  "probando /PUSH como alternativa...")

        push_error = await self._try_push_download(entry)
        if push_error is None:
            return  # éxito

        download.state = DownloadState.ERROR
        download.error_message = (
            f"Conexión directa: {direct_error}. Vía /PUSH: {push_error}. "
            "Prueba otro resultado de la búsqueda."
        )
        if self._progress_callback:
            self._progress_callback(download)

    async def _try_direct_download(self, entry: dict) -> str | None:
        """Intento normal: conectar directamente a host:port. Devuelve
        None si la descarga se completó (o quedó pausada/cancelada a
        mitad, ya resuelto dentro), o un mensaje de error (sin tocar
        download.state: eso lo decide _download_via_http, que puede
        tener un plan B vía /PUSH todavía pendiente)."""
        host, port = entry["host"], entry["port"]
        try:
            reader, writer = await proxy_open_connection(
                host, port, proxy=self._proxy, timeout=self._DOWNLOAD_CONNECT_TIMEOUT
            )
        except asyncio.TimeoutError:
            return f"{host}:{port} no respondió a tiempo"
        except (ConnectionError, OSError) as e:
            return f"no se pudo conectar directamente al host ({e})"

        return await self._send_get_and_receive(reader, writer, host, entry)

    async def _try_push_download(self, entry: dict) -> str | None:
        """Plan B cuando la conexión directa falla: mandamos un /PUSH al
        hub (payload = nuestra propia dirección IP:puerto, hijo /TO =
        GUID del origen — formato confirmado en g2_build_push, src/core/
        g2/build.c) para que sea el propio origen quien nos llame a
        nosotros. Solo tiene alguna posibilidad de funcionar si NOSOTROS
        somos alcanzables desde fuera (puerto reenviado o sin NAT) — el
        propio gtk-gnutella tiene la misma limitación (ni construye el
        /PUSH si no tiene un puerto de escucha válido). La línea de
        vuelta que manda el origen al conectar tiene el formato 'PUSH
        guid:<hex>\\r\\n\\r\\n' (función parse_giv, src/core/downloads.c),
        tras la cual el intercambio HTTP es idéntico al de una descarga
        directa."""
        host, guid = entry["host"], entry["guid"]
        connected: asyncio.Future = asyncio.get_event_loop().create_future()

        async def on_client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if not connected.done():
                connected.set_result((reader, writer))
            else:
                writer.close()

        # Se intenta primero el puerto fijo configurado (el que el
        # usuario puede reenviar en su router, igual que con DC++/eMule)
        # para que un /PUSH tenga alguna posibilidad real de funcionar;
        # si ya está ocupado (p.ej. otra descarga /PUSH en curso a la
        # vez), se cae a un puerto efímero — funciona igual salvo que
        # nadie lo tiene reenviado, así que ese /PUSH concreto solo
        # tendrá éxito si el origen no está detrás de NAT.
        try:
            server = await asyncio.start_server(on_client_connected, host="0.0.0.0", port=self._listen_port)
        except OSError:
            server = await asyncio.start_server(on_client_connected, host="0.0.0.0", port=0)
        listen_port = server.sockets[0].getsockname()[1]

        try:
            push_pkt = G2Packet(
                "PUSH",
                payload=_encode_g2_address(self._conn.remote_ip, listen_port),
                children=[G2Packet("TO", payload=guid)],
            )
            if self._debug:
                print(f"  [debug] enviando /PUSH (guid={guid.hex()}) vía el hub, "
                      f"anunciando {self._conn.remote_ip}:{listen_port}...")
            await self._conn.send_packet(push_pkt)

            try:
                reader, writer = await asyncio.wait_for(connected, timeout=self._PUSH_WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                return (
                    f"nadie conectó de vuelta a {self._conn.remote_ip}:{listen_port} en "
                    f"{self._PUSH_WAIT_TIMEOUT:.0f}s (probable: tú también estás detrás de un "
                    "NAT/firewall sin ese puerto abierto, o el origen ya no está en la red)"
                )

            try:
                callback_line = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), timeout=self._DOWNLOAD_STALL_TIMEOUT
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                writer.close()
                return "el origen conectó de vuelta pero no mandó la línea PUSH esperada"

            if self._debug:
                print(f"  [debug] conexión /PUSH entrante: {callback_line!r}")
            if not callback_line.startswith((b"PUSH ", b"GIV ")):
                writer.close()
                return f"conexión entrante inesperada (no era PUSH/GIV): {callback_line!r}"

            try:
                return await self._send_get_and_receive(reader, writer, host, entry)
            except _HttpRejected as e:
                # El origen SÍ respondió (p.ej. 404): el push funcionó a
                # nivel de conexión, así que esto ya es un fallo final,
                # no de red.
                return str(e)
        finally:
            server.close()
            await server.wait_closed()

    async def _send_get_and_receive(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                     host: str, entry: dict) -> str | None:
        """Una vez hay un socket TCP abierto hacia el origen (por
        conexión directa o por /PUSH, da igual), el intercambio HTTP es
        idéntico: mandar el GET /uri-res/N2R y volcar la respuesta a
        disco. Devuelve None si se completó con éxito (o quedó
        pausada/cancelada a mitad, ya reflejado en download.state), o
        un mensaje de error (sin tocar download.state en ese caso, por
        la misma razón que en _try_direct_download).

        Soporta reanudar: si ya hay bytes en disco de un intento
        anterior (pausado desde este mismo backend), pedimos
        'Range: bytes=<offset>-'. Si el origen responde 206 honra el
        rango y seguimos por donde íbamos (abriendo el fichero en modo
        'ab'); si en cambio responde 200 (no soporta Range: bastantes
        servents G2 antiguos no lo implementan) no hay más remedio que
        volver a bajarlo entero desde el byte 0, igual que si nunca
        hubiéramos tenido nada en disco."""
        download = entry["download"]
        entry["writer"] = writer
        dest_path = entry["dest_path"]
        sha1_b32 = entry["sha1_b32"]
        try:
            os.makedirs(dest_path, exist_ok=True)
            out_path = os.path.join(dest_path, _sanitize_filename(download.title))
            offset = os.path.getsize(out_path) if os.path.exists(out_path) else 0

            path = f"/uri-res/N2R?urn:sha1:{sha1_b32}"
            range_header = f"Range: bytes={offset}-\r\n" if offset else ""
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                f"User-Agent: P2P-Total/0.1\r\n"
                # Algunos servents G2 solo sirven a clientes que se
                # identifican como tal (mismo header que manda
                # gtk-gnutella para orígenes G2 — ver DLS_A_FAKE_G2 /
                # DLS_A_G2_ONLY en src/core/downloads.c).
                f"X-Features: g2/1.0\r\n"
                f"{range_header}"
                f"Connection: close\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()

            status_line = await asyncio.wait_for(reader.readline(), timeout=self._DOWNLOAD_CONNECT_TIMEOUT)
            # 200 = contenido completo, 206 = parcial (el origen honró
            # nuestro Range:). Cualquier otro código (404, 503...) es un
            # rechazo a nivel de aplicación del origen: está claramente
            # alcanzable, así que no tiene sentido reintentar por /PUSH
            # (ver _HttpRejected).
            if b"200" not in status_line and b"206" not in status_line:
                raise _HttpRejected(f"HTTP: {status_line.decode(errors='replace').strip()}")
            honored_range = b"206" in status_line
            if offset and not honored_range:
                offset = 0  # el origen no soporta Range: hay que volver a bajarlo entero

            content_length = None
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=self._DOWNLOAD_STALL_TIMEOUT)
                if line in (b"\r\n", b""):
                    break
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())

            download.state = DownloadState.DOWNLOADING
            total_size = (offset + content_length) if (honored_range and content_length) else content_length

            downloaded = offset
            speed_window_start = time.monotonic()
            speed_window_bytes = downloaded
            with open(out_path, "ab" if offset else "wb") as f:
                while True:
                    if entry["cancelled"] or entry["paused"]:
                        break
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=self._DOWNLOAD_STALL_TIMEOUT)
                    if not chunk:
                        break
                    await global_download_limiter.consume(len(chunk))
                    await entry["limiter"].consume(len(chunk))
                    f.write(chunk)
                    downloaded += len(chunk)
                    download.downloaded_bytes = downloaded
                    if total_size:
                        download.size_bytes = total_size

                    now = time.monotonic()
                    elapsed = now - speed_window_start
                    if elapsed >= 0.5:
                        download.speed_bps = (downloaded - speed_window_bytes) / elapsed
                        speed_window_start = now
                        speed_window_bytes = downloaded
                    if self._progress_callback:
                        self._progress_callback(download)

            download.speed_bps = 0.0
            if entry["cancelled"]:
                return None  # cancel_download ya dejó el estado como corresponde
            if entry["paused"]:
                download.state = DownloadState.PAUSED
                if self._progress_callback:
                    self._progress_callback(download)
                return None

            if total_size and downloaded != total_size:
                return f"descarga incompleta: {downloaded}/{total_size} bytes"

            padded = sha1_b32 + "=" * (-len(sha1_b32) % 8)
            expected_sha1 = base64.b32decode(padded.upper())
            got_sha1 = await run_in_daemon_thread(_sha1_of_file, out_path, name="g2-sha1-verify")
            if got_sha1 != expected_sha1:
                return "verificación SHA1 fallida (fichero corrupto o incompleto)"

            download.state = DownloadState.COMPLETED
            if self._progress_callback:
                self._progress_callback(download)
            return None
        except asyncio.TimeoutError:
            download.speed_bps = 0.0
            if entry["paused"] or entry["cancelled"]:
                if entry["paused"]:
                    download.state = DownloadState.PAUSED
                    if self._progress_callback:
                        self._progress_callback(download)
                return None
            return "el origen dejó de responder a mitad de la descarga"
        except (ConnectionError, OSError) as e:
            download.speed_bps = 0.0
            if entry["paused"] or entry["cancelled"]:
                if entry["paused"]:
                    download.state = DownloadState.PAUSED
                    if self._progress_callback:
                        self._progress_callback(download)
                return None
            return f"conexión cortada a mitad de la descarga ({e})"
        finally:
            entry["writer"] = None
            writer.close()

    async def pause_download(self, download: Download) -> None:
        entry = self._active.get(download.source_id)
        if entry is None:
            return
        entry["paused"] = True
        if entry["writer"]:
            entry["writer"].close()
        download.state = DownloadState.PAUSED
        download.speed_bps = 0.0
        if self._progress_callback:
            self._progress_callback(download)

    async def resume_download(self, download: Download) -> None:
        entry = self._active.get(download.source_id)
        if entry is None:
            return
        entry["paused"] = False
        entry["cancelled"] = False
        download.state = DownloadState.SEARCHING_SOURCES
        if self._progress_callback:
            self._progress_callback(download)
        asyncio.create_task(self._download_via_http(entry))

    async def reattach_download(self, download: Download) -> None:
        """Reengancha una descarga tras reiniciar la app: el `source_id`
        de G2 ya es autocontenido (host, puerto, hash SHA1 y GUID del
        origen), así que basta con reconstruir la entrada y, si no
        estaba en pausa, relanzar `_download_via_http` (que reanuda
        desde el tamaño ya escrito en disco vía `Range:` HTTP, con
        caída automática a reinicio desde 0 si el origen no lo soporta)."""
        if download.source_id in self._active:
            return
        parts = download.source_id.split(":::", 3)
        if len(parts) == 4:
            host_port, sha1_b32, guid_hex, _filename = parts
        else:
            host_port, sha1_b32, _filename = parts
            guid_hex = ""
        host, port_str = host_port.rsplit(":", 1)
        port = int(port_str)
        guid = bytes.fromhex(guid_hex) if guid_hex else None
        entry = {
            "download": download,
            "host": host,
            "port": port,
            "sha1_b32": sha1_b32,
            "guid": guid,
            "dest_path": download.dest_path,
            "paused": download.state == DownloadState.PAUSED,
            "cancelled": False,
            "writer": None,
            "limiter": RateLimiter(),
        }
        self._active[download.source_id] = entry
        if download.state != DownloadState.PAUSED:
            download.state = DownloadState.SEARCHING_SOURCES
            if self._progress_callback:
                self._progress_callback(download)
            asyncio.create_task(self._download_via_http(entry))

    async def cancel_download(self, download: Download) -> None:
        entry = self._active.get(download.source_id)
        if entry is not None:
            entry["cancelled"] = True
            if entry["writer"]:
                entry["writer"].close()
        download.state = DownloadState.CANCELLED
        download.speed_bps = 0.0
        self._active.pop(download.source_id, None)

    def subscribe_progress(self, callback: Callable[[Download], None]) -> None:
        self._progress_callback = callback

    def set_download_limit(self, download: Download, rate_bps: int) -> None:
        entry = self._active.get(download.source_id)
        if entry is not None:
            entry["limiter"].set_rate(rate_bps)

    def get_stats(self) -> dict:
        if self._conn is None:
            return {}
        peer = self._conn.writer.get_extra_info("peername")
        stats = {
            "server": f"{peer[0]}:{peer[1]}" if peer else None,
            "known_peers": len(self._discovered_hubs),
            "active_transfers": len(self._active),
        }
        if self._shared_library is not None and self._shared_library.enabled:
            stats["shared_files"] = len(self._shared_library.list_files())
            stats["active_uploads"] = self._active_uploads
        return stats

    # ---- compartir (servir a otros peers) ----

    async def _handle_incoming_connection(self, reader: asyncio.StreamReader,
                                           writer: asyncio.StreamWriter) -> None:
        """Conexión entrante a nuestro puerto de escucha: al no ser
        nosotros un hub, lo único que puede ser es un peer pidiéndonos
        directamente un fichero por HTTP (nos encontró en un /QH2
        nuestro con nuestra propia /NA)."""
        # Punto 39 del backlog: el camino saliente (conexión al hub, y
        # descargas HTTP directas a otros peers) ya pasa por el mismo
        # filtro dentro de `core.proxy.open_connection`.
        peer_ip = _peer_ip_from_writer(writer)
        if peer_ip and ip_filter.is_blocked(peer_ip):
            writer.close()
            return
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=20.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            writer.close()
            return
        await self._serve_http(reader, writer, request_line)

    async def _handle_incoming_push(self, pkt: G2Packet) -> None:
        """El hub nos reenvía un /PUSH (payload = dirección de quien lo
        pide, hijo /TO = GUID del destino) porque alguien no pudo
        conectar directamente con nosotros para descargar algo que
        compartimos. Marcamos hacia esa dirección y nos anunciamos con
        la misma línea 'PUSH guid:<hex>' que ya sabemos parsear en el
        lado descarga (_try_push_download) — a partir de ahí, servimos
        la petición HTTP que nos manden exactamente igual que una
        conexión directa."""
        to = pkt.find("TO")
        if to is None or to.payload != self._guid:
            return  # no es un /PUSH dirigido a nosotros
        address = _parse_address(pkt.payload)
        if address is None:
            return
        host, port = address
        if self._debug:
            print(f"  [debug] /PUSH entrante para nosotros: conectando a {host}:{port}...")
        try:
            reader, writer = await proxy_open_connection(host, port, proxy=self._proxy, timeout=15.0)
        except (OSError, asyncio.TimeoutError):
            return
        try:
            writer.write(f"PUSH guid:{self._guid.hex()}\r\n\r\n".encode("ascii"))
            await writer.drain()
            request_line = await asyncio.wait_for(reader.readline(), timeout=20.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            writer.close()
            return
        await self._serve_http(reader, writer, request_line)

    async def _serve_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           request_line: bytes) -> None:
        """Sirve UNA petición 'GET /uri-res/N2R?urn:sha1:<base32> HTTP/1.1'
        contra la carpeta compartida (mismo formato que ya sabemos
        construir en _send_get_and_receive, aquí en el lado servidor).
        Soporta 'Range: bytes=<offset>-' para reanudar, igual que ya
        hacemos como cliente."""
        try:
            parts = request_line.decode("ascii", errors="replace").split()
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=15.0)
                if line in (b"\r\n", b""):
                    break
                if b":" in line:
                    key, _, value = line.decode("ascii", errors="replace").partition(":")
                    headers[key.strip().lower()] = value.strip()

            if method == "GET" and path == "/":
                await self._serve_browse_host(writer, headers)
                return

            shared = None
            if method == "GET" and "urn:sha1:" in path and self._shared_library is not None:
                sha1_b32 = path.rsplit("urn:sha1:", 1)[-1]
                shared = self._shared_library.find_by_sha1_b32(sha1_b32)

            if shared is None:
                writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            offset = 0
            range_header = headers.get("range", "")
            if range_header.startswith("bytes="):
                try:
                    offset = int(range_header[len("bytes="):].split("-", 1)[0])
                except ValueError:
                    offset = 0
            offset = max(0, min(offset, shared.size))

            status = "206 Partial Content" if offset else "200 OK"
            remaining = shared.size - offset
            response = (
                f"HTTP/1.1 {status}\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Length: {remaining}\r\n"
            )
            if offset:
                response += f"Content-Range: bytes {offset}-{shared.size - 1}/{shared.size}\r\n"
            response += "Connection: close\r\n\r\n"
            writer.write(response.encode("ascii"))
            await writer.drain()

            self._active_uploads += 1
            try:
                with open(shared.path, "rb") as f:
                    f.seek(offset)
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        await global_upload_limiter.consume(len(chunk))
                        writer.write(chunk)
                        await writer.drain()
                        stats_tracker.record_uploaded(self.network, len(chunk))
            finally:
                self._active_uploads -= 1
        except (ConnectionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    _BROWSE_HOST_BATCH_SIZE = 50  # ficheros por cada /QH2 al contestar un Browse Host propio

    async def _serve_browse_host(self, writer: asyncio.StreamWriter, headers: dict[str, str]) -> None:
        """Lado servidor de Browse Host (petición 'GET /'): contesta con
        toda nuestra carpeta compartida en forma de paquetes /QH2 (uno
        cada `_BROWSE_HOST_BATCH_SIZE` ficheros, igual de espíritu que
        el BH_SCAN_AHEAD de gtk-gnutella), con MUID en blanco ya que no
        responde a ninguna búsqueda concreta. Solo entendemos el
        formato de paquetes G2 (`Accept: application/x-gnutella2`), no
        el HTML que un navegador normal pediría — no hay ninguna UI
        pensada para verlo así, así que directamente no lo ofrecemos."""
        accept = headers.get("accept", "")
        if "application/x-gnutella2" not in accept:
            writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        if self._shared_library is None or not self._shared_library.enabled:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        files = self._shared_library.list_files()
        blank_muid = bytes(16)
        na = G2Packet("NA", payload=_encode_g2_address(self._conn_remote_ip_for_serving(), self._listen_port))
        gu = G2Packet("GU", payload=self._guid)

        body = bytearray()
        for i in range(0, len(files), self._BROWSE_HOST_BATCH_SIZE):
            batch = files[i:i + self._BROWSE_HOST_BATCH_SIZE]
            hit_children = []
            for f in batch:
                urn = G2Packet("URN", payload=b"sha1\x00" + f.sha1)
                hit_children.append(G2Packet("H", children=[
                    G2Packet("DN", payload=f.rel_path.encode("utf-8", errors="replace")),
                    G2Packet("SZ", payload=_encode_vlint(f.size)),
                    urn,
                ]))
            qh2 = G2Packet("QH2", payload=bytes([1]) + blank_muid, children=[na, gu] + hit_children)
            body += encode_packet(qh2)

        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/x-gnutella2\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(response.encode("ascii"))
        writer.write(bytes(body))
        await writer.drain()

    def _conn_remote_ip_for_serving(self) -> str:
        """Nuestra propia IP tal como la ve el hub (Remote-IP del
        handshake, ver _G2Connection), para meterla en el /NA de cada
        /QH2 que contestamos — mismo dato que ya usa
        _handle_incoming_query. Si no estamos conectados a ningún hub
        (p.ej. nos llega un Browse Host directo sin haber hecho
        handshake todavía) no hay forma de saberla; 0.0.0.0 deja claro
        que la dirección no es válida en vez de inventar una."""
        return self._conn.remote_ip if self._conn is not None and self._conn.remote_ip else "0.0.0.0"

    async def _handle_incoming_query(self, pkt: G2Packet) -> None:
        """Contesta con /QH2 si algo de nuestra carpeta compartida hace
        match por palabras contra el /DN de una búsqueda (/Q2) ajena
        que el hub nos reenvía."""
        if len(pkt.payload) < 16 or self._conn is None or self._conn.remote_ip is None:
            return
        muid = pkt.payload[:16]
        dn = pkt.find("DN")
        if dn is None:
            return
        words = [w for w in dn.payload.decode("utf-8", errors="replace").lower().split() if w]
        if not words:
            return

        matches = [
            f for f in self._shared_library.list_files()
            if all(w in f.rel_path.lower() for w in words)
        ][:10]
        if not matches:
            return

        hit_children = []
        for f in matches:
            urn = G2Packet("URN", payload=b"sha1\x00" + f.sha1)
            hit_children.append(G2Packet("H", children=[
                G2Packet("DN", payload=f.rel_path.encode("utf-8", errors="replace")),
                G2Packet("SZ", payload=_encode_vlint(f.size)),
                urn,
            ]))

        na = G2Packet("NA", payload=_encode_g2_address(self._conn.remote_ip, self._listen_port))
        gu = G2Packet("GU", payload=self._guid)
        qh2 = G2Packet("QH2", payload=bytes([1]) + muid, children=[na, gu] + hit_children)
        if self._debug:
            print(f"  [debug] contestando /QH2 con {len(matches)} resultado(s) propio(s)")
        await self._conn.send_packet(qh2)

    # ---- internos ----

    async def _read_loop(self) -> None:
        try:
            while True:
                pkt = await self._conn.read_packet()
                if pkt is None:
                    continue  # marcador de fin de flujo suelto, ignorar
                if self._debug:
                    print(f"  [debug] hub -> /{pkt.name} (payload={len(pkt.payload)}b, {len(pkt.children)} hijo(s))")

                if pkt.name == "QH2" and len(pkt.payload) >= 17 and pkt.payload[1:17] == self._collecting_muid:
                    parsed = parse_query_hit(pkt)
                    if parsed:
                        self._pending_hits.append(parsed)
                        if self._debug:
                            print(f"  [debug] QH2: {len(parsed['hits'])} resultado(s) de {parsed['address']}")
                        if self._collecting_max_results is not None and self._limit_reached is not None:
                            total = sum(
                                1 for h in self._pending_hits for f in h["hits"] if f["sha1"] is not None
                            )
                            if total >= self._collecting_max_results:
                                self._limit_reached.set()
                elif pkt.name == "PI":
                    # Cortesía de red: si el hub nos hace un ping,
                    # respondemos con un pong para no parecer un nodo
                    # muerto (mismo espíritu que el Ping/Pong de G1).
                    await self._conn.send_packet(G2Packet("PO"))
                elif pkt.name == "Q2" and self._shared_library is not None and self._shared_library.enabled:
                    # El hub reenvía búsquedas (/Q2) de otras hojas para
                    # que las hojas conectadas contesten con su propio
                    # contenido, tal como ya reenvía $Search en DC++ —
                    # no confirmado byte a byte contra un hub G2 real
                    # (solo probado contra un hub sintético propio, ver
                    # README.md: a propósito no se ha vuelto a probar
                    # sharing/subida contra la red G2 real, por el mismo
                    # motivo de seguridad ya documentado ahí para la
                    # búsqueda).
                    await self._handle_incoming_query(pkt)
                elif pkt.name == "PUSH" and self._shared_library is not None and self._shared_library.enabled:
                    asyncio.create_task(self._handle_incoming_push(pkt))
                elif pkt.name == "KHL":
                    # Descubrimiento de MÁS hubs por la propia red, sin
                    # nosotros pedir nada: el hub al que estamos
                    # conectados manda esto periódicamente con /NH
                    # (hubs vecinos suyos, mismo formato de dirección de
                    # 6 bytes que /NA) y /CH (hubs en su caché, no
                    # necesariamente vecinos — payload de 10 bytes:
                    # dirección de 6 + 4 bytes de timestamp que
                    # ignoramos) — sección g2_node_handle_khl de
                    # src/core/g2/node.c. Buscamos en TODO el subárbol,
                    # no solo hijos directos, porque gtk-gnutella recorre
                    # /KHL igual (g2_tree_child_foreach = barrido
                    # completo, sección tree.c), no solo su primer nivel.
                    before = len(self._discovered_hubs)
                    for nh in _find_all_recursive(pkt, "NH"):
                        addr = _parse_address(nh.payload)
                        if addr:
                            self._discovered_hubs.add(addr)
                    for ch in _find_all_recursive(pkt, "CH"):
                        if len(ch.payload) == 10:
                            addr = _parse_address(ch.payload[:6])
                            if addr:
                                self._discovered_hubs.add(addr)
                    if self._debug and len(self._discovered_hubs) > before:
                        print(f"  [debug] KHL: {len(self._discovered_hubs) - before} hub(s) nuevo(s) descubiertos")
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError):
            pass

    @property
    def discovered_hubs(self) -> set[tuple[str, int]]:
        """Hubs activos descubiertos vía /KHL desde que nos conectamos
        (más allá del hub inicial), listos para reutilizar en la
        próxima sesión con --hub host:puerto."""
        return set(self._discovered_hubs)
