"""
Índice de las carpetas compartidas propias.

Común a las redes que pueden servir ficheros a otros peers (Soulseek,
DC++, Gnutella2 y eMule/eD2k — ver `backends/soulseek_backend.py`,
`backends/dcpp_backend.py`, `backends/g2_backend.py` y
`backends/emule_backend.py`). BitTorrent no lo necesita: ya siembra de
fábrica vía su propia sesión de libtorrent.

Cada backend traduce el nombre de fichero que le llega por su propio
protocolo (ruta con barra invertida en DC++, ruta con barra normal en
Soulseek, hash SHA1 en Gnutella2, hash eD2k/MD4 en eMule) a una
búsqueda contra este índice.
"""

import asyncio
import hashlib
import os
from base64 import b32decode
from dataclasses import dataclass, field
from pathlib import Path

from core import database
from core.async_utils import run_in_daemon_thread
from core.md4 import md4

# Cada cuántos ficheros (re)hasheados se publica el progreso: tanto hacia
# `self._files` (para que las búsquedas/servir ficheros vean resultados
# según van apareciendo, no solo al terminar el escaneo entero) como hacia
# la caché persistente en SQLite (para no perder el trabajo ya hecho si la
# app se cierra a mitad de un escaneo largo, p.ej. eMule/eD2k contra una
# biblioteca de decenas de GB).
_PUBLISH_EVERY = 100

# Tamaño de "parte" eD2k (PARTSIZE en emule_backend.py): el hash eD2k de
# un fichero de una sola parte es el MD4 del fichero completo; con dos o
# más partes, es el MD4 de la concatenación de los MD4 de cada parte
# (necesario aquí, no solo en emule_backend.py, porque hay que calcularlo
# al indexar la carpeta compartida, igual que el SHA1 de Gnutella2).
_ED2K_PART_SIZE = 9_728_000


@dataclass
class SharedFile:
    path: Path      # ruta absoluta real en disco
    rel_path: str   # ruta relativa a su carpeta compartida, siempre con '/'
    size: int
    sha1: bytes     # 20 bytes crudos, hace falta para servir Gnutella2 (urn:sha1:...)
    ed2k: bytes     # 16 bytes crudos, hace falta para servir eMule (hash eD2k/MD4)
    ed2k_parts: list[bytes] = field(default_factory=list)  # hashset por parte (OP_HASHSETANSWER); vacío si cabe en una sola parte


def _hash_file(path: Path, *, need_sha1: bool = True,
                need_ed2k: bool = True) -> tuple[bytes, bytes, list[bytes]] | None:
    """Calcula SHA1 y/o hash eD2k (con su hashset por parte) en una sola
    pasada de lectura del fichero, para no leerlo dos veces por cada
    carpeta compartida. `need_sha1`/`need_ed2k` a False deja el campo
    correspondiente a b""/[] sin calcular: SHA1 es barato (hashlib en C,
    a velocidad de disco), pero eD2k usa la implementación en Python puro
    de MD4 (core/md4.py, obligada porque el MD4 de OpenSSL no está
    disponible), mucho más lenta -así que solo merece la pena pagarla
    cuando de verdad hace falta (eMule; ver `SharedLibrary.rescan`, que
    es quien decide qué red necesita qué)."""
    sha1 = hashlib.sha1() if need_sha1 else None
    part_hashes: list[bytes] = []
    part_hasher = md4() if need_ed2k else None
    part_remaining = _ED2K_PART_SIZE
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                if sha1 is not None:
                    sha1.update(chunk)
                while part_hasher is not None and chunk:
                    piece, chunk = chunk[:part_remaining], chunk[part_remaining:]
                    part_hasher.update(piece)
                    part_remaining -= len(piece)
                    if part_remaining == 0:
                        part_hashes.append(part_hasher.digest())
                        part_hasher = md4()
                        part_remaining = _ED2K_PART_SIZE
    except OSError:
        return None

    ed2k, ed2k_parts = b"", []
    if need_ed2k:
        if part_remaining < _ED2K_PART_SIZE or not part_hashes:
            part_hashes.append(part_hasher.digest())
        if len(part_hashes) == 1:
            ed2k = part_hashes[0]
        else:
            ed2k = md4(b"".join(part_hashes)).digest()
            ed2k_parts = part_hashes
    sha1_digest = sha1.digest() if sha1 is not None else b""
    return sha1_digest, ed2k, ed2k_parts


