"""
Backend eD2k (eDonkey2000) + Kad (Kademlia de eMule) implementando ambos
protocolos directamente sobre sockets asyncio, sin aMule ni ningún otro
programa externo — igual que el resto de backends de este proyecto.

Referencia usada para implementar esto: el código fuente real de aMule
(https://github.com/amule-project/amule), en concreto:
  - src/protocol/ed2k/{Client2Server,Client2Client}/{TCP,UDP}.h (opcodes)
  - src/protocol/kad/Client2Client/UDP.h, src/protocol/kad/Constants.h,
    src/protocol/kad2/Client2Client/UDP.h (opcodes de Kad)
  - src/tags/{Tag,TagTypes,ClientTags,FileTags}.h (formato de los tags)
  - src/SafeFile.cpp (WriteTag/ReadTag, WriteString, WriteUInt*)
  - src/Packet.{h,cpp} (framing TCP/UDP, compresión zlib)
  - src/ServerList.cpp (formato binario de server.met)
  - src/kad_src/RoutingZone.cpp (formato binario de nodes.dat)
  - src/ServerConnect.cpp (construcción del login OP_LOGINREQUEST)
  - src/kad_src/Search.cpp, src/kad_src/KademliaUDPListener.cpp (Kad)
  - src/Preferences.cpp (URLs por defecto de server.met/nodes.dat)

Es, con diferencia, la red más laboriosa de las seis: mezcla TCP
cliente-servidor (login/búsqueda/fuentes), TCP cliente-cliente
(transferencia real) y UDP (Kad, la DHT de eMule). Todo el hashing
(identificación de ficheros y verificación de partes) usa MD4, que el
OpenSSL de muchos sistemas ya no trae habilitado — de ahí la
implementación nativa en core/md4.py.

Simplificaciones conscientes frente al eMule/aMule real (documentadas
aquí para que quede claro qué NO se ha replicado al 100%):
  - Tabla de rutas de Kad simplificada: una lista plana de contactos
    conocidos en vez del árbol de k-buckets real. No afecta a la
    compatibilidad de cable (los mensajes que mandamos son idénticos),
    solo a qué tan "inteligente" es nuestra elección de a quién
    preguntar — aceptable para búsquedas puntuales, no para ser un nodo
    Kad que ayuda a enrutar a otros.
  - Sin las colas de subida reales de eMule (que pueden tardar horas en
    dar turno), en ninguno de los dos sentidos: como descarga, si una
    fuente no acepta enseguida la descartamos y probamos con la
    siguiente candidata; como subida propia (ver "compartir" más
    abajo), concedemos slot de inmediato a quien nos lo pida en vez de
    encolar según crédito/prioridad como hace el cliente real.
  - Sin ficheros >4GB (variantes _I64 de las partes).
  - Ofuscación de protocolo (punto 20 del backlog): implementado el
    esquema "básico" cliente<->cliente de aMule/eMule real
    ("EncryptedStreamSocket" en su código fuente — a pesar del nombre,
    la propia aMule documenta que NO cumple estándares criptográficos,
    ya que usa claves RC4 no secretas: es puramente ofuscación de
    tráfico para esquivar el throttling por DPI de algunos ISPs, no
    cifrado con fines de seguridad). Ver la sección "Ofuscación de
    protocolo (punto 20)" más abajo para el detalle del handshake. No
    se ha implementado la variante cliente<->servidor (basada en
    acuerdo de claves Diffie-Hellman): es bastante más compleja, tiene
    ~200 bytes más de overhead, y su propio autor documenta en el
    comentario de cabecera de EncryptedStreamSocket.cpp que no aporta
    protección sustancial adicional frente a la variante básica.
"""

import asyncio
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import unquote, urlparse

from core import upnp
from core.aich import EMBLOCKSIZE, block_count, block_sha1, levels_to_part
from core.backend_base import NetworkBackend
from core.config import _config_dir
from core.md4 import md4
from core.models import Download, DownloadState, Network, SearchResult
from core.proxy import ProxyConfig
from core.proxy import open_connection as proxy_open_connection
from core.rate_limiter import RateLimiter, global_download_limiter, global_upload_limiter
from core.sharing import SharedFile, SharedLibrary
from core.stats_tracker import stats_tracker

# --------------------------------------------------------------------------
# Constantes del protocolo (todas verificadas contra el código de aMule)
# --------------------------------------------------------------------------

# Bytes de protocolo (primer byte de cualquier paquete, TCP o UDP)
OP_EDONKEYPROT = 0xE3          # cliente <-> servidor
OP_EMULEPROT = 0xC5            # cliente <-> cliente (extensiones eMule)
OP_PACKEDPROT = 0xD4           # payload comprimido con zlib (TCP)
OP_KADEMLIAHEADER = 0xE4       # Kad sin comprimir (UDP)
OP_KADEMLIAPACKEDPROT = 0xE5   # Kad comprimido con zlib (UDP)

# Opcodes cliente -> servidor / servidor -> cliente (TCP)
OP_LOGINREQUEST = 0x01
OP_DISCONNECT = 0x02
OP_SERVERLIST = 0x32
OP_OFFERFILES = 0x15
OP_SEARCHREQUEST = 0x16
OP_SEARCHRESULT = 0x33
OP_GETSOURCES = 0x19
OP_FOUNDSOURCES = 0x42
OP_SERVERSTATUS = 0x34
OP_SERVERMESSAGE = 0x38
OP_IDCHANGE = 0x40
OP_CALLBACKREQUEST = 0x1C
OP_CALLBACKREQUESTED = 0x35   # servidor -> cliente LowID: <IP 4><PORT 2> del peer a llamar
OP_REJECT = 0x05

# Opcodes cliente <-> cliente (TCP, descarga)
OP_HELLO = 0x01
OP_HELLOANSWER = 0x4C
OP_SETREQFILEID = 0x4F
OP_FILESTATUS = 0x50
OP_FILEREQANSNOFIL = 0x48
OP_HASHSETREQUEST = 0x51
OP_HASHSETANSWER = 0x52
OP_STARTUPLOADREQ = 0x54
OP_ACCEPTUPLOADREQ = 0x55
OP_QUEUERANK = 0x5C
OP_QUEUERANKING = 0x60
OP_REQUESTPARTS = 0x47
OP_SENDINGPART = 0x46

# Opcodes AICH (punto 17 del backlog), bajo OP_EMULEPROT — verificación por
# sub-bloque de 180 KiB en vez de por parte entera de ~9,3 MiB.
OP_AICHREQUEST = 0x9B
OP_AICHANSWER = 0x9C
OP_AICHFILEHASHANS = 0x9D
OP_AICHFILEHASHREQ = 0x9E

# Cola de subida propia (punto 15 del backlog): mismo número de slots
# simultáneos "de fábrica" que trae el eMule real por defecto.
DEFAULT_UPLOAD_SLOTS = 3
UPLOAD_QUEUE_TIMEOUT = 600.0  # 10 min esperando turno antes de abandonar

# Opcodes Kad (UDP, bajo OP_KADEMLIAHEADER/OP_KADEMLIAPACKEDPROT)
KADEMLIA2_BOOTSTRAP_REQ = 0x01
KADEMLIA2_BOOTSTRAP_RES = 0x09
KADEMLIA2_REQ = 0x21
KADEMLIA2_RES = 0x29
KADEMLIA2_SEARCH_KEY_REQ = 0x33
KADEMLIA2_SEARCH_SOURCE_REQ = 0x34
KADEMLIA2_SEARCH_RES = 0x3B
KADEMLIA_FIND_VALUE = 0x02
KADEMLIA_FIND_NODE = 0x0B
KADEMLIA_VERSION = 0x08
KADEMLIA_FIREWALLED_REQ = 0x50      # <TCPPORT (remitente) 2>
KADEMLIA_FIREWALLED_RES = 0x58      # <IP (remitente, tal y como se vio) 4>
KADEMLIA_FIREWALLED_ACK_RES = 0x59  # (vacío) confirma que el TCP sí fue alcanzable

# Tipos de tag (SafeFile.cpp / TagTypes.h)
TAGTYPE_HASH16 = 0x01
TAGTYPE_STRING = 0x02
TAGTYPE_UINT32 = 0x03
TAGTYPE_FLOAT32 = 0x04
TAGTYPE_BLOB = 0x07
TAGTYPE_UINT16 = 0x08
TAGTYPE_UINT8 = 0x09
TAGTYPE_BSOB = 0x0A
TAGTYPE_UINT64 = 0x0B
TAGTYPE_STR1 = 0x11   # .. TAGTYPE_STR16 = 0x20 (longitud = tipo - 0x10)

# IDs de tag usados en login (ClientTags.h)
CT_NAME = 0x01
CT_VERSION = 0x11
CT_SERVER_FLAGS = 0x20
CT_EMULE_VERSION = 0xFB

# IDs de tag de fichero, usados en resultados de búsqueda (FileTags.h)
FT_FILENAME = 0x01
FT_FILESIZE = 0x02
FT_FILETYPE = 0x03
FT_FILEFORMAT = 0x04
FT_SOURCES = 0x15

# Valores de FT_FILETYPE reconocidos por el servidor (opcodes.h de eMule
# real): "Arc"/"Iso" están marcados ahí como "eMule internal use only" y
# no se mandan nunca por red, así que las categorías "archive"/"cdimage"
# del filtro de la GUI (ver gui/widgets/search_tab.py) no tienen
# equivalente server-side y se quedan solo con el filtro client-side ya
# existente.
_ED2K_FILETYPE_BY_CATEGORY = {
    "audio": "Audio",
    "video": "Video",
    "picture": "Image",
    "document": "Doc",
    "program": "Pro",
}

EDONKEYVERSION = 0x3C  # valor histórico usado por eMule/aMule real
SRVCAP_ZLIB = 0x0001
SRVCAP_NEWTAGS = 0x0008
SRVCAP_UNICODE = 0x0010
SRVCAP_LARGEFILES = 0x0100

PARTSIZE = 9_728_000       # tamaño de "parte" para hashing (~9.28 MB)
HIGH_ID_THRESHOLD = 0x01000000
REQUEST_CHUNK = 180 * 1024  # tamaño de cada OP_REQUESTPARTS individual

SERVER_MET_URL = "https://upd.emule-security.org/server.met"
NODES_DAT_URL = "https://upd.emule-security.org/nodes.dat"


# --------------------------------------------------------------------------
# Ofuscación de protocolo (punto 20 del backlog): esquema "básico"
# cliente<->cliente de aMule/eMule real, tal cual está descrito y
# codificado en src/EncryptedStreamSocket.cpp y src/RC4Encrypt.cpp del
# código fuente real de aMule (github.com/amule-org/amule). No es cifrado
# con fines de seguridad -- la propia cabecera de EncryptedStreamSocket.h
# dice explícitamente que no cumple estándares criptográficos, porque usa
# claves RC4 que no son secretas (cualquiera que conozca el userhash del
# peer, que se reparte libremente para poder ser localizado en la red,
# puede derivarlas) -- es puramente ofuscación de tráfico: hace que la
# conversación parezca ruido aleatorio en vez del patrón de bytes
# reconocible de un paquete eD2k/eMule normal, para esquivar el
# throttling basado en DPI que aplican algunos ISPs al P2P.
#
# Handshake (cliente<->cliente):
#   Cliente A (quien abre la conexión saliente):
#     Sendkey    = Md5(<UserHashClienteB 16><0x22 (34) 1><RandomKeyPartA 4>)
#     Receivekey = Md5(<UserHashClienteB 16><0xCB (203) 1><RandomKeyPartA 4>)
#   Cliente B (quien acepta la conexión entrante):
#     Sendkey    = Md5(<UserHashClienteB 16><0xCB (203) 1><RandomKeyPartA 4>)
#     Receivekey = Md5(<UserHashClienteB 16><0x22 (34) 1><RandomKeyPartA 4>)
#   (UserHashClienteB es siempre el hash del que acepta la conexión: quien
#   abre la conexión necesita conocerlo de antemano, típicamente porque ya
#   ha hablado con ese mismo peer alguna vez antes y le vio el hash en un
#   OP_HELLO/OP_HELLOANSWER anterior -- ver `_known_peer_hashes` más abajo).
#   Las claves se generan con las 1024 primeras posiciones del keystream
#   RC4 descartadas (mitigación estándar de los sesgos conocidos del
#   arranque de RC4).
#
#   A -> B: <MarcaSemiAleatoriaNoProtocolo 1 [sin cifrar]>
#           <RandomKeyPartA 4 [sin cifrar]>
#           <MagicValueSync 4><MétodosSoportados 1><MétodoPreferido 1>
#           <LongitudRelleno 1><RellenoAleatorio LongitudRelleno>   [cifrado]
#   B -> A: <MagicValueSync 4><MétodoElegido 1>
#           <LongitudRelleno 1><RellenoAleatorio LongitudRelleno>   [cifrado]
#
# El primer byte (MarcaSemiAleatoriaNoProtocolo) se manda siempre sin
# cifrar y se elige a propósito para que nunca coincida con
# OP_EDONKEYPROT/OP_EMULEPROT/OP_PACKEDPROT: así quien recibe una conexión
# nueva solo necesita mirar ese primer byte para saber si está ante un
# paquete normal o ante el arranque de un handshake de ofuscación, sin
# necesidad de descifrar nada todavía.
# --------------------------------------------------------------------------

_OBF_MAGICVALUE_REQUESTER = 34           # modifica las claves send de A / receive de B
_OBF_MAGICVALUE_SERVER = 203             # modifica las claves send de B / receive de A
_OBF_MAGICVALUE_SYNC = 0x835E6FC4        # valor fijo para verificar que el descifrado funciona
_OBF_ENM_OBFUSCATION = 0x00              # único método soportado hoy por el protocolo real


