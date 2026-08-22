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

import hashlib
import os
from base64 import b32decode
from dataclasses import dataclass, field
from pathlib import Path

from core.md4 import md4

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


def _hash_file(path: Path) -> tuple[bytes, bytes, list[bytes]] | None:
    """Calcula SHA1 y hash eD2k (con su hashset por parte) en una sola
    pasada de lectura del fichero, para no leerlo dos veces por cada
    carpeta compartida."""
    sha1 = hashlib.sha1()
    part_hashes: list[bytes] = []
    part_hasher = md4()
    part_remaining = _ED2K_PART_SIZE
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                sha1.update(chunk)
                while chunk:
                    piece, chunk = chunk[:part_remaining], chunk[part_remaining:]
                    part_hasher.update(piece)
                    part_remaining -= len(piece)
                    if part_remaining == 0:
                        part_hashes.append(part_hasher.digest())
                        part_hasher = md4()
                        part_remaining = _ED2K_PART_SIZE
    except OSError:
        return None

    if part_remaining < _ED2K_PART_SIZE or not part_hashes:
        part_hashes.append(part_hasher.digest())

    if len(part_hashes) == 1:
        ed2k = part_hashes[0]
        ed2k_parts: list[bytes] = []
    else:
        ed2k = md4(b"".join(part_hashes)).digest()
        ed2k_parts = part_hashes
    return sha1.digest(), ed2k, ed2k_parts


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

    @property
    def enabled(self) -> bool:
        return bool(self._files)

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    def set_roots(self, roots: list[str]) -> None:
        self._roots = [Path(r) for r in roots if r]
        self._files = []

    def rescan(self) -> None:
        files: list[SharedFile] = []
        for root in self._roots:
            if not root.is_dir():
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    full = Path(dirpath) / name
                    try:
                        size = full.stat().st_size
                    except OSError:
                        continue
                    hashed = _hash_file(full)
                    if hashed is None:
                        continue
                    sha1, ed2k, ed2k_parts = hashed
                    rel = full.relative_to(root).as_posix()
                    files.append(SharedFile(
                        path=full, rel_path=rel, size=size,
                        sha1=sha1, ed2k=ed2k, ed2k_parts=ed2k_parts,
                    ))
        self._files = files

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
