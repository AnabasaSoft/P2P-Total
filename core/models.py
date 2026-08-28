"""
Modelos de datos comunes a todas las redes P2P soportadas.
Cada backend (torrent, soulseek, dc++, gnutella2, emule) traduce
sus propias estructuras internas a estos modelos, para que el
core y la GUI nunca tengan que saber qué protocolo hay detrás.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from datetime import datetime
from typing import Optional


class Network(str, Enum):
    TORRENT = "torrent"
    SOULSEEK = "soulseek"
    DCPP = "dcpp"
    GNUTELLA2 = "gnutella2"    # G2 — protocolo de árbol binario nativo
    EMULE = "emule"


class DownloadState(str, Enum):
    QUEUED = "queued"
    SEARCHING_SOURCES = "searching_sources"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class SearchResult:
    """Resultado de búsqueda normalizado, venga de la red que venga."""
    network: Network
    title: str
    size_bytes: int
    source_id: str          # identificador interno del backend (hash, user@file, magnet...)
    seeds_or_sources: int = 0     # seeds en torrent, nº de fuentes en emule/dc++, etc.
    extra: dict = field(default_factory=dict)   # info específica del protocolo (peer, hub, etc.)
    # source_id de otras fuentes que comparten el mismo fichero (mismo
    # título+tamaño), fusionadas en este único resultado para no mostrar
    # filas duplicadas en la búsqueda. Al descargar, el backend puede
    # probarlas como alternativa a source_id (p.ej. Soulseek "corre" varias
    # a la vez y se queda con la primera que empiece a transferir datos).
    alt_source_ids: list[str] = field(default_factory=list)


@dataclass
class Download:
    id: Optional[int]              # PK en la base de datos, None si aún no se ha insertado
    network: Network
    title: str
    source_id: str
    dest_path: str
    size_bytes: int = 0
    downloaded_bytes: int = 0
    state: DownloadState = DownloadState.QUEUED
    speed_bps: float = 0.0
    connected_peers: int = 0       # peers/fuentes con los que hay una conexión activa ahora mismo
    speed_limit_bps: int = 0       # límite propio de esta descarga en bytes/s, 0 = sin límite propio (solo runtime, no se persiste)
    priority: int = 0              # orden manual en la cola (menor = más arriba/antes); sí se persiste, a diferencia de los dos campos anteriores
    category: Optional[str] = None # nombre de la categoría (core.config.Category) elegida al arrancar la descarga, None = sin categoría
    added_at: datetime = field(default_factory=datetime.utcnow)
    error_message: Optional[str] = None
    # Selección de archivos de un torrent multi-archivo (índice -> prioridad
    # libtorrent, 0 = no descargar), solo relevante en BitTorrent. Se
    # persiste para que sobreviva a cerrar y reabrir la app (punto reportado
    # por el usuario: antes se perdía y `reattach_download` volvía a marcar
    # todos los archivos como seleccionados en cada reinicio).
    file_priorities: Optional[dict[int, int]] = None

    @property
    def progress(self) -> float:
        if self.state == DownloadState.COMPLETED:
            return 1.0
        if not self.size_bytes:
            return 0.0
        return min(1.0, self.downloaded_bytes / self.size_bytes)


@dataclass
class SearchHistoryEntry:
    """Una búsqueda ya lanzada, guardada para poder repetirla tras
    cerrar y volver a abrir la GUI (punto 7 del backlog)."""
    id: Optional[int]
    query: str
    networks: list[Network]
    file_type: str
    searched_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class SavedSearch:
    """Búsqueda guardada que `SavedSearchManager` reejecuta sola en
    segundo plano cada `interval_minutes`, para avisar cuando aparezcan
    resultados nuevos que no estaban la vez anterior (punto 8 del
    backlog: "avisa cuando aparezca X"). `seen_keys` es el conjunto de
    resultados (red|título|tamaño) ya vistos en comprobaciones
    anteriores, para poder calcular qué es nuevo la próxima vez."""
    id: Optional[int]
    query: str
    networks: list[Network]
    file_type: str
    interval_minutes: int = 30
    enabled: bool = True
    last_checked_at: Optional[datetime] = None
    seen_keys: set = field(default_factory=set)