class ObfuscationError(Exception):
    """La negociación de ofuscación de protocolo falló (cabecera o
    MagicValue inesperados): la conexión debe cerrarse, igual que hace
    aMule real (OnError(ERR_ENCRYPTION))."""


class _RC4Cipher:
    """RC4 estándar (KSA + PRGA), con el mismo "descarte de los primeros
    1024 bytes de keystream" que aplica aMule para las claves de
    ofuscación TCP (mitigación conocida de los sesgos del arranque de
    RC4; aMule se lo salta para las claves UDP por ahorrar CPU, pero aquí
    solo se usa para TCP así que siempre se aplica)."""

    __slots__ = ("_state", "_x", "_y")

    def __init__(self, key: bytes):
        state = list(range(256))
        index2 = 0
        for i in range(256):
            index2 = (key[i % len(key)] + state[i] + index2) % 256
            state[i], state[index2] = state[index2], state[i]
        self._state = state
        self._x = 0
        self._y = 0
        self.crypt(bytes(1024))  # descarta el arranque, solo para avanzar el estado

    def crypt(self, data: bytes) -> bytes:
        state = self._state
        x, y = self._x, self._y
        out = bytearray(len(data))
        for i, byte in enumerate(data):
            x = (x + 1) % 256
            y = (state[x] + y) % 256
            state[x], state[y] = state[y], state[x]
            out[i] = byte ^ state[(state[x] + state[y]) % 256]
        self._x, self._y = x, y
        return bytes(out)


def _obf_derive_key(user_hash: bytes, magic_value: int, random_key_part: int) -> bytes:
    data = user_hash + bytes([magic_value]) + struct.pack("<I", random_key_part)
    return hashlib.md5(data).digest()


def _obf_semi_random_marker() -> int:
    for _ in range(128):
        candidate = os.urandom(1)[0]
        if candidate not in (OP_EDONKEYPROT, OP_EMULEPROT, OP_PACKEDPROT):
            return candidate
    return 0x01  # mala suerte extrema con los 128 intentos; cualquier valor no-protocolo vale


