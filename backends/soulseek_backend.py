"""
Backend Soulseek implementando el protocolo "SoulseekNS" directamente
sobre sockets asyncio, sin librerías externas ni procesos aparte.
Sustituye una versión anterior basada en la librería `aioslsk`, que
dio problemas de estabilidad (fallos al unirse a la red distribuida,
descargas que nunca arrancaban aunque el peer sí tuviera puerto
abierto...).

Referencia usada para implementar esto: el código fuente de
Nicotine+ (https://github.com/nicotine-plus/nicotine-plus), en
particular `pynicotine/slskmessages.py` (formato exacto de cada
mensaje) y `pynicotine/slskproto.py` (máquina de estados de las
conexiones: login, búsqueda y establecimiento de conexiones
peer-a-peer).

Conceptos clave del protocolo:
- Hay un **servidor central** (server.slsknet.org): hace falta login
  (usuario/contraseña; si el usuario no existe, el propio servidor lo
  registra en el primer login).
- Las búsquedas se envían al servidor con un token numérico; el
  servidor las reenvía a otros clientes, y sus respuestas
  (FileSearchResponse) nos llegan más tarde y de forma asíncrona por
  conexiones peer-a-peer nuevas que esos clientes abren hacia
  nosotros.
- El framing de mensajes tiene DOS formatos distintos según el tipo
  de conexión/momento:
    * Servidor y conexiones "P" (peer) ya establecidas:
      uint32(longitud) + uint32(código) + payload.
    * El PRIMER mensaje de cualquier conexión peer-a-peer recién
      abierta (antes de saber si será tipo "P" o "F"):
      uint32(longitud) + uint8(código) + payload — son solo dos
      mensajes posibles aquí: PeerInit (código 1, quien abre la
      conexión activamente) o PierceFireWall (código 0, respuesta a
      una petición de conexión indirecta).
    * Las conexiones tipo "F" (transferencia de fichero), una vez
      superado ese primer mensaje, no llevan NINGÚN framing más:
      el que sube el fichero manda un uint32 crudo (token) seguido
      de un uint64 crudo (offset, mandado por nosotros) y a partir
      de ahí ya son los bytes del fichero sin envolver.
- Para conectar con un peer (para mandarle QueueUpload, o para que
  él nos mande el fichero) se usa una **estrategia dual**: se intenta
  una conexión directa a la última dirección conocida del peer Y, a
  la vez, se le pide al servidor una conexión indirecta (ConnectToPeer)
  para que sea el peer quien nos marque a nosotros. Se usa la que
  llegue antes. Esto es lo que permite que una descarga funcione
  aunque solo UNO de los dos extremos tenga el puerto abierto — pero
  si NINGUNO de los dos lo tiene, no hay forma de establecer la
  conexión de fichero (esto es una limitación del protocolo en sí,
  no de esta implementación).
"""

import asyncio
import hashlib
import os
import random
import struct
import time
import zlib
from typing import Callable

from core import upnp
from core.backend_base import NetworkBackend
from core.ip_filter import ip_filter, peer_ip_from_writer as _peer_ip_from_writer
from core.models import Download, DownloadState, Network, SearchResult
from core.proxy import ProxyConfig
from core.proxy import open_connection as proxy_open_connection
from core.rate_limiter import RateLimiter, global_download_limiter, global_upload_limiter
from core.sharing import SharedFile, SharedLibrary
from core.stats_tracker import stats_tracker

SERVER_HOST = "server.slsknet.org"
SERVER_PORT = 2242

# Puerto "de libro" que usan también los clientes oficiales y Nicotine+.
DEFAULT_LISTEN_PORT = 2234

# Cuántas fuentes distintas de un mismo fichero se prueban en paralelo como
# máximo. Un resultado fusionado puede traer cientos de fuentes (archivos
# muy compartidos); abrir una conexión simultánea a cada una sería
# excesivo tanto para nuestra propia conexión como, sobre todo, para esos
# peers ajenos — con unas pocas en paralelo ya alcanza para no depender de
# que la primera fuente probada esté disponible.
MAX_RACE_SOURCES = 8

# Nicotine+ reserva la versión mayor 160 para sí mismo; la reutilizamos
# tal cual porque el servidor solo la usa para diferenciar clientes,
# no exige un valor concreto para aceptar el login.
CLIENT_VERSION = 160
CLIENT_MINOR_VERSION = 3

# Nuestro SearchResult solo tiene un source_id (string). Aquí codificamos
# usuario + ruta remota en un único string para no tener que cambiar el
# modelo común solo por esta red.
_SEPARATOR = ":::"


def _encode_source_id(username: str, remote_path: str) -> str:
    return f"{username}{_SEPARATOR}{remote_path}"


def _decode_source_id(source_id: str) -> tuple[str, str]:
    username, remote_path = source_id.split(_SEPARATOR, 1)
    return username, remote_path


def _parse_shared_file_list(username: str, payload: bytes) -> list[tuple[str, list[SearchResult]]]:
    """Estructura de `SharedFileListResponse` (código 5), confirmada
    contra el propio código fuente de Nicotine+: zlib + uint32(ndir),
    y por cada carpeta un string (con "/" en vez de "\\") y su lista de
    ficheros con el mismo formato que un `FileSearchResponse`."""
    data = zlib.decompress(payload)
    up = _Unpacker(data)
    nfolders = up.uint32()
    folders: list[tuple[str, list[SearchResult]]] = []
    for _ in range(nfolders):
        folder = up.string().replace("/", "\\")
        nfiles = up.uint32()
        results = []
        for _ in range(nfiles):
            up.uint8()  # code, sin uso
            filename = up.string()
            size = up.file_size()
            ext_len = up.uint32()  # campo "ext", obsoleto
            up.skip(ext_len)
            numattr = up.uint32()
            up.skip(numattr * 8)  # cada atributo son 2 uint32 (num, valor)
            remote_path = f"{folder}\\{filename}"
            results.append(SearchResult(
                network=Network.SOULSEEK,
                title=filename,
                size_bytes=size,
                source_id=_encode_source_id(username, remote_path),
                seeds_or_sources=1,
                extra={"username": username},
            ))
        folders.append((folder, results))
    return folders


def _parse_room_list(up: "_Unpacker") -> list[tuple[str, int]]:
    """Formato de `RoomList` (código 64), confirmado contra Nicotine+:
    uint32(nsalas) + nombres, luego uint32(nsalas) + nº de usuarios de
    cada una (mismo orden que los nombres). El servidor manda a
    continuación las mismas dos listas para salas propias/con
    membresía y una lista de operadores, que aquí se ignoran: este
    backend solo necesita el listado público para poder unirse."""
    numrooms = up.uint32()
    names = [up.string() for _ in range(numrooms)]
    numusers = up.uint32()
    return [(names[i], up.uint32()) for i in range(numusers)]


