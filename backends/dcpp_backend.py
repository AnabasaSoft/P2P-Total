"""
Backend DC++ implementando el protocolo NMDC directamente sobre sockets
asyncio, sin librerías externas ni procesos aparte (aMule/DC++ real no
se lanzan como binario: hablamos el protocolo a mano).

Referencia usada para implementar esto: la especificación NMDC 1.3 de
Fredrik Ullner (https://nmdc.sourceforge.io/NMDC.html), en particular
las secciones 4.4.38-4.4.39 ($Lock/$Key) y 4.4.23-4.4.24 ($Search/$SR).

Diferencias clave frente a Soulseek/BitTorrent:
- Es una red basada en HUBS: hay que elegir/conectar a un hub concreto
  (no hay un único punto de entrada como slsknet.org). El usuario debe
  indicar host:puerto de un hub al conectar.
- Las búsquedas de texto libre se envían al hub, que las reenvía a
  otros usuarios; los resultados ($SR) llegan de vuelta por la misma
  conexión al hub (modo "pasivo", el que implementamos aquí).
- Las descargas requieren una conexión cliente-cliente APARTE de la
  conexión al hub, con su propio handshake $MyNick/$Lock/$Direction/$Key.
  Para que un peer nos pueda conectar, necesitamos modo ACTIVO: escuchar
  en un puerto TCP propio (por defecto 412) y tenerlo accesible desde
  fuera (igual que el 6881 de BitTorrent o el 60000 de Soulseek).
"""

import asyncio
import os
import random
import re
import time
from base64 import b32decode, b32encode
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse
from xml.etree import ElementTree

from core import upnp
from core.backend_base import NetworkBackend
from core.http_client import http_get
from core.models import Download, DownloadState, Network, SearchResult
from core.proxy import ProxyConfig
from core.proxy import open_connection as proxy_open_connection
from core.rate_limiter import RateLimiter, global_download_limiter, global_upload_limiter
from core.sharing import SharedLibrary
from core.stats_tracker import stats_tracker
from core.tth import tth_of_file


def tth_to_base32(tth: bytes) -> str:
    return b32encode(tth).decode("ascii").rstrip("=")


def tth_from_base32(tth_b32: str) -> bytes:
    padded = tth_b32 + "=" * (-len(tth_b32) % 8)
    return b32decode(padded.upper())

# Los puertos <1024 (como el 412 "de libro" de DC++) están reservados
# en Linux y solo root puede abrirlos. Usamos uno por encima de 1024
# por defecto: el protocolo no exige el 412 en concreto, el hub se
# limita a reenviar a los peers el puerto que nosotros le digamos.
DEFAULT_CLIENT_PORT = 41290
CLIENT_VERSION = "1,0091"

_ESCAPE_MAP = {0: "000", 5: "005", 36: "036", 96: "096", 124: "124", 126: "126"}


def lock_to_key(lock: bytes) -> bytes:
    """
    Algoritmo exacto de la sección 4.4.39 del spec NMDC:

        key[i] = lock[i] xor lock[i-1]                       (i = 1..len-1)
        key[0] = lock[0] xor lock[len-1] xor lock[len-2] xor 5
        luego cada byte del key se intercambia de nibble (swap alto/bajo)
        y por último se escapan los bytes 0,5,36,96,124,126.
    """
    n = len(lock)
    if n < 2:
        raise ValueError("Lock demasiado corto para calcular la key")

    key = bytearray(n)
    key[0] = lock[0] ^ lock[n - 1] ^ lock[n - 2] ^ 5
    for i in range(1, n):
        key[i] = lock[i] ^ lock[i - 1]

    for i in range(n):
        b = key[i]
        key[i] = ((b << 4) & 0xF0) | ((b >> 4) & 0x0F)

    out = bytearray()
    for b in key:
        if b in _ESCAPE_MAP:
            out.extend(f"/%DCN{_ESCAPE_MAP[b]}%/".encode("ascii"))
        else:
            out.append(b)
    return bytes(out)


def escape_nmdc(text: str) -> str:
    """Escapa $ y | para texto que va dentro de un mensaje NMDC (chat,
    búsquedas, etc. usan sustitución directa de caracteres problemáticos)."""
    return text.replace("&", "&amp;").replace("$", "&#36;").replace("|", "&#124;")


def _format_ctm_address(ip: str, port: int) -> str:
    """Formatea ip:puerto para un $ConnectToMe saliente. NMDC no define
    oficialmente IPv6 (el spec es de 2003), pero los clientes reales
    modernos (DC++, ADC aparte) que sí lo soportan usan la misma
    convención que una URL: la IPv6 entre corchetes, porque si no el
    ":" que separa host y puerto se confundiría con los ":" propios de
    la dirección."""
    if ":" in ip:
        return f"[{ip}]:{port}"
    return f"{ip}:{port}"