class _ObfuscatedPeerStream:
    """Envuelve un (reader, writer) de asyncio para negociar y aplicar la
    ofuscación básica cliente<->cliente de eMule (ver comentario de
    cabecera de esta sección). Expone la misma superficie mínima que usa
    el resto del código para hablar con un peer (readexactly/write/
    drain/close/wait_closed), así que build_tcp_packet/read_tcp_packet y
    las funciones que dialogan con un peer no necesitan saber si la
    conexión está ofuscada o no: basta con pasarles esta instancia en
    vez del (reader, writer) crudos una vez completado el handshake."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._send_cipher: _RC4Cipher | None = None
        self._recv_cipher: _RC4Cipher | None = None

    async def readexactly(self, n: int) -> bytes:
        data = await self._reader.readexactly(n)
        if self._recv_cipher is not None:
            data = self._recv_cipher.crypt(data)
        return data

    def write(self, data: bytes) -> None:
        if self._send_cipher is not None:
            data = self._send_cipher.crypt(data)
        self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()

    def get_extra_info(self, name: str, default=None):
        return self._writer.get_extra_info(name, default)

    # -- negociación como Cliente A (extremo que abre la conexión) -------

    async def negotiate_outgoing(self, target_user_hash: bytes, timeout: float = 15.0) -> None:
        random_key_part = struct.unpack("<I", os.urandom(4))[0]
        self._send_cipher = _RC4Cipher(_obf_derive_key(target_user_hash, _OBF_MAGICVALUE_REQUESTER, random_key_part))
        self._recv_cipher = _RC4Cipher(_obf_derive_key(target_user_hash, _OBF_MAGICVALUE_SERVER, random_key_part))

        padding_len = os.urandom(1)[0] % 16
        prefix = bytes([_obf_semi_random_marker()]) + struct.pack("<I", random_key_part)  # sin cifrar
        rest = (
            struct.pack("<I", _OBF_MAGICVALUE_SYNC)
            + bytes([_OBF_ENM_OBFUSCATION, _OBF_ENM_OBFUSCATION, padding_len])
            + os.urandom(padding_len)
        )
        self._writer.write(prefix + self._send_cipher.crypt(rest))
        await self._writer.drain()

        magic = struct.unpack("<I", await asyncio.wait_for(self.readexactly(4), timeout=timeout))[0]
        if magic != _OBF_MAGICVALUE_SYNC:
            raise ObfuscationError("MagicValue inválido negociando ofuscación saliente con el peer")
        selected_method, resp_padding_len = await asyncio.wait_for(self.readexactly(2), timeout=timeout)
        if resp_padding_len:
            await asyncio.wait_for(self.readexactly(resp_padding_len), timeout=timeout)  # relleno, se descarta

    # -- negociación como Cliente B (extremo que acepta la conexión) -----

    async def negotiate_incoming(self, own_user_hash: bytes, timeout: float = 15.0) -> None:
        random_key_part = struct.unpack("<I", await asyncio.wait_for(
            self._reader.readexactly(4), timeout=timeout))[0]
        self._recv_cipher = _RC4Cipher(_obf_derive_key(own_user_hash, _OBF_MAGICVALUE_REQUESTER, random_key_part))
        self._send_cipher = _RC4Cipher(_obf_derive_key(own_user_hash, _OBF_MAGICVALUE_SERVER, random_key_part))

        magic = struct.unpack("<I", await asyncio.wait_for(self.readexactly(4), timeout=timeout))[0]
        if magic != _OBF_MAGICVALUE_SYNC:
            raise ObfuscationError("MagicValue inválido negociando ofuscación entrante del peer")
        supported, preferred, padding_len = await asyncio.wait_for(self.readexactly(3), timeout=timeout)
        if padding_len:
            await asyncio.wait_for(self.readexactly(padding_len), timeout=timeout)  # relleno, se descarta

        resp_padding_len = os.urandom(1)[0] % 16
        response = (
            struct.pack("<I", _OBF_MAGICVALUE_SYNC)
            + bytes([_OBF_ENM_OBFUSCATION, resp_padding_len])
            + os.urandom(resp_padding_len)
        )
        self.write(response)
        await self.drain()


async def _maybe_negotiate_incoming_obfuscation(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                                  own_user_hash: bytes, timeout: float = 15.0):
    """Se llama nada más aceptar una conexión entrante cliente-cliente,
    antes de asumir que el primer byte es un OP_EDONKEYPROT/OP_EMULEPROT/
    OP_PACKEDPROT normal. Un peer real de eMule con la ofuscación activada
    manda en su lugar un primer byte "marcador" elegido a propósito para
    no coincidir con ninguno de esos tres opcodes, así que basta con
    mirar ese byte para decidir sin necesidad de descifrar nada todavía.
    Devuelve (reader, writer, protocol_byte) sin tocar nada si la conexión
    es normal, o (stream, stream, None) tras completar el handshake de
    ofuscación si no lo era."""
    first_byte = (await asyncio.wait_for(reader.readexactly(1), timeout=timeout))[0]
    if first_byte in (OP_EDONKEYPROT, OP_EMULEPROT, OP_PACKEDPROT):
        return reader, writer, first_byte
    stream = _ObfuscatedPeerStream(reader, writer)
    await stream.negotiate_incoming(own_user_hash, timeout=timeout)
    return stream, stream, None


# --------------------------------------------------------------------------
# Tags eD2k: codificación (para paquetes salientes) y lectura genérica
# (para paquetes entrantes, que además pueden venir en el formato
# "compacto": bit 0x80 del tipo -> nombre de un solo byte sin longitud).
# --------------------------------------------------------------------------

@dataclass
class Tag:
    name_id: int | None    # ID de un solo byte (caso normal) o None si viene con nombre de texto
    name: str | None       # nombre de texto (raro, casi siempre None)
    type: int
    value: object


def _write_ed2k_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<H", len(b)) + b


def write_tag(name_id: int, tagtype: int, value) -> bytes:
    """Construye un tag en el formato "viejo" (el único que hace falta
    generar nosotros: name_len(2)=1 + name_id(1) + valor)."""
    out = bytearray()
    out.append(tagtype)
    out += struct.pack("<H", 1)
    out.append(name_id)
    if tagtype == TAGTYPE_STRING:
        out += _write_ed2k_string(value)
    elif tagtype == TAGTYPE_UINT64:
        out += struct.pack("<Q", value)
    elif tagtype == TAGTYPE_UINT32:
        out += struct.pack("<I", value)
    elif tagtype == TAGTYPE_UINT16:
        out += struct.pack("<H", value)
    elif tagtype == TAGTYPE_UINT8:
        out += struct.pack("<B", value)
    elif tagtype == TAGTYPE_FLOAT32:
        out += struct.pack("<f", value)
    elif tagtype == TAGTYPE_HASH16:
        out += value
    else:
        raise ValueError(f"write_tag: tipo de tag no soportado {tagtype:#x}")
    return bytes(out)


def read_tag(buf: bytes, offset: int) -> tuple[Tag, int]:
    """Lee un tag en cualquiera de los dos formatos que puede mandar la
    red (viejo con longitud, o compacto con nombre de 1 byte marcado
    con el bit 0x80), y soporta TAGTYPE_STR1..STR16 (cadena de longitud
    fija embebida en el propio byte de tipo)."""
    raw_type = buf[offset]
    offset += 1
    compact_name = bool(raw_type & 0x80)
    tagtype = (raw_type & 0x7F) if compact_name else raw_type

    if compact_name:
        name_id = buf[offset]
        offset += 1
        name = None
    else:
        namelen = struct.unpack_from("<H", buf, offset)[0]
        offset += 2
        if namelen == 1:
            name_id = buf[offset]
            offset += 1
            name = None
        else:
            name = buf[offset:offset + namelen].decode("utf-8", errors="replace")
            offset += namelen
            name_id = None

    if TAGTYPE_STR1 <= tagtype <= TAGTYPE_STR1 + 15:
        strlen = tagtype - 0x10
        value = buf[offset:offset + strlen].decode("utf-8", errors="replace")
        offset += strlen
    elif tagtype == TAGTYPE_STRING:
        strlen = struct.unpack_from("<H", buf, offset)[0]
        offset += 2
        value = buf[offset:offset + strlen].decode("utf-8", errors="replace")
        offset += strlen
    elif tagtype == TAGTYPE_UINT64:
        value = struct.unpack_from("<Q", buf, offset)[0]
        offset += 8
    elif tagtype == TAGTYPE_UINT32:
        value = struct.unpack_from("<I", buf, offset)[0]
        offset += 4
    elif tagtype == TAGTYPE_FLOAT32:
        value = struct.unpack_from("<f", buf, offset)[0]
        offset += 4
    elif tagtype == TAGTYPE_UINT16:
        value = struct.unpack_from("<H", buf, offset)[0]
        offset += 2
    elif tagtype == TAGTYPE_UINT8:
        value = buf[offset]
        offset += 1
    elif tagtype == TAGTYPE_HASH16:
        value = buf[offset:offset + 16]
        offset += 16
    elif tagtype == TAGTYPE_BLOB:
        bloblen = struct.unpack_from("<I", buf, offset)[0]
        offset += 4
        value = buf[offset:offset + bloblen]
        offset += bloblen
    elif tagtype == TAGTYPE_BSOB:
        bsoblen = buf[offset]
        offset += 1
        value = buf[offset:offset + bsoblen]
        offset += bsoblen
    else:
        raise ValueError(f"read_tag: tipo de tag desconocido {tagtype:#x}")

    return Tag(name_id=name_id, name=name, type=tagtype, value=value), offset


# --------------------------------------------------------------------------
# Framing de paquetes TCP y UDP (Packet.h/Packet.cpp)
# --------------------------------------------------------------------------

def build_tcp_packet(protocol: int, opcode: int, payload: bytes) -> bytes:
    body = bytes([opcode]) + payload
    return bytes([protocol]) + struct.pack("<I", len(body)) + body


async def read_tcp_packet(reader: asyncio.StreamReader, timeout: float | None = None,
                            protocol: int | None = None) -> tuple[int, int, bytes]:
    """`protocol`, si se indica, es un byte de protocolo ya leído del
    socket de antemano (caso de `_maybe_negotiate_incoming_obfuscation`,
    que necesita mirar ese primer byte para decidir si la conexión
    entrante viene ofuscada antes de que nada más lea del socket)."""
    if protocol is None:
        header = await asyncio.wait_for(reader.readexactly(5), timeout=timeout)
        protocol = header[0]
        length = struct.unpack_from("<I", header, 1)[0]
    else:
        length = struct.unpack_from("<I", await asyncio.wait_for(reader.readexactly(4), timeout=timeout), 0)[0]
    body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
    opcode = body[0]
    payload = body[1:]
    if protocol == OP_PACKEDPROT:
        payload = zlib.decompress(payload)
    return protocol, opcode, payload


def build_udp_packet(protocol: int, opcode: int, payload: bytes) -> bytes:
    return bytes([protocol, opcode]) + payload


def parse_udp_packet(data: bytes) -> tuple[int, int, bytes]:
    protocol = data[0]
    opcode = data[1]
    payload = data[2:]
    if protocol == OP_KADEMLIAPACKEDPROT:
        payload = zlib.decompress(payload)
    return protocol, opcode, payload


# --------------------------------------------------------------------------
# Cliente HTTP(S) mínimo (mismo patrón que el resto del proyecto, sin
# librerías externas) para descargar server.met/nodes.dat.
# --------------------------------------------------------------------------

async def _http_get(url: str, timeout: float = 15.0, _redirects: int = 3,
                     proxy: ProxyConfig | None = None) -> bytes:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    ssl_context = ssl.create_default_context() if parsed.scheme == "https" else None
    reader, writer = await proxy_open_connection(host, port, proxy=proxy, ssl=ssl_context, timeout=timeout)
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
    headers, _, body = raw.partition(b"\r\n\r\n")
    status_line = headers.split(b"\r\n", 1)[0]

    if _redirects > 0 and (b" 301 " in status_line or b" 302 " in status_line or b" 303 " in status_line or b" 307 " in status_line):
        for line in headers.split(b"\r\n"):
            if line.lower().startswith(b"location:"):
                location = line.split(b":", 1)[1].strip().decode("utf-8", errors="replace")
                return await _http_get(location, timeout=timeout, _redirects=_redirects - 1, proxy=proxy)

    if b"transfer-encoding: chunked" in headers.lower():
        body = _dechunk(body)
    return body


def _dechunk(data: bytes) -> bytes:
    out = bytearray()
    while data:
        line_end = data.find(b"\r\n")
        if line_end == -1:
            break
        try:
            size = int(data[:line_end].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        chunk_start = line_end + 2
        out += data[chunk_start:chunk_start + size]
        data = data[chunk_start + size + 2:]
    return bytes(out)


# --------------------------------------------------------------------------
# server.met / nodes.dat (formatos binarios de ServerList.cpp / RoutingZone.cpp)
# --------------------------------------------------------------------------

def _read_wire_ip(data: bytes, offset: int) -> str:
    """El eD2k guarda las IP como un uint32 little-endian cuyo byte MENOS
    significativo es el PRIMER octeto de la IP (así se ve en el propio
    ID de cliente: HighID = IP real como uint32 LE). Por eso, leyendo
    los 4 bytes tal cual aparecen en el cable y uniéndolos con puntos ya
    se obtiene la dirección correcta, sin invertir nada.

    Es el único punto de lectura de direcciones del backend (lo usan
    `parse_nodes_dat`, la lista de contactos Kad de KADEMLIA2_RES/
    KADEMLIA2_BOOTSTRAP_RES, la lista de servidores que el propio
    servidor eD2k va "soplando" en caliente, y OP_CALLBACKREQUESTED),
    y en los cuatro bytes fijos: igual que `client_id`
    (`_build_hello_payload`), TAG_SOURCEIP en las respuestas de fuentes
    Kad y el payload de KADEMLIA_FIREWALLED_RES, es un límite real del
    protocolo eD2k/Kad -ningún cliente real (eMule, aMule) tiene jamás
    una variante de campo para IPv6-, no una carencia de esta
    implementación."""
    return ".".join(str(b) for b in data[offset:offset + 4])


def parse_ed2k_link(link: str) -> tuple[str, int, str] | None:
    """Parsea un enlace `ed2k://|file|nombre|tamaño|hash|/` (formato
    estándar, el mismo que genera eMule/aMule al compartir un enlace).
    Devuelve `(título, tamaño_bytes, hash_hex)` listo para construir el
    `source_id` que espera `EMuleBackend.start_download` (ver ahí:
    `f"{hash_hex}:::{size}:::{title}"`), o `None` si no es un enlace
    ed2k de fichero válido. No hace falta buscar antes: el hash MD4 ya
    es autosuficiente para pedirle fuentes al servidor/Kad."""
    parts = link.strip().split("|")
    if len(parts) < 5 or parts[0].lower() != "ed2k://" or parts[1].lower() != "file":
        return None
    title = unquote(parts[2])
    try:
        size = int(parts[3])
        file_hash = bytes.fromhex(parts[4])
    except ValueError:
        return None
    if len(file_hash) != 16 or not title:
        return None
    return title, size, parts[4].lower()


def parse_server_met(data: bytes) -> list[tuple[str, int]]:
    if len(data) < 5 or data[0] != 0xE0:
        return []
    offset = 1
    count = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    servers = []
    try:
        for _ in range(count):
            ip = _read_wire_ip(data, offset)
            offset += 4
            port = struct.unpack_from("<H", data, offset)[0]
            offset += 2
            tagcount = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            for _ in range(tagcount):
                _tag, offset = read_tag(data, offset)
            servers.append((ip, port))
    except (struct.error, IndexError):
        pass  # nos quedamos con lo que se haya podido parsear hasta el corte
    return servers


def parse_nodes_dat(data: bytes) -> list[dict]:
    """Formato real de nodes.dat (RoutingZone.cpp::ReadFile/ReadBootstrapNodesDat).

    Cabecera: uint32(0, señal de "formato nuevo") + fileVersion(4).
      - Si fileVersion == 3 Y el siguiente uint32 (bootstrapEdition) es 1:
        variante "bootstrap" especial (500-1000 nodos solo para mandarles
        un bootstrap, no se guardan en la tabla de rutas): numContacts(4)
        + contactos de 25 bytes (id(16)+ip(4)+udp(2)+tcp(2)+version(1)).
      - Si no, es un nodes.dat normal: numContacts(4) + contactos, donde
        cada contacto lleva 25 bytes si fileVersion==1, o 34 bytes si
        fileVersion>=2 (25 + CKadUDPKey de 8 bytes + verified(1)).
        Confirmado en vivo: la URL real de upd.emule-security.org sirve
        actualmente fileVersion=2, no la variante bootstrap."""
    if len(data) < 12:
        return []
    offset = 0
    zero = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    if zero != 0:
        return []
    file_version = struct.unpack_from("<I", data, offset)[0]
    offset += 4

    contact_size = 25
    if file_version == 3:
        bootstrap_edition = struct.unpack_from("<I", data, offset)[0]
        if bootstrap_edition == 1:
            offset += 4  # es la variante bootstrap: contact_size ya es 25, sin campos extra
        else:
            offset -= 4  # no era bootstrapEdition, sino ya el numContacts: retrocedemos
            contact_size = 34 if file_version >= 2 else 25
    else:
        contact_size = 34 if file_version >= 2 else 25

    num_contacts = struct.unpack_from("<I", data, offset)[0]
    offset += 4

    contacts = []
    try:
        for _ in range(num_contacts):
            kad_id = data[offset:offset + 16]
            offset += 16
            ip = _read_wire_ip(data, offset)
            offset += 4
            udp_port = struct.unpack_from("<H", data, offset)[0]
            offset += 2
            tcp_port = struct.unpack_from("<H", data, offset)[0]
            offset += 2
            version = data[offset]
            offset += 1
            offset += contact_size - 25  # nos saltamos CKadUDPKey(8)+verified(1) si aplica
            contacts.append({"id": kad_id, "ip": ip, "udp_port": udp_port, "tcp_port": tcp_port, "version": version})
    except (struct.error, IndexError):
        pass
    return contacts


# --------------------------------------------------------------------------
# Caché local de servidores eD2k y contactos Kad
#
# eMule/aMule real hace exactamente esto: los servidores nuevos que se
# enteran vía OP_SERVERLIST se añaden a server.met y se guarda en disco
# (ServerSocket.cpp::ProcessPacket -> AddServer + SaveServerMet); los
# contactos Kad que responden de verdad a un paquete nuestro se guardan
# en nodes.dat al desconectar. Aquí replicamos el mismo patrón (y la
# misma implementación ya usada para la caché de hubs de G2 en
# g2_backend.py: JSON en el directorio de configuración, mejor esfuerzo,
# nunca crítico si falla).
# --------------------------------------------------------------------------

_SERVER_CACHE_PATH = _config_dir() / "ed2k_server_cache.json"
_SERVER_CACHE_MAX = 200
_SERVER_CACHE_MAX_FAILS = 5  # a la 5ª conexión fallida seguida, se borra de la caché

_KAD_CACHE_PATH = _config_dir() / "kad_contacts_cache.json"
_KAD_CACHE_MAX = 300
_KAD_CACHE_MAX_FAILS = 5  # a los 5 bootstrap sin respuesta seguidos, se borra de la caché

# Identidad propia (GUID de 16 bytes que nos identifica como cliente
# ante el resto de la red), créditos y amigos (punto 15 del backlog).
# eMule real guarda el GUID propio y ambas listas en preferences.dat/
# clients.met/emfriends.met; aquí, mismo patrón JSON que el resto de
# cachés de este backend. El GUID propio se genera una sola vez y se
# reutiliza siempre después, para que los peers reales con los que
# hablamos puedan reconocernos de una sesión a otra y acumular crédito
# sobre nosotros correctamente (si generáramos uno nuevo cada vez,
# seríamos siempre "un desconocido" a ojos de los demás).
_IDENTITY_PATH = _config_dir() / "ed2k_identity.json"
_CREDITS_CACHE_PATH = _config_dir() / "ed2k_credits.json"
_FRIENDS_CACHE_PATH = _config_dir() / "ed2k_friends.json"


def _load_server_cache_raw() -> list[dict]:
    try:
        with open(_SERVER_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = []
        for e in data:
            h, p, fails = e.get("host"), e.get("port"), e.get("fails", 0)
            if isinstance(h, str) and isinstance(p, int):
                out.append({"host": h, "port": p, "fails": fails})
        return out
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _save_server_cache_raw(entries: list[dict]) -> None:
    try:
        _SERVER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SERVER_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(entries[:_SERVER_CACHE_MAX], f)
    except OSError:
        pass


def load_server_cache() -> list[tuple[str, int]]:
    """Servidores eD2k a los que ya nos conectamos o que nos llegaron
    por OP_SERVERLIST en sesiones anteriores. Lista vacía si no hay
    caché todavía o está corrupta (nunca lanza)."""
    return [(e["host"], e["port"]) for e in _load_server_cache_raw()]


def save_server_cache(servers: list[tuple[str, int]]) -> None:
    """Guarda hasta _SERVER_CACHE_MAX servidores (sin duplicados, se
    conserva el orden recibido) para la próxima sesión. Los que ya
    estaban en la caché conservan su contador de fallos; los nuevos
    empiezan en 0. Falla en silencio si no se puede escribir: no es
    crítico, solo acelera el próximo connect_auto()."""
    old_fails = {(e["host"], e["port"]): e["fails"] for e in _load_server_cache_raw()}
    seen: set[tuple[str, int]] = set()
    deduped = []
    for h, p in servers:
        if (h, p) not in seen:
            seen.add((h, p))
            deduped.append({"host": h, "port": p, "fails": old_fails.get((h, p), 0)})
    _save_server_cache_raw(deduped)


def record_server_failure(host: str, port: int) -> None:
    """Cuenta un intento de conexión fallido contra un servidor que
    estaba en la caché local. Al llegar a _SERVER_CACHE_MAX_FAILS
    fallos seguidos se borra de la caché. No hace nada si el servidor
    no estaba en la caché (p.ej. venía de server.met). Una conexión
    lograda (_remember_server) resetea el contador a 0."""
    raw = _load_server_cache_raw()
    new_raw = []
    changed = False
    for e in raw:
        if (e["host"], e["port"]) == (host, port):
            changed = True
            fails = e["fails"] + 1
            if fails >= _SERVER_CACHE_MAX_FAILS:
                continue
            e = {"host": e["host"], "port": e["port"], "fails": fails}
        new_raw.append(e)
    if changed:
        _save_server_cache_raw(new_raw)


def _load_kad_cache_raw() -> list[dict]:
    try:
        with open(_KAD_CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [
            {
                "id": bytes.fromhex(c["id"]),
                "ip": c["ip"],
                "udp_port": c["udp_port"],
                "tcp_port": c["tcp_port"],
                "version": c["version"],
                "fails": c.get("fails", 0),
            }
            for c in raw
        ]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def _save_kad_cache_raw(contacts: list[dict]) -> None:
    try:
        _KAD_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_KAD_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "id": c["id"].hex(),
                        "ip": c["ip"],
                        "udp_port": c["udp_port"],
                        "tcp_port": c["tcp_port"],
                        "version": c["version"],
                        "fails": c["fails"],
                    }
                    for c in contacts[:_KAD_CACHE_MAX]
                ],
                f,
            )
    except OSError:
        pass


def load_kad_cache() -> list[dict]:
    """Contactos Kad que en una sesión anterior llegaron a respondernos
    de verdad (ver EMuleBackend._mark_kad_contact_confirmed), más
    fiables que una entrada sin contrastar de nodes.dat. Lista vacía si
    no hay caché todavía o está corrupta."""
    return [{k: v for k, v in c.items() if k != "fails"} for c in _load_kad_cache_raw()]


def save_kad_cache(contacts: list[dict]) -> None:
    """Guarda hasta _KAD_CACHE_MAX contactos confirmados (sin
    duplicados, los más recientes primero), fusionados con los que ya
    hubiera en la caché de sesiones anteriores y no se hayan
    reconfirmado esta vez (que conservan su contador de fallos). Los
    recién confirmados resetean su contador a 0. Falla en silencio si
    no se puede escribir."""
    seen: set[bytes] = set()
    deduped = []
    for c in contacts:
        if c["id"] not in seen:
            seen.add(c["id"])
            deduped.append({**c, "fails": 0})
    for e in _load_kad_cache_raw():
        if e["id"] not in seen:
            seen.add(e["id"])
            deduped.append(e)
    _save_kad_cache_raw(deduped)


def record_kad_failures(contact_ids: list[bytes]) -> None:
    """Cuenta un bootstrap sin respuesta contra cada contacto que
    estuviera en la caché local. Al llegar a _KAD_CACHE_MAX_FAILS
    fallos seguidos, ese contacto se borra de la caché. No hace nada
    con los IDs que no estuvieran ya en la caché (p.ej. venían de
    nodes.dat). Una respuesta confirmada (save_kad_cache) resetea el
    contador a 0."""
    if not contact_ids:
        return
    ids = set(contact_ids)
    raw = _load_kad_cache_raw()
    new_raw = []
    changed = False
    for c in raw:
        if c["id"] in ids:
            changed = True
            fails = c["fails"] + 1
            if fails >= _KAD_CACHE_MAX_FAILS:
                continue
            c = {**c, "fails": fails}
        new_raw.append(c)
    if changed:
        _save_kad_cache_raw(new_raw)


def load_or_create_identity() -> bytes:
    """GUID propio de 16 bytes, generado una única vez y reutilizado en
    todas las sesiones siguientes (ver comentario junto a
    `_IDENTITY_PATH`)."""
    try:
        with open(_IDENTITY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        user_hash = bytes.fromhex(data["user_hash"])
        if len(user_hash) == 16:
            return user_hash
    except (OSError, ValueError, TypeError, KeyError):
        pass
    user_hash = os.urandom(16)
    try:
        _IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_IDENTITY_PATH, "w", encoding="utf-8") as f:
            json.dump({"user_hash": user_hash.hex()}, f)
    except OSError:
        pass
    return user_hash


def load_credits() -> dict[bytes, dict]:
    """Créditos acumulados con cada peer remoto que hayamos identificado
    (por su userhash) en una subida o bajada real, indexados por
    userhash. Cada entrada: {"nick", "uploaded", "downloaded"} en bytes
    totales histórico. Vacío si no hay caché todavía o está corrupta."""
    try:
        with open(_CREDITS_CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {
            bytes.fromhex(user_hash_hex): {
                "nick": e.get("nick") or "",
                "uploaded": int(e.get("uploaded", 0)),
                "downloaded": int(e.get("downloaded", 0)),
            }
            for user_hash_hex, e in raw.items()
        }
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def save_credits(credits: dict[bytes, dict]) -> None:
    try:
        _CREDITS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CREDITS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({uh.hex(): e for uh, e in credits.items()}, f)
    except OSError:
        pass


def credit_modifier(entry: dict | None) -> float:
    """Aproximación al sistema de créditos real de eMule (documentado
    públicamente en su FAQ/wiki, sin copiar código fuente): cuanto más
    nos ha subido un peer respecto a lo que nosotros le hemos subido a
    él, mayor prioridad recibe al pedirnos algo, con un tope de x10 (el
    mismo límite del eMule real). Si no nos ha dado más de lo que le
    hemos dado (incluido el caso sin historial), el modificador es 1
    (prioridad normal, sin bonus)."""
    if not entry:
        return 1.0
    uploaded = entry.get("uploaded", 0)
    downloaded = entry.get("downloaded", 0)
    if downloaded <= uploaded:
        return 1.0
    return min(downloaded / max(uploaded, 1), 10.0)


def load_friends() -> dict[bytes, str]:
    """Lista de amigos (userhash -> nick), a quienes se da siempre la
    máxima prioridad en la cola de subida propia, por delante de
    cualquier modificador de créditos (igual que el eMule real)."""
    try:
        with open(_FRIENDS_CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {bytes.fromhex(user_hash_hex): nick for user_hash_hex, nick in raw.items()}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def save_friends(friends: dict[bytes, str]) -> None:
    try:
        _FRIENDS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_FRIENDS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({uh.hex(): nick for uh, nick in friends.items()}, f)
    except OSError:
        pass


def _parse_hello_payload(payload: bytes) -> dict | None:
    """Parsea el payload de un OP_HELLO/OP_HELLOANSWER entrante (mismo
    formato que construye `_build_hello_payload`: el byte 0x10 de
    longitud de hash solo está presente en OP_HELLO, no en
    OP_HELLOANSWER) para sacar el userhash estable del cliente remoto y
    su nick — la identificación que usa el sistema de créditos/amigos
    (punto 15 del backlog). None si el payload es demasiado corto o no
    tiene la forma esperada."""
    offset = 1 if payload[:1] == b"\x10" else 0
    if len(payload) < offset + 16 + 4 + 2 + 4:
        return None
    user_hash = payload[offset:offset + 16]
    offset += 16 + 4 + 2  # nos saltamos client_id(4) y listen_port(2), no hacen falta aquí
    tag_count = struct.unpack_from("<I", payload, offset)[0]
    offset += 4
    nick = None
    for _ in range(tag_count):
        try:
            tag, offset = read_tag(payload, offset)
        except (struct.error, IndexError, ValueError):
            break
        if tag.name_id == CT_NAME and isinstance(tag.value, str):
            nick = tag.value
    return {"user_hash": user_hash, "nick": nick}


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------

class _KadProtocol(asyncio.DatagramProtocol):
    def __init__(self, backend: "EMuleBackend") -> None:
        self._backend = backend

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self._backend._udp_transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._backend._on_kad_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        pass


class EMuleBackend(NetworkBackend):
    network = Network.EMULE

    def __init__(self, nickname: str = "P2PTotalUser", listen_port: int = 4662, kad_udp_port: int = 4672,
                 shared_library: SharedLibrary | None = None,
                 proxy: ProxyConfig | None = None, obfuscation: str = "enabled") -> None:
        self._nickname = nickname
        self._listen_port = listen_port
        self._kad_udp_port = kad_udp_port
        self._shared_library = shared_library
        self._proxy = proxy
        self._active_uploads = 0
        # Ofuscación de protocolo (punto 20): "disabled"/"enabled"/"required",
        # ver comentario junto a EMuleConfig.obfuscation en core/config.py.
        # `_known_peer_hashes` es la caché "aprendida sobre la marcha" de
        # userhashes de peers con los que ya hicimos un HELLO/HELLOANSWER
        # alguna vez: la necesitamos para poder ofrecer ofuscación en
        # conexiones salientes (el handshake exige conocer de antemano el
        # userhash del otro lado), igual que hace el eMule real con su
        # lista de "known clients".
        self._obfuscation = obfuscation
        self._known_peer_hashes: dict[tuple[str, int], bytes] = {}

        self._user_hash = load_or_create_identity()
        self._kad_id = os.urandom(16)

        # Créditos y amigos (punto 15 del backlog): ver comentario junto
        # a `_IDENTITY_PATH` y `credit_modifier()`.
        self._credits: dict[bytes, dict] = load_credits()
        self._credits_dirty = False
        self._friends: dict[bytes, str] = load_friends()
        self._upload_slots = DEFAULT_UPLOAD_SLOTS
        self._upload_slots_used = 0
        self._upload_waiting: list[dict] = []

        self._server_reader: asyncio.StreamReader | None = None
        self._server_writer: asyncio.StreamWriter | None = None
        self._server_task: asyncio.Task | None = None
        self._client_id = 0
        self._server_host: str | None = None
        self._server_port: int | None = None

        self._tcp_server: asyncio.AbstractServer | None = None
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._kad_contacts: dict[bytes, dict] = {}
        self._kad_confirmed: dict[bytes, dict] = {}
        self._discovered_servers: set[tuple[str, int]] = set()
        self._kad_search_results: dict[bytes, list[tuple[bytes, list[Tag]]]] = {}

        self._collecting_search = False
        self._pending_search_results: list[tuple[bytes, int, int, list[Tag]]] = []
        self._search_max_results: int | None = None
        self._search_limit_event: asyncio.Event | None = None
        self._sources_future: asyncio.Future | None = None
        self._sources_target_hash: bytes | None = None
        self._pending_callback: dict | None = None
        self._firewall_check_future: asyncio.Future | None = None
        self._kad_firewalled: bool | None = None

        self._active: dict[str, dict] = {}
        self._progress_callback: Callable[[Download], None] | None = None
        self._debug = False

    # -- ciclo de vida ----------------------------------------------------

    async def connect(self) -> None:
        """Arranca el listener TCP (modo activo, para recibir conexiones
        de vuelta vía OP_CALLBACKREQUEST) y el socket UDP de Kad. No
        inicia sesión en ningún servidor eD2k todavía: eso lo hacen
        connect_to_server()/connect_auto(), igual que en DC++/G2."""
        if self._shared_library is not None:
            # need_sha1=True, need_ed2k=True (los valores por defecto,
            # ver SharedLibrary.rescan): a diferencia de Soulseek/DC++,
            # eMule sí necesita el hash eD2k de cada fichero para
            # identificarlo ante otros clientes y el servidor (el SHA1 no
            # le hace falta, pero es barato calcularlo en la misma
            # pasada de lectura). En segundo plano sin esperar (ver
            # SharedLibrary.ensure_scanning): eD2k es, con diferencia, el
            # hash más lento de calcular (MD4 puro Python) -para una
            # biblioteca compartida grande, el primer hasheo completo
            # puede tardar horas- así que connect() no se queda esperando
            # a que termine; el servidor eD2k/Kad queda operativo de
            # inmediato y el índice de ficheros compartidos se va
            # completando poco a poco mientras tanto.
            self._shared_library.ensure_scanning()
        if self._tcp_server is None:
            self._tcp_server = await asyncio.start_server(self._handle_incoming_peer, "0.0.0.0", self._listen_port)
        if self._udp_transport is None:
            loop = asyncio.get_event_loop()
            await loop.create_datagram_endpoint(
                lambda: _KadProtocol(self), local_addr=("0.0.0.0", self._kad_udp_port)
            )
        # Best-effort: abre en el router tanto el puerto TCP de cliente
        # como el UDP de Kad, si tiene UPnP. Nunca bloquea ni falla el
        # connect().
        asyncio.ensure_future(upnp.add_port_mapping(self._listen_port, "TCP", "P2P Total - eMule"))
        asyncio.ensure_future(upnp.add_port_mapping(self._kad_udp_port, "UDP", "P2P Total - Kad"))

    async def disconnect(self) -> None:
        if self._discovered_servers:
            existing = [hp for hp in load_server_cache() if hp not in self._discovered_servers]
            save_server_cache(list(self._discovered_servers) + existing)
        if self._kad_confirmed:
            save_kad_cache(list(self._kad_confirmed.values()))
        if self._server_task:
            self._server_task.cancel()
            self._server_task = None
        if self._server_writer:
            self._server_writer.close()
            self._server_writer = None
        if self._tcp_server:
            self._tcp_server.close()
            self._tcp_server = None
            asyncio.ensure_future(upnp.delete_port_mapping(self._listen_port, "TCP"))
        if self._udp_transport:
            self._udp_transport.close()
            self._udp_transport = None
            asyncio.ensure_future(upnp.delete_port_mapping(self._kad_udp_port, "UDP"))
        self._active.clear()
        self._flush_credits()

    async def is_connected(self) -> bool:
        return self._server_writer is not None

    # -- servidor eD2k ------------------------------------------------------

    async def discover_servers(self, timeout: float = 15.0) -> list[tuple[str, int]]:
        data = await _http_get(SERVER_MET_URL, timeout=timeout, proxy=self._proxy)
        return parse_server_met(data)

    @property
    def discovered_servers(self) -> set[tuple[str, int]]:
        """Servidores eD2k que el propio servidor al que estamos
        conectados nos ha ido soplando durante la sesión actual vía
        OP_SERVERLIST (igual que hace un cliente eMule/aMule real).
        Se guardan en disco al desconectar (ver disconnect())."""
        return set(self._discovered_servers)

    def _remember_server(self, host: str, port: int) -> None:
        """Una conexión lograda resetea a 0 el contador de fallos del
        servidor (ver record_server_failure)."""
        raw = [e for e in _load_server_cache_raw() if (e["host"], e["port"]) != (host, port)]
        _save_server_cache_raw([{"host": host, "port": port, "fails": 0}] + raw)

    async def connect_to_server(self, host: str, port: int, timeout: float = 15.0, debug: bool = False) -> None:
        reader, writer = await proxy_open_connection(host, port, proxy=self._proxy, timeout=timeout)
        self._debug = debug

        tags = (
            write_tag(CT_NAME, TAGTYPE_STRING, self._nickname)
            + write_tag(CT_VERSION, TAGTYPE_UINT32, EDONKEYVERSION)
            + write_tag(CT_SERVER_FLAGS, TAGTYPE_UINT32, SRVCAP_ZLIB | SRVCAP_NEWTAGS | SRVCAP_UNICODE | SRVCAP_LARGEFILES)
            + write_tag(CT_EMULE_VERSION, TAGTYPE_UINT32, 0x3D0602)
        )
        # OJO: el conteo de tags del login es un uint32 crudo (caso
        # especial hardcodeado solo aquí), no el uint8 habitual del
        # resto de paquetes con listas de tags.
        payload = self._user_hash + struct.pack("<I", 0) + struct.pack("<H", self._listen_port) + struct.pack("<I", 4) + tags
        writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_LOGINREQUEST, payload))
        await writer.drain()

        deadline = time.monotonic() + timeout
        logged_in = False
        while time.monotonic() < deadline:
            protocol, opcode, payload = await read_tcp_packet(reader, timeout=deadline - time.monotonic())
            if debug:
                print(f"  [debug] servidor -> opcode {opcode:#x} ({len(payload)} bytes)")
            if opcode == OP_REJECT:
                writer.close()
                raise ConnectionError("Servidor eD2k rechazó el login")
            if opcode == OP_IDCHANGE:
                self._client_id = struct.unpack_from("<I", payload, 0)[0]
                logged_in = True
                break
            if opcode == OP_SERVERMESSAGE and debug:
                msglen = struct.unpack_from("<H", payload, 0)[0]
                print(f"  [debug] mensaje del servidor: {payload[2:2 + msglen].decode('utf-8', errors='replace')!r}")

        if not logged_in:
            writer.close()
            raise ConnectionError("El servidor no confirmó el login (sin OP_IDCHANGE) a tiempo")

        self._server_reader = reader
        self._server_writer = writer
        self._server_host = host
        self._server_port = port
        self._server_task = asyncio.create_task(self._server_read_loop())
        self._remember_server(host, port)

        if self._shared_library is not None and self._shared_library.enabled:
            offer = _build_offer_files_payload(
                self._shared_library.list_files(), self._client_id, self._listen_port
            )
            await self._send_server_packet(OP_OFFERFILES, offer)

    async def connect_auto(self, timeout_per_server: float = 8.0, debug: bool = False) -> tuple[str, int]:
        cached = load_server_cache()
        if cached:
            if debug:
                print(f"  [debug] probando {len(cached)} servidor(es) de la caché local (sesiones anteriores)...")
            for host, port in cached:
                try:
                    await self.connect_to_server(host, port, timeout=timeout_per_server, debug=debug)
                    return host, port
                except (OSError, asyncio.TimeoutError, ConnectionError):
                    record_server_failure(host, port)
                    continue
            if debug:
                print("  [debug] ningún servidor de la caché local respondió; consultando server.met...")

        servers = await self.discover_servers()
        if not servers:
            if cached:
                raise ConnectionError(f"Ninguno de los {len(cached)} servidor(es) de la caché respondió, "
                                       f"y no se pudo descargar/parsear server.met")
            raise ConnectionError("No se pudo descargar/parsear server.met para descubrir servidores eD2k")
        cached_set = set(cached)
        tried = len(cached)
        for host, port in servers[:30]:
            if (host, port) in cached_set:
                continue  # ya lo probamos arriba
            tried += 1
            try:
                await self.connect_to_server(host, port, timeout=timeout_per_server, debug=debug)
                return host, port
            except (OSError, asyncio.TimeoutError, ConnectionError):
                continue
        raise ConnectionError(f"Ninguno de los {tried} servidor(es) candidato(s) respondió")

    @property
    def is_high_id(self) -> bool:
        return self._client_id >= HIGH_ID_THRESHOLD

    async def _send_server_packet(self, opcode: int, payload: bytes) -> None:
        self._server_writer.write(build_tcp_packet(OP_EDONKEYPROT, opcode, payload))
        await self._server_writer.drain()

    async def _server_read_loop(self) -> None:
        try:
            while True:
                protocol, opcode, payload = await read_tcp_packet(self._server_reader)
                if self._debug:
                    print(f"  [debug] servidor -> opcode {opcode:#x} ({len(payload)} bytes)")
                if opcode == OP_SEARCHRESULT and self._collecting_search:
                    self._pending_search_results.extend(_parse_search_result(payload))
                    if (self._search_limit_event is not None and self._search_max_results is not None
                            and len(self._pending_search_results) >= self._search_max_results):
                        self._search_limit_event.set()
                elif opcode == OP_FOUNDSOURCES:
                    self._on_found_sources(payload)
                elif opcode == OP_IDCHANGE:
                    self._client_id = struct.unpack_from("<I", payload, 0)[0]
                elif opcode == OP_SERVERLIST:
                    self._on_server_list(payload)
                elif opcode == OP_CALLBACKREQUESTED:
                    asyncio.ensure_future(self._serve_via_callback(payload))
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass

    def _on_server_list(self, payload: bytes) -> None:
        """OP_SERVERLIST (ServerSocket.cpp::ProcessPacket, case
        OP_SERVERLIST): count(uint8) + count x [ip(4) + port(uint16)].
        El servidor real nos va "soplando" otros servidores que conoce
        durante la sesión, igual que hace el propio aMule (que los
        añade a server.met en caliente); aquí los acumulamos en
        self._discovered_servers y se persisten al desconectar."""
        if not payload or payload[0] * 6 + 1 > len(payload):
            return
        count = payload[0]
        offset = 1
        for _ in range(count):
            ip = _read_wire_ip(payload, offset)
            offset += 4
            port = struct.unpack_from("<H", payload, offset)[0]
            offset += 2
            self._discovered_servers.add((ip, port))

    # -- Kad ------------------------------------------------------------

    async def discover_kad_nodes(self, timeout: float = 15.0) -> list[dict]:
        data = await _http_get(NODES_DAT_URL, timeout=timeout, proxy=self._proxy)
        return parse_nodes_dat(data)

    async def connect_kad(self, timeout: float = 8.0, debug: bool = False) -> int:
        """Bootstrap best-effort: carga la caché local de contactos
        confirmados de sesiones anteriores, descarga nodes.dat, manda
        KADEMLIA2_BOOTSTRAP_REQ a un puñado de contactos (empezando por
        los de la caché, más fiables que un nodes.dat sin contrastar) y
        espera sus respuestas (que rellenan self._kad_contacts). No es
        crítico si falla — la búsqueda/descarga puede seguir
        funcionando solo con el servidor eD2k."""
        if self._udp_transport is None:
            await self.connect()

        cached = load_kad_cache()
        for c in cached:
            self._kad_contacts[c["id"]] = c
        if debug and cached:
            print(f"  [debug] Kad: {len(cached)} contacto(s) de la caché local (sesiones anteriores)")

        try:
            nodes = await self.discover_kad_nodes(timeout=timeout)
        except (OSError, asyncio.TimeoutError, ValueError) as e:
            nodes = []
            if debug:
                print(f"  [debug] no se pudo descargar nodes.dat: {e}")

        for c in nodes:
            self._kad_contacts[c["id"]] = c

        if not self._kad_contacts:
            return 0

        targets = list(self._kad_contacts.values())[:25]
        for c in targets:
            packet = build_udp_packet(OP_KADEMLIAHEADER, KADEMLIA2_BOOTSTRAP_REQ, b"")
            self._udp_transport.sendto(packet, (c["ip"], c["udp_port"]))
        await asyncio.sleep(timeout)

        # Contactos de la caché a los que preguntamos y no respondieron:
        # cuenta como un fallo (ver record_kad_failures), para acabar
        # borrándolos de la caché si dejan de estar vivos.
        record_kad_failures([c["id"] for c in targets if c["id"] not in self._kad_confirmed])

        if debug:
            print(f"  [debug] Kad: {len(self._kad_contacts)} contacto(s) conocidos, "
                  f"{len(self._kad_confirmed)} confirmado(s) vivo(s) tras el bootstrap")

        if self._kad_confirmed:
            await self.check_kad_firewall()
            if debug:
                print(f"  [debug] Kad: estado firewalled = {self._kad_firewalled!r}")

        return len(self._kad_contacts)

    def _on_kad_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            protocol, opcode, payload = parse_udp_packet(data)
        except (zlib.error, IndexError):
            return
        self._mark_kad_contact_confirmed(addr)
        try:
            if opcode == KADEMLIA2_BOOTSTRAP_RES:
                # myID(16) + myTCPport(2) + myKadVersion(1) + numContacts(uint16, OJO: 2 bytes aquí)
                self._parse_kad_contact_list(payload, offset=16 + 2 + 1, count_size=2)
            elif opcode == KADEMLIA2_RES:
                # target(16, echoed) + count(uint8, 1 byte -- distinto del bootstrap)
                self._parse_kad_contact_list(payload, offset=16, count_size=1)
            elif opcode == KADEMLIA2_SEARCH_RES:
                self._on_kad_search_res(payload)
            elif opcode == KADEMLIA_FIREWALLED_REQ:
                self._on_kad_firewalled_req(payload, addr)
            elif opcode == KADEMLIA_FIREWALLED_ACK_RES:
                if self._firewall_check_future is not None and not self._firewall_check_future.done():
                    self._firewall_check_future.set_result(False)
            elif opcode == KADEMLIA_FIREWALLED_RES:
                if self._firewall_check_future is not None and not self._firewall_check_future.done():
                    self._firewall_check_future.set_result(True)
        except (struct.error, IndexError):
            pass

    def _mark_kad_contact_confirmed(self, addr: tuple[str, int]) -> None:
        """Cualquier datagrama Kad válido que nos llega demuestra que su
        contacto de origen está vivo *ahora mismo* -- mucho más fiable
        que una entrada sin contrastar de nodes.dat o de la caché
        local. self._kad_confirmed es lo único que se persiste a la
        caché al desconectar (ver disconnect())."""
        ip, port = addr
        for contact in self._kad_contacts.values():
            if contact["ip"] == ip and contact["udp_port"] == port:
                self._kad_confirmed[contact["id"]] = contact
                return


    def _on_kad_firewalled_req(self, payload: bytes, addr: tuple[str, int]) -> None:
        """KADEMLIA_FIREWALLED_REQ (0x50): <TCPPORT (remitente) 2>. Otro
        nodo Kad nos pide que comprobemos si su puerto TCP es
        alcanzable desde fuera -- somos nosotros quien hace de
        "verificador" para él, el mismo papel que jugaría cualquier
        nodo Kad real al recibirlo."""
        if len(payload) < 2:
            return
        tcp_port = struct.unpack_from("<H", payload, 0)[0]
        asyncio.ensure_future(self._answer_kad_firewalled_req(addr, tcp_port))

    async def _answer_kad_firewalled_req(self, addr: tuple[str, int], tcp_port: int) -> None:
        ip, _udp_port = addr
        if self._udp_transport is None:
            return
        try:
            _, writer = await proxy_open_connection(ip, tcp_port, proxy=self._proxy, timeout=8.0)
            writer.close()
            opcode, response_payload = KADEMLIA_FIREWALLED_ACK_RES, b""
        except (asyncio.TimeoutError, OSError):
            opcode, response_payload = KADEMLIA_FIREWALLED_RES, socket.inet_aton(ip)
        self._udp_transport.sendto(build_udp_packet(OP_KADEMLIAHEADER, opcode, response_payload), addr)

    async def check_kad_firewall(self, timeout: float = 8.0) -> bool | None:
        """Manda KADEMLIA_FIREWALLED_REQ (0x50) a un contacto Kad
        confirmado para que intente abrir él una conexión TCP de
        vuelta a nuestro puerto de escucha: si lo consigue nos
        responde KADEMLIA_FIREWALLED_ACK_RES (no estamos firewalled) y
        si no, KADEMLIA_FIREWALLED_RES (sí lo estamos) -- el
        equivalente Kad del HighID/LowID de eD2k. Devuelve None si no
        hay ningún contacto confirmado al que preguntar, o si no
        responde a tiempo."""
        if not self._kad_confirmed or self._udp_transport is None:
            return None
        contact = next(iter(self._kad_confirmed.values()))
        fut = asyncio.get_event_loop().create_future()
        self._firewall_check_future = fut
        packet = build_udp_packet(OP_KADEMLIAHEADER, KADEMLIA_FIREWALLED_REQ, struct.pack("<H", self._listen_port))
        self._udp_transport.sendto(packet, (contact["ip"], contact["udp_port"]))
        try:
            self._kad_firewalled = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._kad_firewalled = None
        finally:
            self._firewall_check_future = None
        return self._kad_firewalled

    def _parse_kad_contact_list(self, payload: bytes, offset: int, count_size: int) -> None:
        if count_size == 2:
            count = struct.unpack_from("<H", payload, offset)[0]
        else:
            count = payload[offset]
        offset += count_size
        for _ in range(count):
            contact_id = payload[offset:offset + 16]
            offset += 16
            ip = _read_wire_ip(payload, offset)
            offset += 4
            udp_port = struct.unpack_from("<H", payload, offset)[0]
            offset += 2
            tcp_port = struct.unpack_from("<H", payload, offset)[0]
            offset += 2
            version = payload[offset]
            offset += 1
            self._kad_contacts[contact_id] = {
                "id": contact_id, "ip": ip, "udp_port": udp_port, "tcp_port": tcp_port, "version": version,
            }

    def _on_kad_search_res(self, payload: bytes) -> None:
        offset = 16  # senderID, no lo necesitamos
        target = payload[offset:offset + 16]
        offset += 16
        count = struct.unpack_from("<H", payload, offset)[0]
        offset += 2
        results = []
        for _ in range(count):
            answer = payload[offset:offset + 16]
            offset += 16
            tagcount = payload[offset]
            offset += 1
            tags = []
            for _ in range(tagcount):
                tag, offset = read_tag(payload, offset)
                tags.append(tag)
            results.append((answer, tags))
        self._kad_search_results.setdefault(target, []).extend(results)

    async def _kad_search_keyword(self, keyword: str, timeout: float) -> list[tuple[bytes, list[Tag]]]:
        if not self._kad_contacts:
            return []
        target = md4(keyword.lower().encode("utf-8")).digest()
        payload = target + struct.pack("<H", 0)
        for c in list(self._kad_contacts.values())[:30]:
            packet = build_udp_packet(OP_KADEMLIAHEADER, KADEMLIA2_SEARCH_KEY_REQ, payload)
            self._udp_transport.sendto(packet, (c["ip"], c["udp_port"]))
        await asyncio.sleep(timeout)
        return self._kad_search_results.pop(target, [])

    async def _kad_search_sources(self, file_hash: bytes, size: int, timeout: float) -> list[tuple[str, int]]:
        if not self._kad_contacts:
            return []
        payload = file_hash + struct.pack("<H", 0) + struct.pack("<Q", size)
        for c in list(self._kad_contacts.values())[:30]:
            packet = build_udp_packet(OP_KADEMLIAHEADER, KADEMLIA2_SEARCH_SOURCE_REQ, payload)
            self._udp_transport.sendto(packet, (c["ip"], c["udp_port"]))
        await asyncio.sleep(timeout)
        raw = self._kad_search_results.pop(file_hash, [])

        # Los tags exactos de una respuesta de fuentes no se han podido
        # trazar al 100% en el código real (ver notas de diseño); los
        # interpretamos de forma genérica vía los IDs conocidos de
        # FileTags.h (TAG_SOURCEIP/TAG_SOURCEPORT/TAG_SOURCEUPORT).
        TAG_SOURCEIP, TAG_SOURCEPORT, TAG_SOURCEUPORT = 0xFE, 0xFD, 0xFC
        sources = []
        for _answer, tags in raw:
            ip_val = port_val = None
            for tag in tags:
                if tag.name_id == TAG_SOURCEIP and isinstance(tag.value, int):
                    ip_val = socket.inet_ntoa(struct.pack("<I", tag.value))
                elif tag.name_id in (TAG_SOURCEPORT, TAG_SOURCEUPORT) and isinstance(tag.value, int):
                    port_val = tag.value
            if ip_val and port_val:
                sources.append((ip_val, port_val))
        return sources

    # -- búsqueda ---------------------------------------------------------

    async def search(self, query: str, timeout: float = 15.0, debug: bool = False,
                      max_results: int | None = None, file_type: str | None = None) -> list[SearchResult]:
        if self._server_writer is None:
            raise RuntimeError("Backend eMule no conectado a ningún servidor eD2k")
        self._debug = debug
        self._pending_search_results = []
        self._collecting_search = True
        self._search_max_results = max_results
        self._search_limit_event = asyncio.Event() if max_results else None

        await self._send_server_packet(OP_SEARCHREQUEST, _build_search_request(query, file_type))
        kad_task = asyncio.create_task(self._kad_search_keyword(query, timeout)) if self._kad_contacts else None
        try:
            if self._search_limit_event is not None:
                # Solo la búsqueda por servidor soporta corte anticipado
                # (los resultados llegan uno a uno vía OP_SEARCHRESULT);
                # Kad se resuelve de golpe al final de su propia ventana,
                # así que si el tope ya se alcanzó por servidor no merece
                # la pena esperarla también (se cancela más abajo).
                try:
                    await asyncio.wait_for(self._search_limit_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(timeout)
            self._collecting_search = False

            # Un mismo fichero (mismo hash ed2k) puede aparecer repetido en
            # la respuesta del servidor y, además, ser encontrado también
            # por Kad: en vez de descartar el duplicado (perdiendo el
            # recuento de fuentes que aportaba), se fusiona sumando
            # `seeds_or_sources`, igual que hace la GUI al fusionar
            # resultados repetidos de Soulseek por (título, tamaño).
            by_hash: dict[bytes, SearchResult] = {}
            order: list[bytes] = []

            def add_or_merge(file_hash: bytes, tags: list[Tag]) -> bool:
                """Añade o fusiona un resultado; devuelve True si con él
                ya se ha alcanzado `max_results`."""
                result = _tags_to_search_result(file_hash, tags, self.network)
                existing = by_hash.get(file_hash)
                if existing is not None:
                    existing.seeds_or_sources += result.seeds_or_sources
                    return False
                by_hash[file_hash] = result
                order.append(file_hash)
                return max_results is not None and len(order) >= max_results

            for file_hash, _client_id, _client_port, tags in self._pending_search_results:
                if add_or_merge(file_hash, tags):
                    return [by_hash[h] for h in order]

            if kad_task:
                for answer_hash, tags in await kad_task:
                    if add_or_merge(answer_hash, tags):
                        return [by_hash[h] for h in order]

            return [by_hash[h] for h in order]
        finally:
            self._collecting_search = False
            self._search_limit_event = None
            if kad_task is not None and not kad_task.done():
                kad_task.cancel()

    # -- descarga ---------------------------------------------------------

    async def start_download(self, result: SearchResult, dest_path: str) -> Download:
        if self._server_writer is None:
            raise RuntimeError("Backend eMule no conectado a ningún servidor eD2k")

        file_hash_hex, size_str, title = result.source_id.split(":::", 2)
        file_hash = bytes.fromhex(file_hash_hex)
        size = int(size_str)

        download = Download(
            id=None, network=self.network, title=title, source_id=result.source_id,
            dest_path=dest_path, size_bytes=size, state=DownloadState.SEARCHING_SOURCES,
        )
        self._active[file_hash_hex] = {
            "download": download, "dest_path": dest_path, "file_hash": file_hash,
            "paused": False, "cancelled": False, "writer": None, "limiter": RateLimiter(),
        }
        asyncio.create_task(self._run_download(file_hash, download, dest_path))
        return download

    async def _run_download(self, file_hash: bytes, download: Download, dest_path: str,
                              resume_from: int = 0) -> None:
        candidates: list[tuple[str, int, int]] = []  # (kind, a, b): kind "id"->(client_id,port) / "ip"->(ip,port)

        server_sources = await self._get_sources_from_server(file_hash, timeout=15.0)
        candidates.extend(("id", cid, port) for cid, port in server_sources)

        if self._kad_contacts:
            kad_sources = await self._kad_search_sources(file_hash, download.size_bytes, timeout=15.0)
            candidates.extend(("ip", ip, port) for ip, port in kad_sources)

        if not candidates:
            if download.state in (DownloadState.PAUSED, DownloadState.CANCELLED):
                return  # detenida por el usuario mientras se buscaban fuentes
            download.state = DownloadState.ERROR
            download.error_message = "No se encontraron fuentes (ni por servidor ni por Kad)"
            self._notify(download)
            return

        for kind, a, b in candidates:
            try:
                if kind == "ip":
                    ok = await self._download_from_source(a, b, file_hash, download, dest_path,
                                                            resume_from=resume_from)
                elif a >= HIGH_ID_THRESHOLD:
                    ip = socket.inet_ntoa(struct.pack("<I", a))
                    ok = await self._download_from_source(ip, b, file_hash, download, dest_path,
                                                            resume_from=resume_from)
                else:
                    ok = await self._download_via_callback(a, file_hash, download, dest_path,
                                                             resume_from=resume_from)
            except (OSError, asyncio.TimeoutError, ConnectionError, struct.error, IndexError, EOFError):
                ok = False
            if ok:
                return
            if download.state in (DownloadState.PAUSED, DownloadState.CANCELLED):
                return  # detenida por el usuario, no seguir probando fuentes

        download.state = DownloadState.ERROR
        download.error_message = "Ninguna fuente aceptó la descarga"
        self._notify(download)

    async def _get_sources_from_server(self, file_hash: bytes, timeout: float) -> list[tuple[int, int]]:
        self._sources_future = asyncio.get_event_loop().create_future()
        self._sources_target_hash = file_hash
        await self._send_server_packet(OP_GETSOURCES, file_hash)
        try:
            return await asyncio.wait_for(self._sources_future, timeout=timeout)
        except asyncio.TimeoutError:
            return []
        finally:
            self._sources_future = None

    def _on_found_sources(self, payload: bytes) -> None:
        file_hash = payload[0:16]
        if not self._sources_future or self._sources_future.done() or file_hash != self._sources_target_hash:
            return
        count = payload[16]
        offset = 17
        sources = []
        for _ in range(count):
            cid = struct.unpack_from("<I", payload, offset)[0]
            offset += 4
            cport = struct.unpack_from("<H", payload, offset)[0]
            offset += 2
            sources.append((cid, cport))
        self._sources_future.set_result(sources)

    async def _open_peer_connection(self, ip: str, port: int, timeout: float):
        """Abre una conexión saliente cliente-cliente, negociando
        ofuscación de protocolo (punto 20) si está activada en la
        configuración Y ya conocemos el userhash de ese peer, aprendido
        de un HELLO/HELLOANSWER anterior con esa misma IP:puerto -- el
        handshake de ofuscación exige conocer de antemano el userhash
        del otro lado, así que el primer contacto con un peer nuevo
        siempre se conecta sin ofuscar, igual que hace el eMule real con
        su lista de "known clients" (ver `_known_peer_hashes`). Devuelve
        (reader, writer) normales o, si quedó ofuscada, la misma
        instancia de `_ObfuscatedPeerStream` en ambas posiciones: el
        resto del código no necesita distinguir un caso del otro."""
        reader, writer = await proxy_open_connection(ip, port, proxy=self._proxy, timeout=timeout)
        target_hash = self._known_peer_hashes.get((ip, port)) if self._obfuscation != "disabled" else None
        if target_hash is None:
            return reader, writer
        stream = _ObfuscatedPeerStream(reader, writer)
        await stream.negotiate_outgoing(target_hash, timeout=timeout)
        return stream, stream

    async def _download_from_source(self, ip: str, port: int, file_hash: bytes,
                                      download: Download, dest_path: str, timeout: float = 20.0,
                                      resume_from: int = 0) -> bool:
        try:
            reader, writer = await self._open_peer_connection(ip, port, timeout)
        except (OSError, asyncio.TimeoutError, ObfuscationError, asyncio.IncompleteReadError):
            return False
        entry = self._active.get(file_hash.hex())
        if entry is not None:
            entry["writer"] = writer
        try:
            hello_payload = self._build_hello_payload(is_answer=False)
            writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_HELLO, hello_payload))
            await writer.drain()
            protocol, opcode, payload = await read_tcp_packet(reader, timeout=timeout)
            if opcode != OP_HELLOANSWER:
                return False
            remote_info = _parse_hello_payload(payload)
            if remote_info and remote_info.get("user_hash"):
                self._known_peer_hashes[(ip, port)] = remote_info["user_hash"]
            return await self._client_transfer_loop(reader, writer, file_hash, download, dest_path,
                                                       timeout=30.0, resume_from=resume_from,
                                                       remote_info=remote_info)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ObfuscationError):
            return False
        finally:
            writer.close()

    async def _download_via_callback(self, client_id: int, file_hash: bytes,
                                       download: Download, dest_path: str, timeout: float = 20.0,
                                       resume_from: int = 0) -> bool:
        fut = asyncio.get_event_loop().create_future()
        self._pending_callback = {"future": fut}
        await self._send_server_packet(OP_CALLBACKREQUEST, struct.pack("<I", client_id))
        try:
            reader, writer, remote_info = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending_callback = None
        entry = self._active.get(file_hash.hex())
        if entry is not None:
            entry["writer"] = writer
        try:
            hello_payload = self._build_hello_payload(is_answer=True)
            writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_HELLOANSWER, hello_payload))
            await writer.drain()
            return await self._client_transfer_loop(reader, writer, file_hash, download, dest_path,
                                                       timeout=30.0, resume_from=resume_from,
                                                       remote_info=remote_info)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return False
        finally:
            writer.close()


    async def _serve_via_callback(self, payload: bytes) -> None:
        """OP_CALLBACKREQUESTED (servidor -> cliente, 0x35): <IP 4><PORT 2>.
        Llega cuando nosotros somos LowID y otro cliente ha pedido al
        servidor descargar algo que compartimos: como ese cliente no
        puede conectarnos directamente a nosotros, es el servidor quien
        nos pide que abramos NOSOTROS la conexión saliente hacia él.
        Caso inverso y simétrico de _download_via_callback (ahí éramos
        nosotros quien pedía la llamada de vuelta porque la fuente era
        LowID; aquí somos nosotros la fuente LowID)."""
        if len(payload) < 6 or self._shared_library is None or not self._shared_library.enabled:
            return
        ip = _read_wire_ip(payload, 0)
        port = struct.unpack_from("<H", payload, 4)[0]
        try:
            reader, writer = await self._open_peer_connection(ip, port, 15.0)
        except (asyncio.TimeoutError, OSError, ObfuscationError, asyncio.IncompleteReadError):
            return
        try:
            writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_HELLO, self._build_hello_payload(is_answer=False)))
            await writer.drain()
            protocol, opcode, hello_payload = await read_tcp_packet(reader, timeout=20.0)
            if opcode != OP_HELLOANSWER:
                writer.close()
                return
            remote_info = _parse_hello_payload(hello_payload)
            if remote_info and remote_info.get("user_hash"):
                self._known_peer_hashes[(ip, port)] = remote_info["user_hash"]
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ObfuscationError):
            writer.close()
            return
        await self._serve_upload_session(reader, writer, send_hello_answer=False, remote_info=remote_info)

    async def _handle_incoming_peer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Dos usos posibles para una conexión entrante: el fallback de
        LowID (OP_CALLBACKREQUEST, el servidor le dice a una fuente que
        nos conecte a nosotros porque nosotros somos quien descarga) o
        un peer real que quiere descargar algo que compartimos nosotros
        (ver _serve_upload_session más abajo). En ambos casos, quien
        conecta manda primero un OP_HELLO; se distingue por si hay una
        llamada de vuelta pendiente en curso.

        Antes de nada, si la ofuscación de protocolo (punto 20) no está
        desactivada, se mira el primer byte para detectar un handshake
        de ofuscación entrante y negociarlo (`peer_reader`/`peer_writer`
        pasan a ser el stream ya descifrado de forma transparente; con
        obfuscation="required" se rechaza cualquier conexión que no
        venga ofuscada)."""
        peer_reader, peer_writer, first_byte = reader, writer, None
        try:
            if self._obfuscation != "disabled":
                peer_reader, peer_writer, first_byte = await _maybe_negotiate_incoming_obfuscation(
                    reader, writer, self._user_hash, timeout=20.0)
                if first_byte is not None and self._obfuscation == "required":
                    writer.close()
                    return
            protocol, opcode, payload = await read_tcp_packet(peer_reader, timeout=20.0, protocol=first_byte)
            if opcode != OP_HELLO:
                peer_writer.close()
                return
            remote_info = _parse_hello_payload(payload)
            if remote_info and remote_info.get("user_hash"):
                peername = writer.get_extra_info("peername")
                if peername:
                    self._known_peer_hashes[(peername[0], peername[1])] = remote_info["user_hash"]
            if self._pending_callback and not self._pending_callback["future"].done():
                self._pending_callback["future"].set_result((peer_reader, peer_writer, remote_info))
                return  # el writer lo cierra quien consuma el future
            if self._shared_library is not None and self._shared_library.enabled:
                await self._serve_upload_session(peer_reader, peer_writer, remote_info=remote_info)
                return
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ObfuscationError):
            pass
        peer_writer.close()

    # -- compartir (servir a otros peers) ----------------------------------

    async def _serve_upload_session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                      send_hello_answer: bool = True, remote_info: dict | None = None) -> None:
        """Lado "soy la fuente" del protocolo cliente-cliente: quien nos
        conectó ya mandó su OP_HELLO (leído en _handle_incoming_peer), le
        contestamos OP_HELLOANSWER y a partir de ahí es quien pide
        (OP_SETREQFILEID/OP_HASHSETREQUEST/OP_STARTUPLOADREQ/
        OP_REQUESTPARTS) mientras nosotros contestamos con los datos
        (mismo patrón, en espejo, que _client_transfer_loop usa cuando
        somos nosotros quien descarga). send_hello_answer=False cuando
        la sesión viene de _serve_via_callback: ahí quien conecta somos
        nosotros y el handshake ya se hizo (mandamos OP_HELLO y leímos
        su OP_HELLOANSWER) antes de llegar aquí. remote_info trae el
        userhash/nick ya extraídos de ese HELLO/HELLOANSWER (ver
        `_parse_hello_payload`), usados por la cola de subida con
        prioridad de amigos/créditos (punto 15 del backlog, ver
        `_acquire_upload_slot`)."""
        shared: SharedFile | None = None
        slot_acquired = False
        self._active_uploads += 1
        try:
            if send_hello_answer:
                answer_payload = self._build_hello_payload(is_answer=True)
                writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_HELLOANSWER, answer_payload))
                await writer.drain()

            while True:
                protocol, opcode, payload = await read_tcp_packet(reader, timeout=60.0)
                if opcode == OP_SETREQFILEID:
                    file_hash = payload[:16]
                    shared = self._shared_library.find_by_ed2k(file_hash) if self._shared_library else None
                    if shared is None:
                        writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_FILEREQANSNOFIL, file_hash))
                    else:
                        writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_FILESTATUS,
                                                       file_hash + struct.pack("<H", 0)))
                    await writer.drain()
                elif opcode == OP_HASHSETREQUEST:
                    if shared is not None:
                        hashset_payload = (
                            shared.ed2k + struct.pack("<H", len(shared.ed2k_parts)) + b"".join(shared.ed2k_parts)
                        )
                        writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_HASHSETANSWER, hashset_payload))
                        await writer.drain()
                elif opcode == OP_STARTUPLOADREQ:
                    if shared is None:
                        writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_FILEREQANSNOFIL, b""))
                        await writer.drain()
                    else:
                        granted = await self._acquire_upload_slot(writer, remote_info)
                        if not granted:
                            break  # se agotó la espera en cola, o el peer se desconectó
                        slot_acquired = True
                        writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_ACCEPTUPLOADREQ, b""))
                        await writer.drain()
                elif opcode == OP_REQUESTPARTS:
                    if shared is None:
                        break
                    await self._answer_request_parts(writer, shared, payload, remote_info)
                if self._debug:
                    print(f"  [debug] subida: opcode {opcode:#x} de un peer entrante")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, struct.error, IndexError):
            pass
        finally:
            self._active_uploads -= 1
            if slot_acquired:
                self._release_upload_slot()
            if self._credits_dirty:
                save_credits(self._credits)
                self._credits_dirty = False
            writer.close()

    async def _acquire_upload_slot(self, writer: asyncio.StreamWriter, remote_info: dict | None) -> bool:
        """Concede un slot de subida inmediatamente si hay uno libre; si
        no, encola la petición y espera su turno (punto 15 del backlog).
        El turno se decide en `_release_upload_slot`: amigos siempre por
        delante (por orden de llegada entre ellos), y si no hay ninguno
        esperando, quien tenga mayor `credit_modifier() * minutos
        esperando` — la misma combinación de espera y crédito que usa el
        eMule real para no dejar indefinidamente atrás a quien lleve
        mucho tiempo en cola sin historial de créditos."""
        if self._upload_slots_used < self._upload_slots:
            self._upload_slots_used += 1
            return True
        entry = {
            "user_hash": remote_info.get("user_hash") if remote_info else None,
            "since": time.monotonic(),
            "event": asyncio.Event(),
        }
        self._upload_waiting.append(entry)
        try:
            rank = self._upload_waiting.index(entry) + 1
            writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_QUEUERANK, struct.pack("<I", rank)))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        try:
            await asyncio.wait_for(entry["event"].wait(), timeout=UPLOAD_QUEUE_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            if entry in self._upload_waiting:
                self._upload_waiting.remove(entry)

    def _release_upload_slot(self) -> None:
        if not self._upload_waiting:
            self._upload_slots_used -= 1
            return
        friends_waiting = [e for e in self._upload_waiting if e["user_hash"] in self._friends]
        if friends_waiting:
            next_entry = min(friends_waiting, key=lambda e: e["since"])
        else:
            next_entry = max(
                self._upload_waiting,
                key=lambda e: ((time.monotonic() - e["since"]) / 60.0)
                * credit_modifier(self._credits.get(e["user_hash"])),
            )
        # El slot se transfiere directamente a next_entry sin pasar por
        # 0 (por eso no se decrementa self._upload_slots_used aquí): lo
        # ocupaba quien lo libera, ahora pasa a ocuparlo el elegido.
        self._upload_waiting.remove(next_entry)
        next_entry["event"].set()

    def _record_credit(self, remote_info: dict | None, uploaded_delta: int = 0, downloaded_delta: int = 0) -> None:
        if not remote_info or not remote_info.get("user_hash"):
            return
        user_hash = remote_info["user_hash"]
        nick = remote_info.get("nick")
        entry = self._credits.setdefault(user_hash, {"nick": "", "uploaded": 0, "downloaded": 0})
        if nick:
            entry["nick"] = nick
        entry["uploaded"] += uploaded_delta
        entry["downloaded"] += downloaded_delta
        self._credits_dirty = True

    def _flush_credits(self) -> None:
        if self._credits_dirty:
            save_credits(self._credits)
            self._credits_dirty = False

    async def _answer_request_parts(self, writer: asyncio.StreamWriter, shared: SharedFile, payload: bytes,
                                       remote_info: dict | None = None) -> None:
        """OP_REQUESTPARTS pide hasta 3 tramos [start,end) a la vez (los
        no usados van a 0,0); contestamos con un OP_SENDINGPART por cada
        tramo real, igual formato (hash(16)+start(4)+end(4)+datos) que ya
        interpreta _client_transfer_loop en el lado que descarga."""
        starts = struct.unpack_from("<III", payload, 16)
        ends = struct.unpack_from("<III", payload, 28)
        with open(shared.path, "rb") as f:
            for start, end in zip(starts, ends):
                if end <= start:
                    continue
                f.seek(start)
                data = f.read(end - start)
                await global_upload_limiter.consume(len(data))
                header = shared.ed2k + struct.pack("<II", start, end)
                writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_SENDINGPART, header + data))
                await writer.drain()
                self._record_credit(remote_info, uploaded_delta=len(data))
                stats_tracker.record_uploaded(self.network, len(data))

    def _build_hello_payload(self, is_answer: bool) -> bytes:
        # El protocolo eD2k/Kad real (igual que el propio aMule/eMule)
        # empaqueta cualquier dirección -la del servidor aquí, la de un
        # peer, un client_id- como un entero de 4 bytes: no hay ninguna
        # variante de campo para IPv6 en ninguna versión del protocolo,
        # así que no se puede añadir soporte real sin dejar de hablar
        # eD2k/Kad de verdad con la red (que, además, no tiene hoy ningún
        # servidor con IPv6 al que conectarse).
        tags = write_tag(CT_NAME, TAGTYPE_STRING, self._nickname)
        server_ip = socket.inet_aton(self._server_host) if self._server_host else b"\x00\x00\x00\x00"
        server_port = self._server_port or 0
        base = (
            self._user_hash
            + struct.pack("<I", self._client_id)
            + struct.pack("<H", self._listen_port)
            + struct.pack("<I", 1)  # nº de tags (uint32, no uint8)
            + tags
            + server_ip
            + struct.pack("<H", server_port)
        )
        if is_answer:
            return base
        return bytes([0x10]) + base

    async def _client_transfer_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                       file_hash: bytes, download: Download, dest_path: str, timeout: float,
                                       resume_from: int = 0, remote_info: dict | None = None) -> bool:
        entry = self._active.get(file_hash.hex())
        if entry is not None:
            entry["writer"] = writer

        writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_SETREQFILEID, file_hash))
        await writer.drain()
        protocol, opcode, payload = await read_tcp_packet(reader, timeout=timeout)
        if opcode == OP_FILEREQANSNOFIL:
            return False

        hashset: list[bytes] | None = None
        if download.size_bytes > PARTSIZE:
            writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_HASHSETREQUEST, file_hash))
            await writer.drain()
            protocol, opcode, payload = await read_tcp_packet(reader, timeout=timeout)
            if opcode == OP_HASHSETANSWER:
                cnt = struct.unpack_from("<H", payload, 16)[0]
                hashset = [payload[18 + i * 16:18 + i * 16 + 16] for i in range(cnt)]

        writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_STARTUPLOADREQ, file_hash))
        await writer.drain()

        deadline = time.monotonic() + timeout
        accepted = False
        while time.monotonic() < deadline:
            protocol, opcode, payload = await read_tcp_packet(reader, timeout=deadline - time.monotonic())
            if opcode == OP_ACCEPTUPLOADREQ:
                accepted = True
                break
            if opcode in (OP_QUEUERANK, OP_QUEUERANKING, OP_FILEREQANSNOFIL):
                # Las colas reales de eMule pueden tardar horas en darnos
                # turno; no las esperamos, probamos con la siguiente fuente.
                return False
        if not accepted:
            return False

        os.makedirs(dest_path, exist_ok=True)
        out_path = os.path.join(dest_path, download.title)
        resumable = resume_from > 0 and os.path.exists(out_path)
        pos = resume_from if resumable else 0

        download.state = DownloadState.DOWNLOADING
        download.downloaded_bytes = pos
        speed_window_start = time.monotonic()
        speed_window_bytes = pos
        try:
            with open(out_path, "r+b" if resumable else "wb") as f:
                while pos < download.size_bytes:
                    if entry is not None and (entry["paused"] or entry["cancelled"]):
                        return True  # pause_download/cancel_download ya dejaron el estado como corresponde
                    end = min(pos + REQUEST_CHUNK, download.size_bytes)
                    req = file_hash + struct.pack("<IIIIII", pos, 0, 0, end, 0, 0)
                    writer.write(build_tcp_packet(OP_EDONKEYPROT, OP_REQUESTPARTS, req))
                    await writer.drain()
                    # Una sola OP_REQUESTPARTS puede contestarse con VARIOS
                    # OP_SENDINGPART (el peer real trocea el rango pedido en
                    # bloques más pequeños de los suyos propios), así que hay
                    # que seguir leyendo hasta cubrir todo [pos, end) en vez
                    # de asumir que un único paquete ya trae el rango entero.
                    received_up_to = pos
                    while received_up_to < end:
                        protocol, opcode, payload = await read_tcp_packet(reader, timeout=timeout)
                        if opcode != OP_SENDINGPART:
                            download.state = DownloadState.ERROR
                            download.error_message = f"Se esperaba OP_SENDINGPART, llegó opcode {opcode:#x}"
                            self._notify(download)
                            return False
                        r_start = struct.unpack_from("<I", payload, 16)[0]
                        r_end = struct.unpack_from("<I", payload, 20)[0]
                        data = payload[24:24 + (r_end - r_start)]
                        await global_download_limiter.consume(len(data))
                        if entry is not None:
                            await entry["limiter"].consume(len(data))
                        self._record_credit(remote_info, downloaded_delta=len(data))
                        f.seek(r_start)
                        f.write(data)
                        received_up_to = max(received_up_to, r_end)
                        pos = received_up_to
                        download.downloaded_bytes = pos

                    # Velocidad instantánea suavizada en ventanas de ~0.5s,
                    # igual que en Soulseek/G2 (con cada trozo de 180 KB el
                    # intervalo sería demasiado ruidoso para ser útil).
                    now = time.monotonic()
                    elapsed = now - speed_window_start
                    if elapsed >= 0.5:
                        download.speed_bps = (pos - speed_window_bytes) / elapsed
                        speed_window_start = now
                        speed_window_bytes = pos
                    self._notify(download)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            download.speed_bps = 0.0
            self._flush_credits()
            if entry is not None and (entry["paused"] or entry["cancelled"]):
                return True
            download.state = DownloadState.ERROR
            download.error_message = "Conexión perdida durante la descarga"
            self._notify(download)
            return False

        download.speed_bps = 0.0
        self._flush_credits()
        # En un hilo aparte: _verify_download recalcula el MD4 (eD2k) del
        # fichero entero recién descargado, y como el resto del hasheo
        # eD2k de este proyecto usa la implementación en Python puro de
        # core/md4.py (obligada porque el MD4 de OpenSSL no está
        # disponible), llamarla directamente aquí bloquearía el event
        # loop/GUI durante toda la verificación en descargas grandes.
        bad_parts = await asyncio.to_thread(_verify_download, out_path, file_hash, hashset, download.size_bytes)
        if not bad_parts:
            download.state = DownloadState.COMPLETED
        else:
            aich_detail = await _diagnose_aich(reader, writer, file_hash, download.size_bytes,
                                                bad_parts, out_path, timeout)
            download.state = DownloadState.ERROR
            download.error_message = "Verificación MD4 fallida (fichero corrupto o incompleto)" + aich_detail
        self._notify(download)
        return True

    # -- resto de la interfaz -----------------------------------------

    def _find_active(self, download: Download) -> dict | None:
        for entry in self._active.values():
            if entry["download"] is download or entry["download"].id == download.id:
                return entry
        return None

    async def pause_download(self, download: Download) -> None:
        entry = self._find_active(download)
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
        entry = self._find_active(download)
        if entry is None:
            return
        entry["paused"] = False
        entry["cancelled"] = False
        download.state = DownloadState.SEARCHING_SOURCES
        self._notify(download)
        asyncio.create_task(
            self._run_download(entry["file_hash"], download, entry["dest_path"],
                                resume_from=download.downloaded_bytes)
        )

    async def reattach_download(self, download: Download) -> None:
        """Reengancha una descarga tras reiniciar la app. El `source_id`
        de eMule ya es autocontenido (hash eD2k, tamaño y título), así
        que basta con reconstruir la entrada y, si no estaba en pausa,
        relanzar `_run_download` desde `downloaded_bytes` (ya soportado
        vía el parámetro `resume_from`, el mismo que usa `resume_download`)."""
        if self._server_writer is None or self._find_active(download) is not None:
            return
        file_hash_hex, _size_str, _title = download.source_id.split(":::", 2)
        file_hash = bytes.fromhex(file_hash_hex)
        self._active[file_hash_hex] = {
            "download": download, "dest_path": download.dest_path, "file_hash": file_hash,
            "paused": download.state == DownloadState.PAUSED, "cancelled": False,
            "writer": None, "limiter": RateLimiter(),
        }
        if download.state != DownloadState.PAUSED:
            download.state = DownloadState.SEARCHING_SOURCES
            self._notify(download)
            asyncio.create_task(
                self._run_download(file_hash, download, download.dest_path,
                                    resume_from=download.downloaded_bytes)
            )

    async def cancel_download(self, download: Download) -> None:
        for key, entry in list(self._active.items()):
            if entry["download"] is download or entry["download"].id == download.id:
                entry["cancelled"] = True
                if entry["writer"] is not None:
                    entry["writer"].close()
                self._active.pop(key, None)
        download.state = DownloadState.CANCELLED
        download.speed_bps = 0.0
        self._notify(download)

    def set_download_limit(self, download: Download, rate_bps: int) -> None:
        entry = self._find_active(download)
        if entry is not None:
            entry["limiter"].set_rate(rate_bps)

    def subscribe_progress(self, callback: Callable[[Download], None]) -> None:
        self._progress_callback = callback

    # -- amigos y créditos (punto 15 del backlog) --------------------------

    def list_known_peers(self) -> list[dict]:
        """Todo peer del que llevamos créditos (por haber compartido algo
        con él, en cualquiera de los dos sentidos), para el diálogo de
        Amigos y créditos de la GUI. `modifier` es el mismo valor que usa
        `_release_upload_slot` para decidir a quién le toca turno."""
        return [
            {
                "user_hash": user_hash,
                "nick": entry.get("nick") or "",
                "uploaded": entry.get("uploaded", 0),
                "downloaded": entry.get("downloaded", 0),
                "modifier": credit_modifier(entry),
                "is_friend": user_hash in self._friends,
            }
            for user_hash, entry in self._credits.items()
        ]

    def is_friend(self, user_hash: bytes) -> bool:
        return user_hash in self._friends

    def add_friend(self, user_hash: bytes, nick: str | None = None) -> None:
        self._friends[user_hash] = nick or self._friends.get(user_hash, "")
        save_friends(self._friends)

    def remove_friend(self, user_hash: bytes) -> None:
        self._friends.pop(user_hash, None)
        save_friends(self._friends)

    def get_stats(self) -> dict:
        if self._server_writer is None and not self._kad_contacts:
            return {}
        stats = {
            "server": f"{self._server_host}:{self._server_port}" if self._server_host else None,
            "nickname": self._nickname,
            "known_peers": len(self._kad_contacts),
            "active_transfers": len(self._active),
        }
        if self._server_writer is not None:
            stats["id_status"] = "high" if self.is_high_id else "low"
        if self._kad_firewalled is not None:
            stats["kad_firewalled"] = "firewalled" if self._kad_firewalled else "open"
        if self._shared_library is not None and self._shared_library.enabled:
            stats["shared_files"] = len(self._shared_library.list_files())
            stats["active_uploads"] = self._active_uploads
            stats["upload_slots"] = f"{self._upload_slots_used}/{self._upload_slots}"
            if self._upload_waiting:
                stats["upload_queue"] = len(self._upload_waiting)
        return stats

    def _notify(self, download: Download) -> None:
        if self._progress_callback:
            self._progress_callback(download)