class SharedLibrary:
    """Indexa en memoria una o varias carpetas compartidas. El índice
    se construye con `rescan()` (llamado al conectar cada backend que
    comparte, y cuando el usuario cambia las carpetas en Preferencias)
    y se consulta por ruta relativa, por nombre de fichero suelto o
    por hash SHA1."""

    def __init__(self, roots: list[str] | str | None = None) -> None:
        if isinstance(roots, str):
            roots = [roots]
        self._roots: list[Path] = [Path(r) for r in (roots or []) if r]
        self._files: list[SharedFile] = []
        # Caché de hashes por ruta absoluta -> (size, mtime_ns, sha1, ed2k,
        # ed2k_parts, tiene_sha1, tiene_ed2k): hashear el contenido de las
        # carpetas compartidas es caro (lee cada fichero entero), así que
        # rescan() solo (re)calcula un hash si hace falta de verdad -si su
        # tamaño/fecha de modificación han cambiado desde la última vez, o
        # si la entrada en caché no tiene todavía ese hash en concreto
        # (p.ej. si antes solo se pidió SHA1 para G2 y ahora eMule
        # necesita también el eD2k del mismo fichero, sobre la misma
        # SharedLibrary compartida entre redes - ver
        # `gui.connection_manager.ConnectionManager`)- en vez de repetir
        # el trabajo entero cada vez que se llama. Se precarga desde
        # SQLite (`core/database.py`, tabla `shared_hash_cache`) para que
        # sobreviva a reinicios de la app: sin esto, eMule tendría que
        # rehashear con eD2k (el más lento, MD4 puro Python) toda la
        # biblioteca compartida en cada arranque.
        database.init_db()
        self._hash_cache = database.load_shared_hash_cache()
        # Escaneo en segundo plano (ver `ensure_scanning`): los backends ya
        # no esperan (`await`) a que `rescan()` termine dentro de
        # `connect()`, así que una biblioteca grande no bloquea ni el
        # event loop/GUI (eso ya lo evitaba `asyncio.to_thread`) ni el
        # propio flujo de conexión de esa red -queda "conectada" y
        # operativa de inmediato, mientras el índice se va rellenando.
        self._scan_task: asyncio.Task | None = None
        self._scan_need_sha1 = False
        self._scan_need_ed2k = False

    @property
    def enabled(self) -> bool:
        return bool(self._files)

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    @property
    def scanning(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    def set_roots(self, roots: list[str]) -> None:
        self._roots = [Path(r) for r in roots if r]
        self._files = []

    def ensure_scanning(self, *, need_sha1: bool = True, need_ed2k: bool = True) -> None:
        """Debe llamarse desde una corrutina (dentro del event loop, p.ej.
        en `connect()` de cada backend que comparte). Lanza -o amplía- un
        escaneo de las carpetas compartidas en segundo plano SIN
        esperarlo: a diferencia de `await asyncio.to_thread(self.rescan,
        ...)`, esta llamada vuelve al instante, así que el backend puede
        terminar `connect()` y quedar operativo de inmediato aunque la
        biblioteca compartida sea enorme (el caso real que motivó esto:
        eMule necesita eD2k, con diferencia el hash más lento -MD4 puro
        Python-, y para una biblioteca de decenas de GB el primer escaneo
        completo puede tardar horas).

        Si ya hay un escaneo en marcha no lanza uno nuevo -para eso está
        `scanning`-, pero si la llamada pide un hash que el escaneo en
        marcha no estaba calculando (p.ej. G2 conecta primero pidiendo
        solo SHA1 y poco después conecta eMule, que además necesita eD2k,
        sobre la misma SharedLibrary compartida entre redes), amplía lo
        que se le pide y el bucle de `_background_scan` vuelve a pasar
        para completarlo, sin reiniciar desde cero lo ya hasheado."""
        self._scan_need_sha1 = self._scan_need_sha1 or need_sha1
        self._scan_need_ed2k = self._scan_need_ed2k or need_ed2k
        if self.scanning:
            return
        self._scan_task = asyncio.ensure_future(self._background_scan())

    async def _background_scan(self) -> None:
        while True:
            need_sha1, need_ed2k = self._scan_need_sha1, self._scan_need_ed2k
            await run_in_daemon_thread(
                self.rescan, need_sha1=need_sha1, need_ed2k=need_ed2k, name="shared-library-scan"
            )
            # Si nadie ha ampliado los requisitos mientras se escaneaba,
            # ya está todo cubierto y se puede parar; si sí (p.ej. eMule
            # conectó a mitad del escaneo de G2 y añadió need_ed2k), se
            # repite para completar solo lo que faltaba (rescan() ya
            # reutiliza del caché lo que no ha cambiado).
            if need_sha1 == self._scan_need_sha1 and need_ed2k == self._scan_need_ed2k:
                return

    def rescan(self, *, need_sha1: bool = True, need_ed2k: bool = True) -> None:
        """Indexa las carpetas compartidas, calculando solo los hashes
        que hagan falta de verdad: Soulseek y DC++ (en esta
        implementación) buscan un fichero compartido por su ruta/nombre,
        no por hash, así que pueden pasar `need_sha1=False,
        need_ed2k=False` y no pagar nada de hasheo; Gnutella2 solo
        necesita el SHA1 (para anunciar y responder búsquedas
        "urn:sha1:"), así que puede pasar `need_ed2k=False` y ahorrarse
        el eD2k, con diferencia el más caro de calcular porque usa la
        implementación en Python puro de MD4 (core/md4.py, obligada
        porque el MD4 de OpenSSL no está disponible en este sistema);
        eMule sí necesita el eD2k (los valores por defecto). Los hashes
        no pedidos quedan a b""/[] para ese fichero.

        Publica el progreso cada `_PUBLISH_EVERY` ficheros hacia
        `self._files`, para que quien llame a `list_files()`/`find_by_*`
        mientras el escaneo sigue en marcha vea resultados según van
        apareciendo, y hacia la caché persistente en SQLite, para no
        perder el trabajo ya hecho si la app se cierra a mitad de un
        escaneo largo- en vez de solo al terminar del todo. `self.
        _hash_cache` en cambio solo se sustituye al final: se lee de un
        alias fijo (`old_cache`) tomado al principio, para no perder
        durante la propia pasada el acceso a las entradas cacheadas de
        ficheros que todavía no le ha tocado el turno de visitar."""
        old_cache = self._hash_cache
        files: list[SharedFile] = []
        new_cache: dict[str, tuple[int, int, bytes, bytes, list[bytes], bool, bool]] = {}
        pending_db_writes: list[tuple[str, int, int, bytes, bytes, list[bytes], bool, bool]] = []
        since_publish = 0
        for root in self._roots:
            if not root.is_dir():
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    full = Path(dirpath) / name
                    try:
                        st = full.stat()
                    except OSError:
                        continue
                    size, mtime_ns = st.st_size, st.st_mtime_ns
                    key = str(full)
                    cached = old_cache.get(key)
                    if cached is not None and cached[0] == size and cached[1] == mtime_ns:
                        sha1, ed2k, ed2k_parts, has_sha1, has_ed2k = cached[2:]
                    else:
                        sha1, ed2k, ed2k_parts, has_sha1, has_ed2k = b"", b"", [], False, False

                    missing_sha1 = need_sha1 and not has_sha1
                    missing_ed2k = need_ed2k and not has_ed2k
                    if missing_sha1 or missing_ed2k:
                        hashed = _hash_file(full, need_sha1=missing_sha1, need_ed2k=missing_ed2k)
                        if hashed is None:
                            continue
                        h_sha1, h_ed2k, h_ed2k_parts = hashed
                        if missing_sha1:
                            sha1, has_sha1 = h_sha1, True
                        if missing_ed2k:
                            ed2k, ed2k_parts, has_ed2k = h_ed2k, h_ed2k_parts, True
                        pending_db_writes.append((key, size, mtime_ns, sha1, ed2k, ed2k_parts, has_sha1, has_ed2k))

                    new_cache[key] = (size, mtime_ns, sha1, ed2k, ed2k_parts, has_sha1, has_ed2k)
                    rel = full.relative_to(root).as_posix()
                    files.append(SharedFile(
                        path=full, rel_path=rel, size=size,
                        sha1=sha1, ed2k=ed2k, ed2k_parts=ed2k_parts,
                    ))

                    since_publish += 1
                    if since_publish >= _PUBLISH_EVERY:
                        since_publish = 0
                        self._files = list(files)
                        if pending_db_writes:
                            database.save_shared_hash_cache_entries(pending_db_writes)
                            pending_db_writes = []
        if pending_db_writes:
            database.save_shared_hash_cache_entries(pending_db_writes)
        database.prune_shared_hash_cache(set(new_cache.keys()))
        self._files = files
        self._hash_cache = new_cache

    def list_files(self) -> list[SharedFile]:
        return list(self._files)

    def find_by_rel_path(self, rel_path: str) -> SharedFile | None:
        """`rel_path` con '/' como separador (formato interno)."""
        for shared in self._files:
            if shared.rel_path == rel_path:
                return shared
        return None

    def find_by_native_path(self, native_path: str) -> SharedFile | None:
        """`native_path` puede venir con '\\' (DC++) o '/' (Soulseek) —
        normaliza y prueba tanto la ruta completa como solo el nombre
        de fichero final, por si el que pregunta no conserva subcarpetas."""
        normalized = native_path.replace("\\", "/").lstrip("/")
        found = self.find_by_rel_path(normalized)
        if found is not None:
            return found
        name = normalized.rsplit("/", 1)[-1]
        for shared in self._files:
            if shared.rel_path.rsplit("/", 1)[-1] == name:
                return shared
        return None

    def find_by_sha1(self, sha1: bytes) -> SharedFile | None:
        for shared in self._files:
            if shared.sha1 == sha1:
                return shared
        return None

    def find_by_sha1_b32(self, sha1_b32: str) -> SharedFile | None:
        """`sha1_b32` es el hash en Base32 (RFC 4648, sin relleno '=')
        tal como aparece en una URN 'urn:sha1:<base32>' de Gnutella2."""
        padded = sha1_b32 + "=" * (-len(sha1_b32) % 8)
        try:
            sha1 = b32decode(padded.upper())
        except (ValueError, TypeError):
            return None
        return self.find_by_sha1(sha1)

    def find_by_ed2k(self, ed2k: bytes) -> SharedFile | None:
        for shared in self._files:
            if shared.ed2k == ed2k:
                return shared
        return None