def _parse_room_usernames(up: "_Unpacker") -> list[str]:
    """Formato de la lista de usuarios que trae la confirmación de
    `JoinRoom` (código 14), confirmado contra Nicotine+: no es un
    registro por usuario, sino varias listas paralelas (nombres,
    luego estados, luego estadísticas, luego slots, luego países),
    cada una con su propio contador delante. Solo nos interesan los
    nombres; el resto se descarta leyéndolo igualmente para dejar el
    cursor del payload en el sitio correcto."""
    numusers = up.uint32()
    usernames = [up.string() for _ in range(numusers)]
    statuslen = up.uint32()
    for _ in range(statuslen):
        up.uint32()
    statslen = up.uint32()
    for _ in range(statslen):
        up.uint32()  # avgspeed
        up.uint32()  # uploadnum
        up.uint32()  # unknown
        up.uint32()  # files
        up.uint32()  # dirs
    slotslen = up.uint32()
    for _ in range(slotslen):
        up.uint32()
    countrylen = up.uint32()
    for _ in range(countrylen):
        up.string()
    return usernames


# ---- códigos de mensaje (subconjunto necesario para conectar, buscar y descargar) ----

_SRV_LOGIN = 1
_SRV_SET_WAIT_PORT = 2
_SRV_GET_PEER_ADDRESS = 3
_SRV_SAY_CHATROOM = 13
_SRV_JOIN_ROOM = 14
_SRV_LEAVE_ROOM = 15
_SRV_USER_JOINED_ROOM = 16
_SRV_USER_LEFT_ROOM = 17
_SRV_CONNECT_TO_PEER = 18
_SRV_MESSAGE_USER = 22
_SRV_MESSAGE_ACKED = 23
_SRV_FILE_SEARCH = 26
_SRV_ROOM_LIST = 64
_SRV_CANT_CONNECT_TO_PEER = 1001

_INIT_PIERCE_FIREWALL = 0
_INIT_PEER_INIT = 1

_PEER_SHARED_FILE_LIST_REQUEST = 4
_PEER_SHARED_FILE_LIST_RESPONSE = 5
_PEER_FILE_SEARCH_RESPONSE = 9
_PEER_TRANSFER_REQUEST = 40
_PEER_TRANSFER_RESPONSE = 41
_PEER_QUEUE_UPLOAD = 43
_PEER_PLACE_IN_QUEUE_RESPONSE = 44
_PEER_UPLOAD_DENIED = 50

_TRANSFER_DIRECTION_UPLOAD = 1


# ---- empaquetado binario ----

def pack_uint8(n: int) -> bytes:
    return struct.pack("<B", n)


def pack_uint32(n: int) -> bytes:
    return struct.pack("<I", n)


def pack_uint64(n: int) -> bytes:
    return struct.pack("<Q", n)


def pack_bool(b: bool) -> bytes:
    return struct.pack("<B", 1 if b else 0)


def pack_string(s: str) -> bytes:
    encoded = s.encode("utf-8", errors="replace")
    return pack_uint32(len(encoded)) + encoded


class _Unpacker:
    """Va consumiendo secuencialmente un payload ya separado del
    framing, igual que hace Nicotine+ con su cursor `_offset`."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def uint8(self) -> int:
        v = self._data[self._offset]
        self._offset += 1
        return v

    def uint32(self) -> int:
        v = struct.unpack_from("<I", self._data, self._offset)[0]
        self._offset += 4
        return v

    def uint64(self) -> int:
        v = struct.unpack_from("<Q", self._data, self._offset)[0]
        self._offset += 8
        return v

    def boolean(self) -> bool:
        return self.uint8() != 0

    def string(self) -> str:
        length = self.uint32()
        raw = self._data[self._offset:self._offset + length]
        self._offset += length
        return raw.decode("utf-8", errors="replace")

    def ip(self) -> str:
        """4 bytes fijos: el protocolo Soulseek real (SoulseekQt,
        Nicotine+, el propio servidor slsknet.org) no tiene ninguna
        variante de campo de dirección para IPv6 — a día de hoy ningún
        cliente de la red lo soporta. No es una limitación de esta
        implementación: es del protocolo en sí, y no se puede añadir
        sin dejar de ser compatible con la red Soulseek real."""
        raw = self._data[self._offset:self._offset + 4]
        self._offset += 4
        return ".".join(str(b) for b in reversed(raw))

    def file_size(self) -> int:
        # Los tamaños de fichero en FileSearchResponse van en 8 bytes,
        # pero hay un bug histórico de Soulseek NS: para ficheros >2GiB
        # el byte más significativo vale 0xFF y el resto es basura: solo
        # los primeros 4 bytes (uint32) son el tamaño real.
        raw = self._data[self._offset:self._offset + 8]
        self._offset += 8
        if raw[7] == 0xFF:
            return struct.unpack_from("<I", raw, 0)[0]
        return struct.unpack("<Q", raw)[0]

    def skip(self, n: int) -> None:
        self._offset += n


async def _send_init(writer: asyncio.StreamWriter, code: int, payload: bytes) -> None:
    body = pack_uint8(code) + payload
    writer.write(pack_uint32(len(body)) + body)
    await writer.drain()


async def _read_init(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(4)
    length = struct.unpack("<I", header)[0]
    body = await reader.readexactly(length)
    return body[0], bytes(body[1:])


class _FramedConnection:
    """Conexión con framing uint32(longitud) + uint32(código) + payload:
    la usan tanto el servidor como las conexiones "P" (peer) ya
    establecidas."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def send(self, code: int, payload: bytes = b"") -> None:
        body = pack_uint32(code) + payload
        self.writer.write(pack_uint32(len(body)) + body)
        await self.writer.drain()

    async def read_message(self) -> tuple[int, bytes]:
        header = await self.reader.readexactly(4)
        length = struct.unpack("<I", header)[0]
        body = await self.reader.readexactly(length)
        code = struct.unpack_from("<I", body, 0)[0]
        return code, bytes(body[4:])

    def close(self) -> None:
        self.writer.close()