# --------------------------------------------------------------------------
# Helpers de parseo/construcción independientes de la instancia
# --------------------------------------------------------------------------

def _build_offer_files_payload(files: list[SharedFile], client_id: int, listen_port: int) -> bytes:
    """OP_OFFERFILES (ClientSocket.cpp): count(uint32) + por cada fichero
    HASH(16) + ID(4) + PORT(2) + tagcount(uint32) + tags — mismo formato,
    en espejo, que las entradas ya verificadas de OP_SEARCHRESULT (ver
    `_parse_search_result`, que interpreta justo lo que el servidor
    reenvía de vuelta cuando alguien busca), con FT_FILENAME y
    FT_FILESIZE que es lo mínimo que el servidor necesita para poder
    indexarlo y devolverlo luego en una búsqueda ajena."""
    out = bytearray()
    out += struct.pack("<I", len(files))
    for f in files:
        out += f.ed2k
        out += struct.pack("<I", client_id)
        out += struct.pack("<H", listen_port)
        name = f.rel_path.rsplit("/", 1)[-1]
        tags = write_tag(FT_FILENAME, TAGTYPE_STRING, name) + write_tag(FT_FILESIZE, TAGTYPE_UINT32, f.size)
        out += struct.pack("<I", 2) + tags
    return bytes(out)


