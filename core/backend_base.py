"""
Contrato común que debe implementar cada backend de red
(torrent, soulseek, dc++, gnutella2, emule).

La idea: el core y la GUI solo hablan con esta interfaz.
Añadir una red nueva = crear una clase que la implemente
y registrarla en el BackendRegistry, sin tocar el resto.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator, Callable, Optional

from core.models import Download, SearchResult, Network


class NetworkBackend(ABC):
    """Backend base. Todos los métodos son async porque las operaciones
    de red pueden tardar (conectar a un hub, esperar resultados, etc.)."""

    network: Network

    @abstractmethod
    async def connect(self) -> None:
        """Establece conexión con la red (servidor Soulseek, hub DC++,
        bootstrap DHT, daemon aMule, etc.). Debe ser idempotente."""
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def is_connected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def search(self, query: str, timeout: float = 15.0) -> list[SearchResult]:
        """Lanza una búsqueda y devuelve resultados normalizados.
        En redes con resultados asíncronos (Soulseek, DC++) el backend
        debe encargarse de esperar/agregar durante `timeout`."""
        raise NotImplementedError

    @abstractmethod
    async def start_download(self, result: SearchResult, dest_path: str) -> Download:
        """Inicia una descarga a partir de un SearchResult y devuelve
        el objeto Download inicial (aún sin datos descargados)."""
        raise NotImplementedError

    @abstractmethod
    async def pause_download(self, download: Download) -> None:
        raise NotImplementedError

    @abstractmethod
    async def resume_download(self, download: Download) -> None:
        raise NotImplementedError

    @abstractmethod
    async def cancel_download(self, download: Download) -> None:
        raise NotImplementedError

    @abstractmethod
    def subscribe_progress(self, callback: Callable[[Download], None]) -> None:
        """Registra un callback que el backend debe invocar cada vez
        que cambie el progreso/estado de una descarga suya."""
        raise NotImplementedError

    def set_global_limits(self, download_bps: int, upload_bps: int) -> None:
        """Fija el límite global de bajada/subida de este backend, en
        bytes/s (0 = sin límite). Por defecto no hace nada: Soulseek,
        DC++, Gnutella2 y eMule comparten en su lugar los limitadores
        globales de `core.rate_limiter`, que se configuran una única vez
        de forma centralizada (ver `ConnectionManager`) y a los que ya
        recurren sus propios bucles de transferencia. Solo lo sobreescribe
        `TorrentBackend`, que usa el limitador nativo de libtorrent."""
        return

    def set_download_limit(self, download: Download, rate_bps: int) -> None:
        """Fija el límite de velocidad propio de una descarga concreta,
        en bytes/s (0 = sin límite propio; sigue sujeta al límite global).
        No abstracto porque no todos los backends tenían por qué
        implementarlo desde el principio; cada uno que lo soporte lo
        sobreescribe."""
        return

    def list_files(self, download: Download) -> Optional[list[dict]]:
        """Lista los archivos de un torrent multi-archivo (índice, ruta,
        tamaño, prioridad y progreso de cada uno). Devuelve `None` si el
        backend no soporta el concepto de "torrent con varios archivos"
        (Soulseek, DC++, Gnutella2 y eMule solo descargan un archivo suelto
        por descarga). Solo lo sobreescribe `TorrentBackend`."""
        return None

    def set_file_priorities(self, download: Download, priorities: dict[int, int]) -> None:
        """Fija la prioridad (0 = no descargar, 1-7 = prioridad creciente)
        de cada archivo de un torrent, por índice. No hace nada en los
        backends que no soportan multi-archivo."""
        return

    def set_sequential_download(self, download: Download, enabled: bool) -> None:
        """Activa/desactiva la descarga secuencial (orden de piezas de
        principio a fin en vez de rarest-first), útil para poder reproducir
        un vídeo mientras se sigue descargando. No hace nada en los
        backends que no soportan multi-archivo."""
        return

    def is_sequential_download(self, download: Download) -> bool:
        """Estado actual del flag anterior, consultado directamente al
        backend (no se persiste en el modelo `Download`: en BitTorrent
        libtorrent ya lo expone vía `torrent_status.sequential_download`)."""
        return False

    async def browse_user(self, username: str, timeout: float = 30.0) -> Optional[list[tuple[str, list[SearchResult]]]]:
        """Lista los archivos compartidos de un usuario, agrupados por
        carpeta: lista de (ruta_carpeta, [SearchResult, ...]). Devuelve
        `None` si el backend no soporta navegar la colección completa de
        otro usuario (solo lo sobreescribe `SoulseekBackend`, único
        protocolo soportado con este concepto — `BrowseUser`/
        `GetSharedFileList` — en su propio wire protocol)."""
        return None

    async def browse_host(self, host: str, port: int, timeout: float = 20.0) -> Optional[list[SearchResult]]:
        """Browse Host (punto 10 del backlog): lista TODO lo que
        comparte un nodo/hub concreto (`host`:`port`), no solo lo que
        hiciera match con una búsqueda. Devuelve `None` si el backend no
        soporta este concepto (solo lo sobreescribe `G2Backend`, único
        protocolo soportado aquí con `/BH` — Browse Host — en su propio
        wire protocol)."""
        return None

    def supports_chat(self) -> bool:
        """Indica si esta red soporta chat de salas/mensajes privados
        (puntos 13 y 14 del backlog). Por defecto no lo soporta; lo
        sobreescriben `SoulseekBackend` (`SayChatroom`/`MessageUser`,
        con salas de verdad, múltiples y con nombre) y `DCPPBackend`
        (chat de hub NMDC: un único canal implícito por hub, expuesto
        como una "sala" sintética de una sola entrada vía
        `get_room_list()` para poder reutilizar el mismo flujo de GUI
        que Soulseek; y `$To:` para mensajes privados)."""
        return False

    async def join_room(self, room: str) -> None:
        """Se une a una sala de chat. No hace nada si `supports_chat()`
        devuelve `False`."""
        return

    async def leave_room(self, room: str) -> None:
        return

    async def say_in_room(self, room: str, message: str) -> None:
        return

    async def send_private_message(self, username: str, message: str) -> None:
        return

    async def get_room_list(self, timeout: float = 10.0) -> list[tuple[str, int]]:
        """Lista de salas públicas disponibles: (nombre, nº de usuarios)."""
        return []

    def subscribe_chat(self, callback) -> None:
        """Registra un callback que el backend invoca con cada evento de
        chat entrante (ver `SoulseekBackend.subscribe_chat` para el
        detalle de los tipos de evento)."""
        return

    def supports_verify(self) -> bool:
        """Indica si esta red permite reverificar contra su hash de
        referencia el contenido de una descarga ya en disco (punto 27
        del backlog). Por defecto no lo soporta -- Soulseek, DC++,
        Gnutella2 y eMule son fuentes únicas sin hash propio verificable
        hoy (ver punto 16); solo lo sobreescribe `TorrentBackend`, vía
        el recheck nativo de libtorrent contra los hashes SHA1 por pieza
        que ya trae el propio `.torrent`."""
        return False

    async def verify_download(self, download: Download, timeout: float = 300.0) -> bool:
        """Reverifica los datos ya descargados contra su hash de
        referencia. Si encuentra piezas corruptas o incompletas las
        marca como no descargadas, dejando que la propia descarga las
        vuelva a pedir sola (no hace falta relanzarla a mano). Devuelve
        `True` si todo el contenido verificado resultó íntegro, `False`
        si se detectó y corrigió alguna pieza corrupta/incompleta. Solo
        tiene sentido llamarlo si `supports_verify()` devuelve `True`."""
        raise NotImplementedError("Esta red no soporta verificar contenido ya descargado")

    def get_stats(self) -> dict:
        """Estadísticas de conexión para la pestaña Red de la GUI
        (servidor/hub al que está conectado, nº de peers/nodos
        conocidos, descargas activas...). Claves reconocidas por la
        GUI: server, listen_port, dht_nodes, known_peers,
        active_transfers. No abstracto porque no todas las redes
        tienen todas las claves; cada backend devuelve las que le
        aplican."""
        return {}


class BackendRegistry:
    """Registro central de backends disponibles. El core pide un backend
    por Network y no necesita saber nada de su implementación concreta."""

    _backends: dict[Network, NetworkBackend] = {}

    @classmethod
    def register(cls, backend: NetworkBackend) -> None:
        cls._backends[backend.network] = backend

    @classmethod
    def get(cls, network: Network) -> Optional[NetworkBackend]:
        return cls._backends.get(network)

    @classmethod
    def all(cls) -> list[NetworkBackend]:
        return list(cls._backends.values())