class _PeerConn(_FramedConnection):
    """Igual que _FramedConnection, pero además recuerda con qué
    usuario habla (para poder identificar quién nos manda qué)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, username: str) -> None:
        super().__init__(reader, writer)
        self.username = username


class SoulseekBackend(NetworkBackend):
    network = Network.SOULSEEK

    def __init__(self, username: str, password: str, download_dir: str = "/tmp/soulseek-downloads",
                 listen_port: int = DEFAULT_LISTEN_PORT,
                 shared_library: SharedLibrary | None = None,
                 proxy: ProxyConfig | None = None) -> None:
        """
        username/password: credenciales de Soulseek. Si el usuario no
        existe todavía en la red, el servidor lo registra automáticamente
        con esta contraseña en el primer login.

        shared_library: si se pasa una `SharedLibrary` con carpeta
        configurada, este backend responde a las peticiones de subida
        (`QueueUpload`) de otros peers sirviendo los ficheros que
        contenga; si no se pasa (o su carpeta está vacía), el backend
        se comporta como hasta ahora, solo "leecher".
        """
        self._username = username
        self._password = password
        self._download_dir = download_dir
        self._listen_port = listen_port
        self._shared_library = shared_library
        self._proxy = proxy
        self._active_uploads = 0
        # IP externa tal como la ve el propio servidor de Soulseek, leída
        # de la respuesta de login (ver connect()) — útil para detectar
        # NAT/redirección de puerto desde la pestaña Red sin depender de
        # un servicio externo de "cuál es mi IP".
        self._external_ip: str | None = None

        self._server: _FramedConnection | None = None
        self._server_task: asyncio.Task | None = None
        self._listen_server: asyncio.AbstractServer | None = None
        self._progress_callback: Callable[[Download], None] | None = None
        # callback para eventos de chat (salas y mensajes privados, punto
        # 13 del backlog): recibe un dict con clave "type" (ver
        # subscribe_chat() para el detalle de cada tipo de evento)
        self._chat_callback: Callable[[dict], None] | None = None
        self._room_list_future: asyncio.Future | None = None

        # id interno de descarga -> {"download", "username", "remote_path",
        #                 "candidates" (lista de (username, remote_path) que
        #                 se prueban en paralelo), "dest_path",
        #                 "conns" (conexiones "P" abiertas mientras se
        #                 compite por una fuente), "file_conn" (conexión
        #                 "F" de la fuente ganadora), "winner", "token"
        #                 (de la transferencia), "size", "paused",
        #                 "cancelled"}. Se indexa por id numérico propio en
        #                 vez de por remote_path porque varias fuentes
        #                 (una por candidato) pueden compartir el mismo
        #                 remote_path o el mismo username entre sí.
        self._active: dict[int, dict] = {}
        self._next_entry_id = 1

        # token de búsqueda -> lista de (username, filename, size) acumulados
        self._search_results: dict[int, list[tuple[str, str, int]]] = {}
        # token de búsqueda -> callback a invocar con cada resultado en
        # crudo nada más llegar (búsquedas multi-palabra, ver search())
        self._search_callbacks: dict[int, Callable[[tuple], None]] = {}

        # Tope de conexiones entrantes (puerto de escucha) gestionándose a
        # la vez, desde que se aceptan hasta que se cierran del todo. Una
        # búsqueda por una palabra popular ("jackson") hace que miles de
        # peers de la red distribuida abran conexión entrante casi al mismo
        # tiempo para mandarnos su resultado; aceptarlas todas agota los
        # file descriptors del proceso (visto en producción:
        # `OSError: [Errno 24] Demasiados ficheros abiertos` en el propio
        # accept() del listener). Las que llegan por encima del tope se
        # cierran al instante en vez de aceptarse. Complementa al backlog
        # bajo del listen_server (ver connect()): el backlog frena la
        # ráfaga a nivel de kernel antes de aceptar, este tope frena lo que
        # sí se llega a aceptar pero tarda en procesarse/cerrarse.
        self._incoming_gate_limit = 100
        self._incoming_gate_count = 0

        self._pending_address: dict[str, asyncio.Future] = {}
        # token de ConnectToPeer -> (conn_type, future) para las conexiones
        # indirectas que hemos pedido nosotros
        self._pending_indirect: dict[int, tuple[str, asyncio.Future]] = {}
        # token de transferencia -> id interno de descarga, para poder
        # identificar a qué descarga corresponde una conexión "F" entrante
        self._transfers_by_token: dict[int, int] = {}

        # Empieza en un valor aleatorio (no en 1) para no colisionar con
        # resultados que lleguen tarde (la búsqueda distribuida de Soulseek
        # puede tardar minutos en propagarse) de una sesión anterior bajo
        # la misma cuenta: si dos sesiones consecutivas usaran ambas el
        # token 1 para su primera búsqueda, un resultado tardío de la
        # sesión vieja se colaría en los resultados de la búsqueda nueva.
        self._next_token = random.randint(1, 2**31 - 1)

    # ---- ciclo de vida ----

    async def connect(self) -> None:
        if self._server is not None:
            return

        if self._shared_library is not None:
            # need_sha1=False, need_ed2k=False: Soulseek busca un fichero
            # compartido por ruta/nombre (find_by_native_path), no por
            # hash, así que no hace falta pagar el coste de hashear todo
            # el contenido (ver SharedLibrary.rescan). En segundo plano
            # sin esperar (ver SharedLibrary.ensure_scanning): aunque ya
            # no hashea, sigue siendo un os.walk() síncrono sobre la
            # carpeta compartida, y así connect() no se queda esperando
            # ni un instante a que termine de indexarla.
            self._shared_library.ensure_scanning(need_sha1=False, need_ed2k=False)

        reader, writer = await proxy_open_connection(SERVER_HOST, SERVER_PORT, proxy=self._proxy, timeout=15.0)
        conn = _FramedConnection(reader, writer)

        payload = pack_string(self._username) + pack_string(self._password) + pack_uint32(CLIENT_VERSION)
        md5hash = hashlib.md5((self._username + self._password).encode("utf-8")).hexdigest()
        payload += pack_string(md5hash) + pack_uint32(CLIENT_MINOR_VERSION)
        await conn.send(_SRV_LOGIN, payload)

        code, resp = await asyncio.wait_for(conn.read_message(), timeout=15.0)
        if code != _SRV_LOGIN:
            conn.close()
            raise ConnectionError(f"Respuesta inesperada del servidor durante el login (código {code})")

        up = _Unpacker(resp)
        success = up.boolean()
        if not success:
            reason = up.string()
            conn.close()
            raise ConnectionError(f"Login de Soulseek rechazado: {reason}")

        # Tras el booleano de éxito, el servidor manda un mensaje de
        # bienvenida (MOTD, que no usamos) y luego nuestra propia IP
        # externa tal como la ve él -el campo real del protocolo, no una
        # invención-. Se descarta en silencio si el formato no encaja (p.
        # ej. una versión de servidor que cambiara el mensaje): no es
        # crítico para el login, solo un dato informativo de más.
        try:
            up.string()
            external_ip = up.ip()
            if external_ip.count(".") == 3:
                self._external_ip = external_ip
        except (struct.error, IndexError):
            pass

        self._listen_server = await asyncio.start_server(
            self._handle_incoming_connection, "0.0.0.0", self._listen_port,
            # Backlog bajo a propósito (el por defecto de asyncio es 100):
            # una búsqueda de un término muy popular ("Michael Jackson") hace
            # que decenas/cientos de miles de peers abran conexión entrante
            # casi a la vez, y asyncio drena hasta backlog+1 conexiones de
            # golpe por cada aviso de socket listo (ver
            # selector_events._accept_connection). Un backlog pequeño hace
            # que el propio kernel rechace de entrada las conexiones que
            # sobran de la ráfaga (con RST, sin gastar un file descriptor
            # nuestro) en vez de que las aceptemos todas y las cerremos
            # nosotros a posteriori, que es lo que agotaba los file
            # descriptors del proceso.
            backlog=64,
        )

        self._server = conn
        await self._server.send(_SRV_SET_WAIT_PORT, pack_uint32(self._listen_port))
        self._server_task = asyncio.create_task(self._server_read_loop())

        # Best-effort: si el router tiene UPnP, abre el puerto de escucha
        # automáticamente para que los peers puedan conectar directamente
        # (si no, sigue funcionando igual vía la conexión indirecta a
        # través del servidor). Nunca bloquea ni falla el connect().
        asyncio.ensure_future(upnp.add_port_mapping(self._listen_port, "TCP", "P2P Total - Soulseek"))

    async def disconnect(self) -> None:
        if self._server_task:
            self._server_task.cancel()
            self._server_task = None
        if self._server:
            self._server.close()
            self._server = None
        if self._listen_server:
            self._listen_server.close()
            self._listen_server = None
            asyncio.ensure_future(upnp.delete_port_mapping(self._listen_port, "TCP"))
        self._active.clear()

    async def is_connected(self) -> bool:
        return self._server is not None

    # ---- búsqueda ----

    async def search(self, query: str, timeout: float = 15.0,
                      on_result: Callable[[SearchResult], None] | None = None,
                      max_results: int | None = None) -> list[SearchResult]:
        if self._server is None:
            raise RuntimeError("Backend de Soulseek no conectado")

        # Una query de dos o más palabras tiene muchas menos probabilidades
        # de llegar a un peer que la comparta que cada palabra suelta por
        # separado: la búsqueda distribuida de Soulseek solo alcanza a una
        # muestra aleatoria de usuarios conectados en cada intento, y una
        # palabra común como "jackson" la reciben y comprueban muchísimos
        # más peers que la frase completa "michael jackson". Así que en vez
        # de mandar la frase tal cual, mandamos una búsqueda por cada
        # palabra y nos quedamos solo con los resultados cuya ruta completa
        # contenga TODAS las palabras — recupera prácticamente los mismos
        # resultados que buscaría un cliente real, pero con muchas más
        # probabilidades de encontrarlos.
        words = query.split()
        search_terms = words if len(words) >= 2 else [query]
        match_words = [w.lower() for w in words] if len(words) >= 2 else None

        seen: set[tuple[str, str]] = set()
        collected: list[SearchResult] = []
        limit_reached = asyncio.Event()

        def handle_entry(entry: tuple[str, str, int, bool]) -> None:
            if max_results is not None and len(collected) >= max_results:
                return
            username, filename, size, has_free_slots = entry
            if match_words is not None:
                haystack = filename.lower()
                if not all(w in haystack for w in match_words):
                    return
            key = (username, filename)
            if key in seen:
                return
            seen.add(key)
            result = SearchResult(
                network=self.network,
                title=filename.rsplit("\\", 1)[-1],  # Soulseek usa rutas estilo Windows
                size_bytes=size,
                source_id=_encode_source_id(username, filename),
                seeds_or_sources=1,
                extra={"username": username, "has_free_slots": has_free_slots},
            )
            collected.append(result)
            if on_result is not None:
                on_result(result)
            if max_results is not None and len(collected) >= max_results:
                limit_reached.set()

        tokens = []
        for term in search_terms:
            token = self._next_token
            self._next_token += 1
            self._search_results[token] = []
            self._search_callbacks[token] = handle_entry
            tokens.append(token)
            await self._server.send(_SRV_FILE_SEARCH, pack_uint32(token) + pack_string(term))

        if max_results is not None:
            try:
                await asyncio.wait_for(limit_reached.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(timeout)

        for token in tokens:
            self._search_results.pop(token, None)
            self._search_callbacks.pop(token, None)

        return collected

    # ---- descarga ----

    async def start_download(self, result: SearchResult, dest_path: str) -> Download:
        if self._server is None:
            raise RuntimeError("Backend de Soulseek no conectado")

        # Un resultado fusionado (misma búsqueda, mismo fichero, varias
        # fuentes) trae source_id + alt_source_ids: se prueban todas en
        # paralelo (hasta MAX_RACE_SOURCES) y se descarga de la primera
        # que empiece a transferir datos de verdad — el usuario solo ve
        # una descarga, nunca una por fuente.
        all_ids = [result.source_id, *result.alt_source_ids][:MAX_RACE_SOURCES]
        candidates = [_decode_source_id(sid) for sid in all_ids]
        username, remote_path = candidates[0]

        download = Download(
            id=None,
            network=self.network,
            title=result.title,
            source_id=result.source_id,
            dest_path=dest_path,
            size_bytes=result.size_bytes,
            state=DownloadState.SEARCHING_SOURCES,
        )
        entry_id = self._next_entry_id
        self._next_entry_id += 1
        self._active[entry_id] = {
            "download": download,
            "username": username,
            "remote_path": remote_path,
            "candidates": candidates,
            "dest_path": dest_path,
            "conns": [],
            "file_conn": None,
            "token": None,
            "size": result.size_bytes,
            "paused": False,
            "cancelled": False,
            "winner": None,
            "winner_conn": None,
            "last_error": None,
            "limiter": RateLimiter(),
        }
        asyncio.create_task(self._run_download(entry_id))
        return download

    async def pause_download(self, download: Download) -> None:
        found = self._find_entry(download)
        if found is None:
            return
        _, entry = found
        entry["paused"] = True
        for conn in entry["conns"]:
            conn.close()
        entry["conns"] = []
        if entry["file_conn"]:
            entry["file_conn"].close()
        download.state = DownloadState.PAUSED
        download.speed_bps = 0.0
        self._notify(download)

    async def resume_download(self, download: Download) -> None:
        found = self._find_entry(download)
        if found is None:
            return
        entry_id, entry = found
        entry["paused"] = False
        entry["cancelled"] = False
        download.state = DownloadState.SEARCHING_SOURCES
        self._notify(download)
        asyncio.create_task(self._run_download(entry_id))

    async def reattach_download(self, download: Download) -> None:
        """Reengancha una descarga tras reiniciar la app. Solo se
        recupera la fuente original (`source_id`): las fuentes
        alternativas de un resultado fusionado no se persisten en el
        modelo `Download`, así que aquí no hay "carrera" entre varias,
        a diferencia de `start_download`."""
        if self._server is None or self._find_entry(download) is not None:
            return
        username, remote_path = _decode_source_id(download.source_id)
        entry_id = self._next_entry_id
        self._next_entry_id += 1
        self._active[entry_id] = {
            "download": download,
            "username": username,
            "remote_path": remote_path,
            "candidates": [(username, remote_path)],
            "dest_path": download.dest_path,
            "conns": [],
            "file_conn": None,
            "token": None,
            "size": download.size_bytes,
            "paused": download.state == DownloadState.PAUSED,
            "cancelled": False,
            "winner": None,
            "winner_conn": None,
            "last_error": None,
            "limiter": RateLimiter(),
        }
        if download.state != DownloadState.PAUSED:
            download.state = DownloadState.SEARCHING_SOURCES
            self._notify(download)
            asyncio.create_task(self._run_download(entry_id))

    async def cancel_download(self, download: Download) -> None:
        found = self._find_entry(download)
        if found is None:
            return
        entry_id, entry = found
        entry["cancelled"] = True
        for conn in entry["conns"]:
            conn.close()
        entry["conns"] = []
        if entry["file_conn"]:
            entry["file_conn"].close()
        download.state = DownloadState.CANCELLED
        download.speed_bps = 0.0
        self._notify(download)
        self._active.pop(entry_id, None)

    def subscribe_progress(self, callback: Callable[[Download], None]) -> None:
        self._progress_callback = callback

    # ---- chat: salas y mensajes privados (punto 13 del backlog) ----

    def supports_chat(self) -> bool:
        return True

    def subscribe_chat(self, callback: Callable[[dict], None]) -> None:
        """Registra un callback que recibe cada evento de chat según
        llega del servidor, como dict con clave "type":
        - {"type": "room_message", "room", "user", "message"}
        - {"type": "room_joined", "room", "users": [str, ...]}
        - {"type": "user_joined_room", "room", "username"}
        - {"type": "user_left_room", "room", "username"}
        - {"type": "private_message", "user", "message", "message_id", "timestamp", "is_new"}
        - {"type": "room_list", "rooms": [(nombre, nº_usuarios), ...]}
        """
        self._chat_callback = callback

    def _notify_chat(self, event: dict) -> None:
        if self._chat_callback:
            self._chat_callback(event)

    async def join_room(self, room: str) -> None:
        if self._server is None:
            raise RuntimeError("Backend de Soulseek no conectado")
        await self._server.send(_SRV_JOIN_ROOM, pack_string(room) + pack_uint32(0))

    async def leave_room(self, room: str) -> None:
        if self._server is None:
            raise RuntimeError("Backend de Soulseek no conectado")
        await self._server.send(_SRV_LEAVE_ROOM, pack_string(room))

    async def say_in_room(self, room: str, message: str) -> None:
        if self._server is None:
            raise RuntimeError("Backend de Soulseek no conectado")
        await self._server.send(_SRV_SAY_CHATROOM, pack_string(room) + pack_string(message))

    async def send_private_message(self, username: str, message: str) -> None:
        if self._server is None:
            raise RuntimeError("Backend de Soulseek no conectado")
        await self._server.send(_SRV_MESSAGE_USER, pack_string(username) + pack_string(message))

    async def get_room_list(self, timeout: float = 10.0) -> list[tuple[str, int]]:
        if self._server is None:
            raise RuntimeError("Backend de Soulseek no conectado")
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._room_list_future = future
        try:
            await self._server.send(_SRV_ROOM_LIST)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return []
        finally:
            self._room_list_future = None

    def get_stats(self) -> dict:
        if self._server is None:
            return {}
        stats = {
            "server": f"{SERVER_HOST}:{SERVER_PORT}",
            "username": self._username,
            "listen_port": self._listen_port,
            "active_transfers": len(self._active),
        }
        if self._external_ip is not None:
            stats["external_ip"] = self._external_ip
        if self._shared_library is not None and self._shared_library.enabled:
            stats["shared_files"] = len(self._shared_library.list_files())
            stats["active_uploads"] = self._active_uploads
        return stats

    def set_download_limit(self, download: Download, rate_bps: int) -> None:
        found = self._find_entry(download)
        if found is not None:
            found[1]["limiter"].set_rate(rate_bps)

    def _find_entry(self, download: Download) -> tuple[int, dict] | None:
        for entry_id, entry in self._active.items():
            if entry["download"] is download or entry["download"].id == download.id:
                return entry_id, entry
        return None

    def _notify(self, download: Download) -> None:
        if self._progress_callback:
            self._progress_callback(download)

    # ---- servidor: bucle de lectura y helpers de direcciones/conexión ----

    async def _server_read_loop(self) -> None:
        try:
            while True:
                code, payload = await self._server.read_message()
                up = _Unpacker(payload)

                if code == _SRV_GET_PEER_ADDRESS:
                    user = up.string()
                    ip = up.ip()
                    port = up.uint32()
                    future = self._pending_address.get(user)
                    if future and not future.done():
                        future.set_result((ip if port else None, port))

                elif code == _SRV_CONNECT_TO_PEER:
                    # OJO: el orden aquí NO es el mismo que al enviar
                    # ConnectToPeer nosotros mismos (token+user+conn_type):
                    # esta es la versión que nos manda el servidor cuando
                    # OTRO peer nos pide conectarnos a él, y va sin el
                    # token por delante (user, conn_type, ip, port, token...).
                    user = up.string()
                    conn_type = up.string()
                    ip = up.ip()
                    port = up.uint32()
                    token = up.uint32()
                    asyncio.create_task(self._handle_connect_to_peer_request(token, user, conn_type, ip, port))

                elif code == _SRV_CANT_CONNECT_TO_PEER:
                    token = up.uint32()
                    pending = self._pending_indirect.pop(token, None)
                    if pending:
                        _conn_type, future = pending
                        if not future.done():
                            future.set_exception(ConnectionError("El peer no pudo completar la conexión indirecta"))

                elif code == _SRV_SAY_CHATROOM:
                    room = up.string()
                    user = up.string()
                    message = up.string()
                    self._notify_chat({"type": "room_message", "room": room, "user": user, "message": message})

                elif code == _SRV_JOIN_ROOM:
                    room = up.string()
                    users = _parse_room_usernames(up)
                    self._notify_chat({"type": "room_joined", "room": room, "users": users})

                elif code == _SRV_USER_JOINED_ROOM:
                    room = up.string()
                    username = up.string()
                    self._notify_chat({"type": "user_joined_room", "room": room, "username": username})

                elif code == _SRV_USER_LEFT_ROOM:
                    room = up.string()
                    username = up.string()
                    self._notify_chat({"type": "user_left_room", "room": room, "username": username})

                elif code == _SRV_MESSAGE_USER:
                    message_id = up.uint32()
                    timestamp = up.uint32()
                    user = up.string()
                    message = up.string()
                    is_new = up.boolean()
                    asyncio.ensure_future(self._server.send(_SRV_MESSAGE_ACKED, pack_uint32(message_id)))
                    self._notify_chat({
                        "type": "private_message", "user": user, "message": message,
                        "message_id": message_id, "timestamp": timestamp, "is_new": is_new,
                    })

                elif code == _SRV_ROOM_LIST:
                    rooms = _parse_room_list(up)
                    if self._room_list_future and not self._room_list_future.done():
                        self._room_list_future.set_result(rooms)
                    self._notify_chat({"type": "room_list", "rooms": rooms})
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass

    async def _get_peer_address(self, username: str) -> tuple[str | None, int]:
        future = asyncio.get_event_loop().create_future()
        self._pending_address[username] = future
        try:
            await self._server.send(_SRV_GET_PEER_ADDRESS, pack_string(username))
            return await asyncio.wait_for(future, timeout=10.0)
        except asyncio.TimeoutError:
            return None, 0
        finally:
            self._pending_address.pop(username, None)

    async def _dial_peer_direct(self, username: str, ip: str | None, port: int, conn_type: str) -> _PeerConn:
        if not ip or not port:
            raise ConnectionError("Dirección directa desconocida")
        reader, writer = await proxy_open_connection(ip, port, proxy=self._proxy, timeout=8.0)
        payload = pack_string(self._username) + pack_string(conn_type) + pack_uint32(0)
        await _send_init(writer, _INIT_PEER_INIT, payload)
        return _PeerConn(reader, writer, username)

    async def _dial_peer(self, username: str, conn_type: str, timeout: float = 20.0) -> _PeerConn:
        """Estrategia dual: intenta una conexión directa Y, a la vez, le
        pide al servidor una conexión indirecta (el peer nos marca a
        nosotros). Se usa la que llegue antes."""
        ip, port = await self._get_peer_address(username)

        token = self._next_token
        self._next_token += 1
        indirect_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_indirect[token] = (conn_type, indirect_future)

        direct_task = asyncio.create_task(self._dial_peer_direct(username, ip, port, conn_type))
        await self._server.send(
            _SRV_CONNECT_TO_PEER, pack_uint32(token) + pack_string(username) + pack_string(conn_type)
        )

        try:
            done, pending = await asyncio.wait(
                {direct_task, indirect_future}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            self._pending_indirect.pop(token, None)

        winner: _PeerConn | None = None
        for finished in done:
            try:
                result = finished.result()
            except Exception:
                continue
            if isinstance(result, _PeerConn):
                if winner is None:
                    winner = result
                else:
                    # Directa e indirecta llegaron a completarse las dos:
                    # nos quedamos con la primera y cerramos la otra para
                    # no dejar el socket abierto indefinidamente.
                    result.close()

        for unfinished in pending:
            unfinished.cancel()

        if winner is None:
            raise ConnectionError(f"No se pudo conectar con {username} (ni directa ni indirectamente)")
        return winner

    async def _handle_connect_to_peer_request(self, token: int, user: str, conn_type: str,
                                                ip: str, port: int) -> None:
        """El servidor nos pide, en nombre de `user`, que nos conectemos
        NOSOTROS hacia él/ella porque su intento directo hacia nosotros
        falló."""
        try:
            reader, writer = await proxy_open_connection(ip, port, proxy=self._proxy, timeout=15.0)
        except (OSError, asyncio.TimeoutError):
            if self._server:
                await self._server.send(_SRV_CANT_CONNECT_TO_PEER, pack_uint32(token) + pack_string(user))
            return

        try:
            await _send_init(writer, _INIT_PIERCE_FIREWALL, pack_uint32(token))
        except (ConnectionError, OSError):
            # El peer (normalmente ajeno a nuestras propias descargas —
            # esto también nos llega por tráfico de la red distribuida de
            # otros usuarios) cerró la conexión justo después de aceptarla.
            # No hay nada que hacer salvo descartarla.
            writer.close()
            return
        peer = _PeerConn(reader, writer, user)
        if conn_type == "F":
            asyncio.create_task(self._handle_incoming_file_connection(peer))
        else:
            asyncio.create_task(self._handle_incoming_peer_connection(peer))

    async def _handle_incoming_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Punto 39 del backlog: descarta antes incluso de gastar un hueco
        # del "gate" de conexiones concurrentes -el camino saliente ya pasa
        # por el mismo filtro dentro de `core.proxy.open_connection`.
        peer_ip = _peer_ip_from_writer(writer)
        if peer_ip and ip_filter.is_blocked(peer_ip):
            writer.close()
            return
        if self._incoming_gate_count >= self._incoming_gate_limit:
            writer.close()
            return
        self._incoming_gate_count += 1
        released = False

        def release_gate() -> None:
            nonlocal released
            if not released:
                released = True
                self._incoming_gate_count -= 1

        try:
            code, payload = await asyncio.wait_for(_read_init(reader), timeout=30.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            writer.close()
            release_gate()
            return

        up = _Unpacker(payload)

        if code == _INIT_PEER_INIT:
            init_user = up.string()
            conn_type = up.string()
            peer = _PeerConn(reader, writer, init_user)
            if conn_type == "F":
                asyncio.create_task(self._handle_incoming_file_connection(peer, release_gate))
            else:
                asyncio.create_task(self._handle_incoming_peer_connection(peer, release_gate))

        elif code == _INIT_PIERCE_FIREWALL:
            token = up.uint32()
            pending = self._pending_indirect.get(token)
            if pending is None:
                writer.close()
                release_gate()
                return
            _conn_type, future = pending
            if future.done():
                writer.close()
                release_gate()
                return
            future.set_result(_PeerConn(reader, writer, ""))
            # A partir de aquí la conexión pasa a gestionarla _dial_peer
            # (se queda como ganadora o se cierra si pierde la carrera
            # frente a la directa); son pocas, proporcionales a nuestras
            # propias descargas activas, así que no hace falta que sigan
            # contando contra el tope de entrantes sin solicitar.
            release_gate()

        else:
            writer.close()
            release_gate()

    # ---- conexiones "P": búsqueda y negociación de descarga ----

    async def _handle_incoming_peer_connection(self, peer: _PeerConn,
                                                release_gate: Callable[[], None] | None = None) -> None:
        """Conexión "P" que NO hemos iniciado nosotros para pedir una
        descarga: en el alcance de este backend, esto son casi siempre
        resultados de búsqueda que otro peer nos manda por su cuenta,
        pero también puede ser un peer que quiere DESCARGAR de nosotros
        un fichero de nuestra carpeta compartida (`QueueUpload`), si
        `shared_library` está configurada.
        Se cierra en cuanto llega el primer mensaje útil (salvo que sea
        una petición de subida, ver `_handle_queue_upload`) en vez de
        mantenerse abierta hasta 30 s de inactividad: una búsqueda de
        varias palabras abre una petición por cada una hacia la red
        distribuida, y para un término popular eso hace que miles de
        peers abran conexión entrante casi a la vez — mantenerlas vivas
        agota los file descriptors del proceso (visto en producción:
        `OSError: [Errno 24] Demasiados ficheros abiertos`, que acaba
        provocando un SEGV en Qt al romperle el bucle de eventos). Además
        del cierre rápido, `_handle_incoming_connection` limita cuántas de
        estas conexiones se gestionan a la vez (`release_gate` libera ese
        hueco cuando esta termina, si venimos de ese camino)."""
        close_now = True
        try:
            code, payload = await asyncio.wait_for(peer.read_message(), timeout=30.0)
            if code == _PEER_FILE_SEARCH_RESPONSE:
                self._handle_search_response(payload)
            elif code == _PEER_QUEUE_UPLOAD:
                up = _Unpacker(payload)
                remote_path = up.string()
                await self._handle_queue_upload(peer, remote_path)
                close_now = False  # la negociación puede seguir viva (ver _handle_queue_upload)
            # Otros mensajes de peer (listas compartidas, info de
            # usuario, chat...) quedan fuera del alcance de este
            # backend (solo conexión + búsqueda + descarga + subida).
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if close_now:
                peer.close()
            if release_gate is not None:
                release_gate()

    async def _handle_queue_upload(self, peer: _PeerConn, remote_path: str) -> None:
        """Otro peer nos pide (por `QueueUpload`) que le subamos
        `remote_path`. Si lo tenemos compartido, negociamos la subida
        con un `TransferRequest`/`TransferResponse` igual que hace un
        cliente real de Soulseek en el papel de uploader; si acepta,
        abrimos nosotros la conexión "F" (el que sube el fichero es
        quien la inicia, ver cabecera del módulo) y mandamos los
        bytes."""
        shared = self._shared_library.find_by_native_path(remote_path) if self._shared_library else None
        if shared is None:
            try:
                await peer.send(_PEER_UPLOAD_DENIED, pack_string(remote_path) + pack_string("File not shared."))
            finally:
                peer.close()
            return

        token = self._next_token
        self._next_token += 1
        try:
            await peer.send(
                _PEER_TRANSFER_REQUEST,
                pack_uint32(_TRANSFER_DIRECTION_UPLOAD) + pack_uint32(token)
                + pack_string(remote_path) + pack_uint64(shared.size),
            )
            code, payload = await asyncio.wait_for(peer.read_message(), timeout=30.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            peer.close()
            return

        allowed = False
        if code == _PEER_TRANSFER_RESPONSE:
            up = _Unpacker(payload)
            up.uint32()  # token, ya lo sabemos
            allowed = up.boolean()

        peer.close()
        if allowed:
            asyncio.create_task(self._upload_file(peer.username, token, shared))

    async def _upload_file(self, username: str, token: int, shared: SharedFile) -> None:
        """Abre la conexión "F" hacia el descargador y le manda el
        fichero desde el offset que él mismo pida (permite que
        reanude una descarga a medias sin volver a empezar)."""
        try:
            conn = await self._dial_peer(username, "F")
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return

        self._active_uploads += 1
        try:
            conn.writer.write(pack_uint32(token))
            await conn.writer.drain()
            offset_bytes = await conn.reader.readexactly(8)
            offset = struct.unpack("<Q", offset_bytes)[0]
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
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self._active_uploads -= 1
            conn.close()

    def _handle_search_response(self, payload: bytes) -> None:
        try:
            data = zlib.decompress(payload)
        except zlib.error:
            return

        up = _Unpacker(data)
        username = up.string()
        token = up.uint32()
        if token not in self._search_results:
            return  # búsqueda ya cerrada (timeout cumplido) o token ajeno

        nfiles = up.uint32()
        files = []
        for _ in range(nfiles):
            up.uint8()  # code, sin uso
            filename = up.string()
            size = up.file_size()
            ext_len = up.uint32()  # campo "ext", obsoleto
            up.skip(ext_len)
            numattr = up.uint32()
            up.skip(numattr * 8)  # cada atributo son 2 uint32 (num, valor)
            files.append((filename, size))

        has_free_slots = up.boolean()
        callback = self._search_callbacks.get(token)
        for filename, size in files:
            entry = (username, filename, size, has_free_slots)
            self._search_results[token].append(entry)
            if callback is not None:
                callback(entry)

    # ---- navegar archivos compartidos de un usuario (punto 9 del backlog) ----

    async def browse_user(self, username: str, timeout: float = 30.0) -> list[tuple[str, list[SearchResult]]]:
        """`BrowseUser`/`GetSharedFileList`: pide y espera la lista
        completa de archivos compartidos por `username`, agrupada por
        carpeta. Reutiliza la misma conexión "P" y el mismo patrón
        petición+espera de respuesta que ya usa `_try_source` para pedir
        una descarga."""
        if self._server is None:
            raise RuntimeError("Backend de Soulseek no conectado")

        conn = await self._dial_peer(username, "P")
        try:
            await conn.send(_PEER_SHARED_FILE_LIST_REQUEST)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                code, payload = await asyncio.wait_for(conn.read_message(), timeout=remaining)
                if code == _PEER_SHARED_FILE_LIST_RESPONSE:
                    return _parse_shared_file_list(username, payload)
            raise TimeoutError(f"{username} no respondió con su lista de archivos a tiempo")
        finally:
            conn.close()

    async def _run_download(self, entry_id: int) -> None:
        """Orquesta la carrera entre todas las fuentes candidatas de una
        descarga: las prueba todas a la vez con `_try_source` y se queda
        con la primera que llegue a "empezar a transferir datos" (marca
        `entry["winner"]`); el resto se cancela en cuanto eso pasa."""
        entry = self._active.get(entry_id)
        if entry is None or entry["cancelled"]:
            return

        download = entry["download"]
        entry["winner"] = None
        entry["winner_conn"] = None
        entry["last_error"] = None

        tasks = [
            asyncio.create_task(self._try_source(entry_id, username, remote_path))
            for username, remote_path in entry["candidates"]
        ]
        pending = set(tasks)
        while pending and entry["winner"] is None:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        # Cierra las conexiones "P" de todas las fuentes perdedoras (o de
        # todas si ninguna ganó); la de la ganadora, si la hay, se deja
        # abierta — sigue haciendo falta hasta que pause/cancel_download.
        winner_conn = entry.get("winner_conn")
        for conn in entry["conns"]:
            if conn is not winner_conn:
                conn.close()
        entry["conns"] = [winner_conn] if winner_conn else []

        if entry["winner"] is None and not entry["cancelled"]:
            n = len(entry["candidates"])
            download.state = DownloadState.ERROR
            download.error_message = entry["last_error"] or (
                f"No se pudo conectar con ninguna de las {n} fuentes disponibles"
                if n > 1 else
                "No se pudo conectar con el peer (posible NAT/firewall en ambos extremos)"
            )
            self._notify(download)

    async def _try_source(self, entry_id: int, username: str, remote_path: str) -> None:
        """Intenta una única fuente candidata; si es la primera en llegar
        a un TransferRequest válido, se declara ganadora (`entry["winner"]`)
        y la conexión "F" que llegue después ya sabrá, vía
        `_transfers_by_token`, que corresponde a esta descarga."""
        entry = self._active.get(entry_id)
        if entry is None or entry["cancelled"] or entry["winner"] is not None:
            return
        download = entry["download"]

        try:
            conn = await self._dial_peer(username, "P")
        except (ConnectionError, OSError, asyncio.TimeoutError):
            entry["last_error"] = "No se pudo conectar con el peer (posible NAT/firewall en ambos extremos)"
            return

        if entry["cancelled"] or entry["winner"] is not None:
            conn.close()
            return

        entry["conns"].append(conn)
        await conn.send(_PEER_QUEUE_UPLOAD, pack_string(remote_path))
        if entry["winner"] is None:
            self._notify(download)

        deadline = time.monotonic() + 120.0  # incluye una posible espera en cola del otro lado
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                code, payload = await asyncio.wait_for(conn.read_message(), timeout=remaining)
                up = _Unpacker(payload)

                if code == _PEER_TRANSFER_REQUEST:
                    direction = up.uint32()
                    token = up.uint32()
                    up.string()  # file, ya lo sabemos (remote_path)
                    if direction != _TRANSFER_DIRECTION_UPLOAD:
                        continue
                    filesize = up.uint64()

                    if entry["winner"] is not None:
                        return  # otra fuente ya ganó la carrera mientras esperábamos

                    entry["winner"] = (username, remote_path)
                    entry["winner_conn"] = conn
                    entry["username"] = username
                    entry["remote_path"] = remote_path
                    entry["candidates"] = [(username, remote_path)]  # para un futuro pause/resume
                    entry["token"] = token
                    entry["size"] = filesize
                    download.size_bytes = filesize
                    self._transfers_by_token[token] = entry_id

                    await conn.send(_PEER_TRANSFER_RESPONSE, pack_uint32(token) + pack_bool(True))
                    download.state = DownloadState.DOWNLOADING
                    self._notify(download)
                    return  # el fichero llega por una conexión "F" aparte

                elif code == _PEER_UPLOAD_DENIED:
                    up.string()  # file
                    reason = up.string()
                    entry["last_error"] = f"Descarga rechazada por el peer: {reason}"
                    return

                elif code == _PEER_PLACE_IN_QUEUE_RESPONSE:
                    continue  # informativo, seguimos esperando el TransferRequest
        except asyncio.TimeoutError:
            entry["last_error"] = "Tiempo de espera agotado esperando que el peer inicie la transferencia"
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            if not entry["cancelled"] and not entry["paused"]:
                entry["last_error"] = "El peer cerró la conexión antes de iniciar la transferencia"

    # ---- conexiones "F": transferencia real del fichero ----

    async def _handle_incoming_file_connection(self, peer: _PeerConn,
                                                release_gate: Callable[[], None] | None = None) -> None:
        try:
            token_bytes = await peer.reader.readexactly(4)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            peer.close()
            if release_gate is not None:
                release_gate()
            return
        token = struct.unpack("<I", token_bytes)[0]

        entry_id = self._transfers_by_token.pop(token, None)
        entry = self._active.get(entry_id) if entry_id is not None else None
        if entry is None:
            peer.close()
            if release_gate is not None:
                release_gate()
            return

        download = entry["download"]
        size = entry.get("size") or download.size_bytes
        entry["file_conn"] = peer

        os.makedirs(entry["dest_path"], exist_ok=True)
        out_path = os.path.join(entry["dest_path"], download.title)
        existing_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        offset = existing_size if existing_size < size else 0

        try:
            peer.writer.write(pack_uint64(offset))
            await peer.writer.drain()
        except (ConnectionError, OSError):
            peer.close()
            if release_gate is not None:
                release_gate()
            return

        downloaded = offset
        speed_window_start = time.monotonic()
        speed_window_bytes = downloaded
        try:
            with open(out_path, "ab" if offset else "wb") as f:
                while downloaded < size:
                    chunk = await peer.reader.read(min(65536, size - downloaded))
                    if not chunk:
                        break
                    await global_download_limiter.consume(len(chunk))
                    await entry["limiter"].consume(len(chunk))
                    f.write(chunk)
                    downloaded += len(chunk)
                    download.downloaded_bytes = downloaded

                    # Velocidad instantánea suavizada en ventanas de ~0.5s
                    # (con cada trozo de 64 KB el intervalo sería demasiado
                    # ruidoso para ser una velocidad útil de mostrar).
                    now = time.monotonic()
                    elapsed = now - speed_window_start
                    if elapsed >= 0.5:
                        download.speed_bps = (downloaded - speed_window_bytes) / elapsed
                        speed_window_start = now
                        speed_window_bytes = downloaded
                    self._notify(download)
        except (ConnectionError, OSError):
            pass
        finally:
            entry["file_conn"] = None
            peer.close()
            download.speed_bps = 0.0
            if release_gate is not None:
                release_gate()

        if entry["cancelled"]:
            return  # cancel_download ya dejó el estado como corresponde
        if entry["paused"]:
            download.state = DownloadState.PAUSED
        elif downloaded >= size and size > 0:
            download.state = DownloadState.COMPLETED
        else:
            download.state = DownloadState.ERROR
            download.error_message = f"Conexión cerrada tras descargar {downloaded} de {size} bytes"
        self._notify(download)

        if download.state in (DownloadState.COMPLETED, DownloadState.ERROR):
            self._active.pop(entry_id, None)