def _build_search_request(keyword: str, file_type: str | None = None) -> bytes:
    """Query_Tree en notación polaca prefija (SearchList.cpp): para una
    sola hoja colapsa a uint8(1)+string; para N hojas, N-1 nodos AND
    seguidos de las N hojas.

    Si `file_type` mapea a una categoría eD2k reconocida
    (`_ED2K_FILETYPE_BY_CATEGORY`), se añade como una hoja adicional
    ANDed de tipo "tag" (parameter type 2, formato confirmado contra el
    código fuente real de aMule en `CSearchExprTarget::WriteMetaDataSearchParam`):
    uint8(2) + string(valor) + uint16(1) + uint8(FT_FILETYPE) — así el
    propio servidor descarta los resultados que no sean de esa
    categoría, en vez de traerlos todos y filtrarlos en cliente."""
    words = keyword.strip().split()
    leaves = [b"\x01" + _write_ed2k_string(w) for w in words]
    ed2k_type = _ED2K_FILETYPE_BY_CATEGORY.get(file_type) if file_type else None
    if ed2k_type:
        leaves.append(
            b"\x02" + _write_ed2k_string(ed2k_type) + struct.pack("<H", 1) + bytes([FT_FILETYPE])
        )
    if len(leaves) <= 1:
        return leaves[0]
    out = bytearray()
    for _ in range(len(leaves) - 1):
        out += b"\x00\x00"  # nodo operador: type=0, subtype=0 (AND)
    for leaf in leaves:
        out += leaf
    return bytes(out)