def _split_ctm_address(addr: str) -> tuple[str, str] | None:
    """Inverso de `_format_ctm_address`: separa host y puerto de un
    'host:puerto' aceptando tanto IPv4/nombre de host sin corchetes
    como una IPv6 literal entre corchetes ('[::1]:411'). Devuelve
    None si `addr` no tiene ninguna de las dos formas."""
    if addr.startswith("["):
        host, sep, rest = addr[1:].partition("]")
        if not sep or not rest.startswith(":"):
            return None
        return host, rest[1:]
    host, sep, port_str = addr.rpartition(":")
    if not sep:
        return None
    return host, port_str


def parse_search_string(query: str) -> str:
    """Construye el string de búsqueda NMDC: size_restricted?is_max?size?type?pattern.
    Los espacios del patrón se sustituyen por '$' (sección 4.4.23)."""
    pattern = query.strip().replace(" ", "$")
    return f"F?F?0?1?{pattern}"


def parse_sr(line: str) -> dict | None:
    """
    Parsea una respuesta $SR (sección 4.4.24):

        $SR nick filename<0x05>filesize free_slots/total_slots<0x05>hub_name (hubip[:port])|

    OJO con el formato real: separando por \\x05 salen exactamente 3
    partes -- [filename, "filesize free_slots/total_slots", hub_info] --
    porque el tamaño y los slots van juntos en la parte central,
    separados por un simple espacio (no por \\x05). Ejemplo literal del
    spec: "ponies.txt<0x05>437 3/4<0x05>Testhub (192.168.1.1:411)".

    Devuelve None si la línea no tiene esta forma (resultados de
    directorio sin tamaño, o cualquier variante no reconocida) para
    poder ignorarla con seguridad en vez de reventar o dar datos falsos.
    """
    if not line.startswith("$SR "):
        return None
    body = line[len("$SR "):].rstrip("|")

    try:
        nick, rest = body.split(" ", 1)
        parts = rest.split("\x05")
        # 3 partes = resultado normal; 4 = llevaba target_nick colgando
        # (no debería pasar, el hub lo quita, pero por si acaso lo ignoramos)
        if len(parts) not in (3, 4):
            return None

        filename = parts[0]
        size_and_slots = parts[1]
        hub_info = parts[2]

        filesize_str, slots = size_and_slots.rsplit(" ", 1)
        free_slots, total_slots = slots.split("/", 1)

        return {
            "nick": nick,
            "filename": filename,
            "filesize": int(filesize_str),
            "free_slots": int(free_slots),
            "total_slots": int(total_slots),
            "hub_info": hub_info,
        }
    except (ValueError, IndexError):
        return None