def _parse_search_result(payload: bytes) -> list[tuple[bytes, int, int, list[Tag]]]:
    """Formato real (CSearchList::ProcessSearchAnswer + CSearchFile::CSearchFile
    en SearchList.cpp/SearchFile.cpp): count(uint32) global, y por cada
    entrada HASH(16) + ID(4) + PORT(2) + tagcount(uint32, OJO: 4 bytes,
    no el uint8 habitual del resto de listas de tags) + tags."""
    offset = 0
    count = struct.unpack_from("<I", payload, offset)[0]
    offset += 4
    results = []
    for _ in range(count):
        file_hash = payload[offset:offset + 16]
        offset += 16
        client_id = struct.unpack_from("<I", payload, offset)[0]
        offset += 4
        client_port = struct.unpack_from("<H", payload, offset)[0]
        offset += 2
        tagcount = struct.unpack_from("<I", payload, offset)[0]
        offset += 4
        tags = []
        for _ in range(tagcount):
            tag, offset = read_tag(payload, offset)
            tags.append(tag)
        results.append((file_hash, client_id, client_port, tags))
    return results


def _tags_to_search_result(file_hash: bytes, tags: list[Tag], network: Network) -> SearchResult:
    title = "?"
    size = 0
    sources = 0
    for tag in tags:
        if tag.name_id == FT_FILENAME and isinstance(tag.value, str):
            title = tag.value
        elif tag.name_id == FT_FILESIZE and isinstance(tag.value, int):
            size = tag.value
        elif tag.name_id == FT_SOURCES and isinstance(tag.value, int):
            sources = tag.value
    return SearchResult(
        network=network,
        title=title,
        size_bytes=size,
        source_id=f"{file_hash.hex()}:::{size}:::{title}",
        seeds_or_sources=sources,
    )


def _verify_download(path: str, file_hash: bytes, hashset: list[bytes] | None, size: int) -> list[int]:
    """Verifica el MD4 del fichero completo (ficheros <= PARTSIZE) o de cada
    parte de PARTSIZE bytes (ficheros mayores, si hay hashset). Devuelve la
    lista de índices de parte corruptos (vacía si todo verifica); para
    ficheros de una sola parte sin hashset, el índice en caso de fallo es
    siempre 0."""
    with open(path, "rb") as f:
        if hashset:
            bad = []
            for i, part_hash in enumerate(hashset):
                chunk = f.read(PARTSIZE)
                if md4(chunk).digest() != part_hash:
                    bad.append(i)
            return bad
        data = f.read()
        return [] if md4(data).digest() == file_hash else [0]


async def _diagnose_aich(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, file_hash: bytes,
                          size: int, bad_parts: list[int], out_path: str, timeout: float) -> str:
    """Tras un fallo de verificación MD4 por parte (punto 16), intenta
    identificar vía AICH (punto 17) qué sub-bloques de 180 KiB dentro de
    cada parte corrupta son los realmente dañados, en vez de dejar la parte
    entera de ~9,3 MiB como sospechosa. Es un diagnóstico "best effort": si
    el peer no soporta AICH, se pierde la conexión, o cualquier otra cosa
    falla, se devuelve una cadena vacía sin propagar el error (la
    verificación MD4 ya determinó de forma fiable que la descarga está
    corrupta; esto solo intenta precisar dónde).

    Limitaciones honestas frente al aMule real: el master hash se toma tal
    cual del único peer conectado, sin el modelo de confianza multi-fuente
    (aMule exige ≥10 IPs independientes de acuerdo antes de fiarse de un
    master hash); y como el backend solo descarga de una fuente a la vez,
    no hay ningún mecanismo para volver a pedir automáticamente solo el
    sub-bloque corrupto a otra fuente — el resultado es puramente
    informativo en `download.error_message`.
    """
    try:
        writer.write(build_tcp_packet(OP_EMULEPROT, OP_AICHFILEHASHREQ, file_hash))
        await writer.drain()
        _, opcode, payload = await read_tcp_packet(reader, timeout=timeout)
        if opcode != OP_AICHFILEHASHANS or len(payload) < 36:
            return ""
        master_hash = payload[16:36]

        details = []
        with open(out_path, "rb") as f:
            for part_index in bad_parts:
                part_start = part_index * PARTSIZE
                part_size = min(PARTSIZE, size - part_start)
                req = file_hash + struct.pack("<H", part_index) + master_hash
                writer.write(build_tcp_packet(OP_EMULEPROT, OP_AICHREQUEST, req))
                await writer.drain()
                _, opcode, payload = await read_tcp_packet(reader, timeout=timeout)
                if opcode != OP_AICHANSWER or len(payload) < 38:
                    continue  # el peer no tiene/soporta AICH para esta parte

                recovery = payload[38:]
                entry_size = 22
                total_entries = len(recovery) // entry_size
                k = levels_to_part(size, part_index)
                n = total_entries - k
                n_blocks = block_count(part_size)
                if n != n_blocks:
                    continue  # respuesta inconsistente con lo esperado, se descarta

                bad_blocks = []
                for i in range(n_blocks):
                    entry = recovery[(k + i) * entry_size:(k + i + 1) * entry_size]
                    expected_block_hash = entry[2:22]
                    block_start = part_start + i * EMBLOCKSIZE
                    block_len = min(EMBLOCKSIZE, size - block_start)
                    f.seek(block_start)
                    data = f.read(block_len)
                    if block_sha1(data) != expected_block_hash:
                        bad_blocks.append(i)

                if bad_blocks:
                    details.append(f"parte {part_index}: sub-bloques {bad_blocks} de {n_blocks} corruptos")
                else:
                    details.append(f"parte {part_index}: AICH no localizó el sub-bloque corrupto")

        if not details:
            return ""
        return " [AICH] " + "; ".join(details)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, struct.error):
        return ""