class _NMDCConnection:
    """Envoltorio fino sobre un StreamReader/Writer para leer/escribir
    mensajes NMDC delimitados por '|'."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def send(self, message: str) -> None:
        self.writer.write(message.encode("utf-8", errors="replace") + b"|")
        await self.writer.drain()

    async def send_raw(self, prefix: str, raw_bytes: bytes) -> None:
        """Para mensajes que llevan bytes crudos no-ASCII (la $Key
        calculada por lock_to_key puede contener cualquier valor de
        byte 0-255; solo 6 valores concretos se escapan según el spec,
        el resto va tal cual, no como texto codificado)."""
        self.writer.write(prefix.encode("ascii") + raw_bytes + b"|")
        await self.writer.drain()

    async def read_message(self, timeout: float | None = None) -> str:
        data = await asyncio.wait_for(self.reader.readuntil(b"|"), timeout=timeout)
        return data[:-1].decode("utf-8", errors="replace")

    def close(self) -> None:
        self.writer.close()


class DCPPBackend(NetworkBackend):
    network = Network.DCPP

    def __init__(self, nickname: str, listen_port: int = DEFAULT_CLIENT_PORT,
                 shared_library: SharedLibrary | None = None,
                 proxy: ProxyConfig | None = None) -> None:
        """
        shared_library: si se pasa una `SharedLibrary` con carpeta
        configurada, este backend responde a las búsquedas ($Search)
        que reenvía el hub con $SR propios cuando hay coincidencia, y
        sirve el fichero cuando otro usuario nos pide $Get; si no se
        pasa (o su carpeta está vacía), el backend sigue siendo
        puramente "leecher" como hasta ahora.
        """
        self._nickname = nickname
        self._listen_port = listen_port
        self._shared_library = shared_library
        self._proxy = proxy
        self._active_uploads = 0
        self._hub: _NMDCConnection | None = None
        self._hub_task: asyncio.Task | None = None
        self._hub_host: str | None = None
        self._hub_port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._progress_callback: Callable[[Download], None] | None = None
        self._active: dict[str, dict] = {}
        self._pending_search_results: list[dict] = []
        self._collecting_search = False
        self._debug = False
        self._search_max_results: int | None = None
        self._search_limit_event: asyncio.Event | None = None
        # chat de hub (punto 14 del backlog): callback de eventos, ver
        # subscribe_chat()
        self._chat_callback: Callable[[dict], None] | None = None

    async def connect_to_hub(self, host: str, port: int = 411, password: str | None = None,
                              timeout: float = 15.0, debug: bool = False) -> None:
        reader, writer = await proxy_open_connection(host, port, proxy=self._proxy, timeout=timeout)
        conn = _NMDCConnection(reader, writer)

        deadline = time.monotonic() + timeout
        lock_msg = await conn.read_message(timeout=timeout)
        if debug:
            print(f"  [debug] hub -> {lock_msg!r}")
        if not lock_msg.startswith("$Lock "):
            conn.close()
            raise ConnectionError(f"Se esperaba $Lock del hub, llegó: {lock_msg!r}")

        lock_body = lock_msg[len("$Lock "):]
        lock_str = lock_body.split(" Pk=")[0]
        key = lock_to_key(lock_str.encode("utf-8"))

        await conn.send("$Supports NoHello NoGetINFO")
        await conn.send_raw("$Key ", key)
        await conn.send(f"$ValidateNick {self._nickname}")

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = await conn.read_message(timeout=remaining)
            if debug:
                preview = msg if len(msg) < 150 else msg[:150] + "..."
                print(f"  [debug] hub -> {preview!r}")
            if msg.startswith("$ValidateDenide"):
                conn.close()
                raise ConnectionError(f"Nick '{self._nickname}' rechazado por el hub (¿ya en uso?)")
            if msg == "$HubIsFull":
                conn.close()
                raise ConnectionError("El hub está lleno")
            if msg.startswith("$GetPass"):
                if password is None:
                    conn.close()
                    raise ConnectionError("Este hub requiere contraseña y no se ha proporcionado ninguna")
                await conn.send(f"$MyPass {password}")
                continue
            if msg == "$BadPass":
                conn.close()
                raise ConnectionError("Contraseña incorrecta para este hub")
            if msg.startswith(f"$Hello {self._nickname}"):
                break

        await conn.send(f"$Version {CLIENT_VERSION}")
        share_size = (
            sum(f.size for f in self._shared_library.list_files())
            if self._shared_library is not None else 0
        )
        my_info = (
            f"$MyINFO $ALL {self._nickname} P2P Total<++ V:0.1,M:A,H:1/0/0,S:1>"
            # Formato NMDC: $ $<conexión><flag_estado>$<email>$<tamaño_compartido>$
            # (el campo email va vacío, pero su "$" delimitador es obligatorio;
            # sin él el hub responde "MyINFO is corrupted").
            f"$ $LAN(T3)\x01$${share_size}$"
        )
        await conn.send(my_info)

        self._hub = conn
        self._hub_host = host
        self._hub_port = port
        self._hub_task = asyncio.create_task(self._hub_read_loop())
        self._debug = debug

    async def connect(self) -> None:
        if self._shared_library is not None:
            self._shared_library.rescan()
        # Escucha en IPv4 y en IPv6 a la vez: a diferencia de G2/eD2k/
        # Soulseek (protocolos binarios con un campo de dirección de solo
        # 4 bytes que no puede representar una IPv6), NMDC es texto plano
        # y el $ConnectToMe ya sabe anunciar una IPv6 entre corchetes
        # (ver _format_ctm_address/_split_ctm_address), así que un peer
        # con conectividad IPv6 nativa (sin NAT, cada vez más común) sí
        # puede llegar a este puerto directamente.
        try:
            self._server = await asyncio.start_server(
                self._handle_incoming_peer, ["0.0.0.0", "::"], self._listen_port
            )
        except OSError:
            # Entorno sin pila IPv6 disponible (contenedores/CI sin
            # soporte v6): nos quedamos solo con IPv4, igual que antes.
            self._server = await asyncio.start_server(
                self._handle_incoming_peer, "0.0.0.0", self._listen_port
            )
        # Best-effort: abre el puerto de escucha en el router si tiene
        # UPnP. Nunca bloquea ni falla el connect().
        asyncio.ensure_future(upnp.add_port_mapping(self._listen_port, "TCP", "P2P Total - DC++"))

    async def disconnect(self) -> None:
        if self._hub_task:
            self._hub_task.cancel()
            self._hub_task = None
        if self._hub:
            self._hub.close()
            self._hub = None
        if self._server:
            self._server.close()
            self._server = None
            asyncio.ensure_future(upnp.delete_port_mapping(self._listen_port, "TCP"))
        self._active.clear()

    async def is_connected(self) -> bool:
        return self._hub is not None

    async def search(self, query: str, timeout: float = 15.0, debug: bool = False,
                      max_results: int | None = None) -> list[SearchResult]:
        srs = await self._do_search(parse_search_string(query), timeout, debug, max_results)
        results = []
        for sr in srs:
            results.append(SearchResult(
                network=self.network,
                title=sr["filename"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                size_bytes=sr["filesize"],
                source_id=f"{sr['nick']}:::{sr['filename']}",
                seeds_or_sources=sr["free_slots"],
                extra={"nick": sr["nick"], "free_slots": sr["free_slots"], "total_slots": sr["total_slots"]},
            ))
        return results

    async def search_by_tth(self, tth: bytes, timeout: float = 15.0, debug: bool = False,
                             max_results: int | None = None) -> list[SearchResult]:
        """Búsqueda por hash exacto (tipo "9" del spec NMDC, sección
        4.4.23): a diferencia de la búsqueda por nombre, el hub solo
        devuelve ficheros cuyo TTH coincide exactamente con el que
        pedimos, así que podemos adjuntar el hash buscado a cada
        resultado sin necesidad de que venga en la propia respuesta
        $SR (que no lleva el hash, solo nombre/tamaño/slots) — es lo
        que permite verificar el contenido tras la descarga."""
        search_str = f"F?F?0?9?TTH:{tth_to_base32(tth)}"
        srs = await self._do_search(search_str, timeout, debug, max_results)
        results = []
        for sr in srs:
            results.append(SearchResult(
                network=self.network,
                title=sr["filename"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                size_bytes=sr["filesize"],
                source_id=f"{sr['nick']}:::{sr['filename']}",
                seeds_or_sources=sr["free_slots"],
                extra={
                    "nick": sr["nick"], "free_slots": sr["free_slots"],
                    "total_slots": sr["total_slots"], "tth": tth,
                },
            ))
        return results

    async def _do_search(self, search_str: str, timeout: float, debug: bool,
                          max_results: int | None) -> list[dict]:
        if self._hub is None:
            raise RuntimeError("Backend de DC++ no conectado a ningún hub")

        self._pending_search_results = []
        self._collecting_search = True
        self._debug = debug
        self._search_max_results = max_results
        # El hub no soporta cortar la búsqueda en el propio protocolo (no
        # hay un "ya basta" que enviarle): el corte anticipado es un Event
        # que _hub_read_loop dispara al llegar al límite, igual que en
        # Gnutella2/eMule.
        self._search_limit_event = asyncio.Event() if max_results else None
        search_cmd = f"$Search Hub:{self._nickname} {search_str}"
        if debug:
            print(f"  [debug] enviando: {search_cmd!r}")
        await self._hub.send(search_cmd)

        try:
            if self._search_limit_event is not None:
                try:
                    await asyncio.wait_for(self._search_limit_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(timeout)
        finally:
            self._collecting_search = False
            self._debug = False
            self._search_limit_event = None

        return self._pending_search_results

    async def start_download(self, result: SearchResult, dest_path: str) -> Download:
        if self._hub is None:
            raise RuntimeError("Backend de DC++ no conectado a ningún hub")

        nick, remote_path = result.source_id.split(":::", 1)

        download = Download(
            id=None,
            network=self.network,
            title=result.title,
            source_id=result.source_id,
            dest_path=dest_path,
            size_bytes=result.size_bytes,
            state=DownloadState.SEARCHING_SOURCES,
        )
        self._active[remote_path] = {
            "download": download,
            "nick": nick,
            "dest_path": dest_path,
            "paused": False,
            "cancelled": False,
            "writer": None,
            "limiter": RateLimiter(),
            "expected_tth": result.extra.get("tth"),
        }

        my_ip = await self._get_local_ip()
        my_addr = _format_ctm_address(my_ip, self._listen_port)
        await self._hub.send(f"$ConnectToMe {nick} {my_addr}")

        return download

    async def pause_download(self, download: Download) -> None:
        entry = self._find_entry(download)
        if entry is None:
            return
        entry["paused"] = True
        if entry["writer"] is not None:
            entry["writer"].close()
        entry["writer"] = None
        download.state = DownloadState.PAUSED
        download.speed_bps = 0.0
        self._notify(download)

    async def resume_download(self, download: Download) -> None:
        entry = self._find_entry(download)
        if entry is None or self._hub is None:
            return
        entry["paused"] = False
        entry["cancelled"] = False
        download.state = DownloadState.SEARCHING_SOURCES
        self._notify(download)
        # No mantenemos la conexión cliente-cliente abierta durante la
        # pausa (se cierra en pause_download), así que hay que pedirle al
        # hub que le diga al otro peer que vuelva a conectarnos; cuando
        # llegue, _handle_incoming_peer volverá a encontrar esta entry por
        # nick y pedirá el fichero con $Get desde downloaded_bytes.
        my_ip = await self._get_local_ip()
        my_addr = _format_ctm_address(my_ip, self._listen_port)
        await self._hub.send(f"$ConnectToMe {entry['nick']} {my_addr}")

    async def cancel_download(self, download: Download) -> None:
        for path, entry in list(self._active.items()):
            if entry["download"] is download or entry["download"].id == download.id:
                entry["cancelled"] = True
                if entry["writer"] is not None:
                    entry["writer"].close()
                self._active.pop(path, None)
        download.state = DownloadState.CANCELLED
        download.speed_bps = 0.0
        self._notify(download)

    def subscribe_progress(self, callback: Callable[[Download], None]) -> None:
        self._progress_callback = callback

    # ---- chat de hub (punto 14 del backlog) ----

    def supports_chat(self) -> bool:
        return True

    def subscribe_chat(self, callback: Callable[[dict], None]) -> None:
        """Registra un callback que recibe cada evento de chat según
        llega del hub, como dict con clave "type":
        - {"type": "room_message", "room", "user", "message"} (chat
          público del hub, ver get_room_list)
        - {"type": "private_message", "user", "message"}
        """
        self._chat_callback = callback

    def _notify_chat(self, event: dict) -> None:
        if self._chat_callback:
            self._chat_callback(event)

    async def get_room_list(self, timeout: float = 10.0) -> list[tuple[str, int]]:
        """NMDC no tiene salas independientes: todo el hub es un único
        canal de chat compartido por todos los que están conectados a
        él. Se expone como una "sala" sintética identificada por el
        propio host:puerto del hub (0 usuarios, dato que este backend
        no lleva la cuenta de) para que la GUI pueda reutilizar el
        mismo flujo "unirse a sala" que ya tiene para Soulseek sin
        necesitar un caso especial: unirse a esa única sala no manda
        nada al hub (ya estamos "dentro" por el mero hecho de estar
        conectados), simplemente abre la pestaña de chat."""
        if self._hub_host is None:
            return []
        return [(f"{self._hub_host}:{self._hub_port}", 0)]

    async def say_in_room(self, room: str, message: str) -> None:
        # El parámetro `room` se ignora: solo existe el chat del propio
        # hub (ver get_room_list). El hub reenvía la línea tal cual a
        # todos los conectados, incluido a nosotros mismos.
        if self._hub is None:
            raise RuntimeError("Backend de DC++ no conectado a ningún hub")
        await self._hub.send(f"<{self._nickname}> {escape_nmdc(message)}")

    async def send_private_message(self, username: str, message: str) -> None:
        if self._hub is None:
            raise RuntimeError("Backend de DC++ no conectado a ningún hub")
        await self._hub.send(
            f"$To: {username} From: {self._nickname} $<{self._nickname}> {escape_nmdc(message)}"
        )

    def get_stats(self) -> dict:
        if self._hub is None:
            return {}
        peer = self._hub.writer.get_extra_info("peername")
        stats = {
            "server": f"{peer[0]}:{peer[1]}" if peer else None,
            "nickname": self._nickname,
            "listen_port": self._listen_port,
            "active_transfers": len(self._active),
        }
        if self._shared_library is not None and self._shared_library.enabled:
            stats["shared_files"] = len(self._shared_library.list_files())
            stats["active_uploads"] = self._active_uploads
        return stats

    def set_download_limit(self, download: Download, rate_bps: int) -> None:
        entry = self._find_entry(download)
        if entry is not None:
            entry["limiter"].set_rate(rate_bps)

    def _find_entry(self, download: Download) -> dict | None:
        for entry in self._active.values():
            if entry["download"] is download or entry["download"].id == download.id:
                return entry
        return None

    def _notify(self, download: Download) -> None:
        if self._progress_callback:
            self._progress_callback(download)

    async def _get_local_ip(self) -> str:
        """Nuestra IP local para anunciarla en un $ConnectToMe saliente.
        Se prefiere la que ve la propia conexión TCP al hub (`sockname`
        del socket ya abierto): así, si el hub es alcanzable por IPv6,
        anunciamos automáticamente una IPv6 (misma familia con la que de
        verdad estamos routeando hacia el exterior), y si es IPv4 pues
        IPv4 — sin necesidad de adivinar qué familia usar. Solo si no hay
        hub conectado (no debería pasar, se llama tras conectar) se cae
        al truco clásico de abrir un socket UDP "de mentira" hacia un DNS
        público IPv4 para leer la IP local de la ruta por defecto."""
        if self._hub is not None:
            sockname = self._hub.writer.get_extra_info("sockname")
            if sockname:
                return sockname[0]
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()

    async def _hub_read_loop(self) -> None:
        try:
            while True:
                msg = await self._hub.read_message()
                if getattr(self, "_debug", False):
                    preview = msg if len(msg) < 150 else msg[:150] + "..."
                    print(f"  [debug] hub -> {preview!r}")
                if msg.startswith("$SR ") and self._collecting_search:
                    parsed = parse_sr(msg)
                    if parsed:
                        self._pending_search_results.append(parsed)
                        if (self._search_max_results is not None
                                and self._search_limit_event is not None
                                and len(self._pending_search_results) >= self._search_max_results):
                            self._search_limit_event.set()
                    elif getattr(self, "_debug", False):
                        print(f"  [debug] $SR recibido pero no se pudo parsear: {msg!r}")
                elif msg.startswith("$Search ") and self._shared_library is not None and self._shared_library.enabled:
                    self._handle_incoming_search(msg)
                elif msg.startswith("$ConnectToMe ") and self._shared_library is not None and self._shared_library.enabled:
                    # Alguien ha visto uno de nuestros $SR y quiere descargarlo:
                    # nos toca a NOSOTROS abrir la conexión hacia su ip:puerto
                    # (el $ConnectToMe que mandamos nosotros mismos para
                    # nuestras propias descargas nunca nos vuelve, así que
                    # cualquiera que nos llegue aquí es de este tipo).
                    self._handle_incoming_connect_to_me(msg[len("$ConnectToMe "):])
                elif msg.startswith("$To: "):
                    self._handle_incoming_private_message(msg[len("$To: "):])
                elif msg.startswith("<"):
                    self._handle_incoming_room_message(msg)
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError):
            pass

    def _handle_incoming_room_message(self, msg: str) -> None:
        """Chat público del hub: el propio hub reenvía a todos los
        conectados (incluido a quien lo escribió) la línea tal cual
        `<Nick> mensaje`, sin ningún envoltorio `$` — a diferencia de
        todos los demás mensajes NMDC del protocolo. Se ignora
        silenciosamente cualquier línea que no tenga esta forma exacta
        (nick vacío o sin "> " de separación) por si algún hub manda
        algo parecido que no sea chat de verdad."""
        nick, sep, message = msg[1:].partition("> ")
        if not sep or not nick:
            return
        room = f"{self._hub_host}:{self._hub_port}" if self._hub_host else ""
        self._notify_chat({"type": "room_message", "room": room, "user": nick, "message": message})

    def _handle_incoming_private_message(self, arg: str) -> None:
        """Formato de `$To:` (mensaje privado), sección de chat del
        spec NMDC: `$To: MiNick From: OtroNick $<OtroNick> mensaje`.
        El destinatario (antes de "From:") no hace falta comprobarlo:
        el propio hub ya se encarga de entregárnoslo solo a nosotros."""
        _to_nick, sep, rest = arg.partition(" From: ")
        if not sep:
            return
        from_nick, sep, rest = rest.partition(" $")
        if not sep:
            return
        _echo_nick, sep, message = rest.partition("> ")
        if not sep:
            return
        self._notify_chat({"type": "private_message", "user": from_nick, "message": message})

    def _handle_incoming_connect_to_me(self, arg: str) -> None:
        # El hub puede reenviar el mensaje tal cual lo mandó el otro
        # cliente (target_nick ip:puerto) o solo la parte ip:puerto,
        # según implementación del hub — se aceptan ambas formas.
        parts = arg.split(" ", 1)
        addr = parts[1] if len(parts) == 2 and ":" in parts[1] else parts[0]
        split = _split_ctm_address(addr)
        if split is None:
            return
        host, port_str = split
        if not host or not port_str.isdigit():
            return
        asyncio.create_task(self._dial_out_as_uploader(host, int(port_str)))

    async def _dial_out_as_uploader(self, host: str, port: int) -> None:
        try:
            reader, writer = await proxy_open_connection(host, port, proxy=self._proxy, timeout=15.0)
        except (OSError, asyncio.TimeoutError):
            return
        # Reutiliza el mismo manejador que usa el servidor de escucha:
        # habla primero ($MyNick/$Lock) y, al no encontrar ninguna
        # descarga propia activa para este nick, cae solo en modo
        # "Upload" (ver _handle_incoming_peer).
        await self._handle_incoming_peer(reader, writer)

    def _handle_incoming_search(self, msg: str) -> None:
        """El hub reenvía la búsqueda ($Search) de otro usuario a todos
        los clientes conectados; si tenemos algo que hace match en la
        carpeta compartida, contestamos con $SR — el propio hub se
        encarga de entregárselo solo a quien buscó, a partir del
        "Hub:nick" final. Solo se soporta el modo pasivo del que
        busca (target "Hub:nick"): el modo activo ("ip:port") exigiría
        responder por UDP directo, fuera del alcance de este MVP."""
        body = msg[len("$Search "):]
        try:
            target, criteria = body.split(" ", 1)
        except ValueError:
            return
        if not target.startswith("Hub:"):
            return
        requester_nick = target[len("Hub:"):]
        if requester_nick == self._nickname:
            return

        parts = criteria.split("?")
        if len(parts) != 5:
            return
        pattern = parts[4]
        words = [w.lower() for w in pattern.split("$") if w]
        if not words:
            return

        matches = [
            shared for shared in self._shared_library.list_files()
            if all(w in shared.rel_path.lower() for w in words)
        ][:5]  # los hubs NMDC suelen limitar a 5 resultados por búsqueda
        if not matches:
            return

        hub_desc = f"{self._hub_host}:{self._hub_port}" if self._hub_host else ""
        for shared in matches:
            filename = shared.rel_path.replace("/", "\\")
            sr = (
                f"$SR {self._nickname} {filename}\x05{shared.size} 1/1"
                f"\x05P2P Total ({hub_desc})Hub:{requester_nick}"
            )
            asyncio.ensure_future(self._hub.send(sr))

    async def _handle_incoming_peer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = _NMDCConnection(reader, writer)
        try:
            await conn.send(f"$MyNick {self._nickname}")
            lock = "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ", k=32))
            await conn.send(f"$Lock EXTENDEDPROTOCOL{lock} Pk=P2PTotal0.1")

            my_rand = random.randint(1, 32767)
            entry: dict | None = None
            remote_path_to_get: str | None = None
            # Si el nick de quien conecta no corresponde a ninguna
            # descarga nuestra en curso, es que ha llegado hasta
            # nosotros por su cuenta (típicamente tras un $ConnectToMe
            # suyo, después de vernos en un $SR) para descargar algo de
            # NUESTRA carpeta compartida: pasamos a modo "Upload".
            upload_mode = False

            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                msg = await conn.read_message(timeout=deadline - time.monotonic())
                if msg.startswith("$MyNick "):
                    their_nick = msg[len("$MyNick "):]
                    # ¿a qué descarga pendiente corresponde este nick? (una
                    # entry sigue en self._active mientras esté pausada, así
                    # que esto también encuentra la reconexión de un resume)
                    for path, candidate in self._active.items():
                        if candidate["nick"] == their_nick:
                            remote_path_to_get = path
                            entry = candidate
                            break
                    if entry is None and self._shared_library is not None and self._shared_library.enabled:
                        upload_mode = True
                elif msg.startswith("$Lock "):
                    their_lock = msg[len("$Lock "):].split(" Pk=")[0]
                    their_key = lock_to_key(their_lock.encode("utf-8"))
                    direction = "Upload" if upload_mode else "Download"
                    await conn.send(f"$Direction {direction} {my_rand}")
                    await conn.send_raw("$Key ", their_key)
                    if not upload_mode and remote_path_to_get and entry is not None:
                        entry["writer"] = writer
                        # NMDC usa offset de base 1: pedir desde el byte que
                        # ya tenemos descargado (0 la primera vez) es
                        # downloaded_bytes + 1, no un "$1" fijo — así es como
                        # se reanuda una descarga a medias.
                        offset = entry["download"].downloaded_bytes + 1
                        await conn.send(f"$Get {remote_path_to_get}${offset}")
                elif msg.startswith("$Get ") and upload_mode:
                    await self._handle_get(conn, msg[len("$Get "):])
                    break
                elif msg.startswith("$FileLength "):
                    size = int(msg[len("$FileLength "):])
                    await self._receive_file(conn, size, entry)
                    break
                elif msg.startswith("$Error") or msg.startswith("$MaxedOut"):
                    if entry is not None:
                        d = entry["download"]
                        d.state = DownloadState.ERROR
                        d.error_message = msg
                        d.speed_bps = 0.0
                        self._notify(d)
                    break
        finally:
            conn.close()

    async def _handle_get(self, conn: "_NMDCConnection", get_arg: str) -> None:
        """El otro extremo, ya en modo "Upload" para nosotros, nos pide
        un fichero con `$Get archivo$offset` (offset en base 1). Le
        respondemos `$FileLength` y, si confirma con `$Send`, le
        mandamos los bytes desde ese offset — así puede reanudar una
        descarga a medias sin volver a empezar."""
        try:
            filename, offset_str = get_arg.rsplit("$", 1)
            offset = int(offset_str) - 1
        except ValueError:
            await conn.send("$Error Malformed request")
            return

        shared = self._shared_library.find_by_native_path(filename) if self._shared_library else None
        if shared is None or offset < 0:
            await conn.send("$Error File Not Available")
            return

        await conn.send(f"$FileLength {shared.size}")
        msg = await conn.read_message(timeout=20.0)
        if not msg.startswith("$Send"):
            return

        self._active_uploads += 1
        try:
            with open(shared.path, "rb") as f:
                f.seek(offset)
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    await global_upload_limiter.consume(len(chunk))
                    conn.writer.write(chunk)
                    await conn.writer.drain()
                    stats_tracker.record_uploaded(self.network, len(chunk))
        except (ConnectionError, OSError):
            pass
        finally:
            self._active_uploads -= 1

    async def _receive_file(self, conn: _NMDCConnection, size: int, entry: dict | None) -> None:
        """Recibe el fichero escribiendo a disco de forma incremental (no
        se acumula en memoria), actualizando downloaded_bytes/speed_bps
        chunk a chunk y comprobando paused/cancelled para poder cortar la
        transferencia en marcha desde pause_download()/cancel_download()."""
        await conn.send("$Send")

        if entry is None:
            # No sabemos a qué descarga pertenece esta conexión (no debería
            # pasar, pero por seguridad consumimos y descartamos el flujo en
            # vez de dejar la conexión colgada a medio protocolo).
            remaining = size
            while remaining > 0:
                chunk = await conn.reader.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            return

        download = entry["download"]
        os.makedirs(entry["dest_path"], exist_ok=True)
        out_path = os.path.join(entry["dest_path"], download.title)
        resumable = download.downloaded_bytes > 0 and os.path.exists(out_path)
        pos = download.downloaded_bytes if resumable else 0
        download.downloaded_bytes = pos
        download.state = DownloadState.DOWNLOADING
        speed_window_start = time.monotonic()
        speed_window_bytes = pos

        try:
            with open(out_path, "r+b" if resumable else "wb") as f:
                f.seek(pos)
                remaining = size
                while remaining > 0:
                    if entry["paused"] or entry["cancelled"]:
                        return  # pause_download/cancel_download ya dejaron el estado como corresponde
                    chunk = await conn.reader.read(min(65536, remaining))
                    if not chunk:
                        break
                    await global_download_limiter.consume(len(chunk))
                    await entry["limiter"].consume(len(chunk))
                    f.write(chunk)
                    pos += len(chunk)
                    remaining -= len(chunk)
                    download.downloaded_bytes = pos

                    now = time.monotonic()
                    elapsed = now - speed_window_start
                    if elapsed >= 0.5:
                        download.speed_bps = (pos - speed_window_bytes) / elapsed
                        speed_window_start = now
                        speed_window_bytes = pos
                    self._notify(download)
        except (ConnectionError, OSError):
            pass

        download.speed_bps = 0.0
        if entry["paused"] or entry["cancelled"]:
            self._notify(download)
            return

        if pos != download.size_bytes:
            download.state = DownloadState.ERROR
            download.error_message = "Transferencia incompleta (conexión cerrada antes de tiempo)"
            self._notify(download)
            return

        expected_tth = entry.get("expected_tth")
        if expected_tth is not None:
            # Solo las descargas que vinieron de search_by_tth() (búsqueda
            # por hash exacto) traen un TTH esperado que verificar: una
            # búsqueda normal por nombre no tiene forma de conocer de
            # antemano el hash del fichero (el $SR no lo lleva), así que
            # esas descargas se quedan sin verificar (limitación real y
            # documentada, no un descuido).
            got_tth = await asyncio.to_thread(tth_of_file, out_path)
            if got_tth != expected_tth:
                download.state = DownloadState.ERROR
                download.error_message = "Verificación TTH fallida (fichero corrupto o incompleto)"
                self._notify(download)
                return

        download.state = DownloadState.COMPLETED
        self._notify(download)


# --------------------------------------------------------------------------
# Lista pública de hubs (punto 11 del backlog): agregador tipo hublist.org
# para poder elegir hub desde la GUI en vez de teclear IP:puerto a mano.
#
# Formato real comprobado descargando en vivo
# http://dchublist.com/hublist.xml (agregador "Team Elite Hublist",
# redirige a te-home.net): XML plano, un elemento <Hub .../> por hub con
# atributos Name/Address/Description/Country/Users/Shared/Software entre
# otros. El atributo Address es "host" o "host:puerto" (sin puerto
# implica el 411 por defecto del protocolo NMDC).
# --------------------------------------------------------------------------

HUB_LIST_URL = "http://dchublist.com/hublist.xml"


def parse_dchub_link(link: str) -> tuple[str, int] | None:
    """Parsea un enlace `dchub://host:puerto` (o `dchub://host`, sin
    puerto -> 411 por defecto), el esquema de URI estándar que usan
    los clientes DC++ reales para enlazar un hub directamente (punto
    12 del backlog: pegar el enlace debe conectar sin tener que
    guardarlo antes como hub por defecto en Preferencias).

    Se apoya en `urlparse` (en vez de partir por ":" a mano) para
    soportar también hubs con dirección IPv6 literal en notación con
    corchetes (`dchub://[2001:db8::1]:411`, RFC 3986 igual que
    `http://`): partir por el primer/último ":" rompería con una
    IPv6 sin corchetes, y `urlparse` ya sabe manejar esto bien."""
    link = link.strip()
    if not link.lower().startswith("dchub://"):
        return None
    address = link[len("dchub://"):].rstrip("/")
    if not address:
        return None
    parsed = urlparse(f"dchub://{address}")
    host = parsed.hostname
    if not host:
        return None
    try:
        return host, parsed.port or 411
    except ValueError:
        return None


@dataclass
class HubListEntry:
    name: str
    host: str
    port: int
    description: str
    country: str
    users: int
    shared_bytes: int
    software: str


def parse_hub_list(xml_text: str) -> list[HubListEntry]:
    """Parsea el XML de un agregador tipo hublist.org (ver `HUB_LIST_URL`).
    Ignora hubs sin `Address` (no debería darse, pero por robustez)."""
    root = ElementTree.fromstring(xml_text)
    entries = []
    for hub_el in root.iter("Hub"):
        address = hub_el.get("Address", "").strip()
        if not address:
            continue
        host, _, port_str = address.partition(":")
        try:
            port = int(port_str) if port_str else 411
        except ValueError:
            port = 411
        try:
            users = int(hub_el.get("Users", "0") or "0")
        except ValueError:
            users = 0
        try:
            shared_bytes = int(hub_el.get("Shared", "0") or "0")
        except ValueError:
            shared_bytes = 0
        entries.append(HubListEntry(
            name=hub_el.get("Name", host),
            host=host,
            port=port,
            description=hub_el.get("Description", ""),
            country=hub_el.get("Country", ""),
            users=users,
            shared_bytes=shared_bytes,
            software=hub_el.get("Software", ""),
        ))
    return entries


async def fetch_public_hub_list(timeout: float = 20.0, proxy: ProxyConfig | None = None) -> list[HubListEntry]:
    body = await http_get(HUB_LIST_URL, timeout=timeout, proxy=proxy)
    return parse_hub_list(body.decode("utf-8", errors="replace"))
