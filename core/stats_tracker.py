"""
Estadísticas globales acumuladas por red (punto 24 del backlog): total
subido/bajado y tiempo conectado, persistidos en SQLite junto con un
histórico diario — a diferencia de `NetworkBackend.get_stats()`, que
solo expone el estado instantáneo de la conexión en curso y se pierde
en cuanto se reconecta o se cierra la app.

Instancia única a nivel de módulo (`stats_tracker`), igual que los
limitadores globales de `core.rate_limiter`: la usan tanto los
backends (bytes subidos, al servir un fichero a otro peer) como
`DownloadManager` (bytes bajados, vía el mismo callback de progreso
que ya recibe cada tick) y `ConnectionManager` (tiempo conectado, al
abrir/cerrar cada red).
"""

from datetime import datetime

from core import database
from core.models import Download, Network


class StatsTracker:
    def __init__(self) -> None:
        # download.id -> último downloaded_bytes visto, para sumar solo
        # el delta de cada tick de progreso (downloaded_bytes ya es
        # acumulado por descarga, no hay que volver a sumarlo entero).
        self._last_downloaded_bytes: dict[int, int] = {}
        self._connected_since: dict[Network, datetime] = {}

    def record_download_progress(self, download: Download) -> None:
        # No se borra la entrada al llegar a un estado terminal: el
        # mismo `on_progress` de DownloadManager puede invocarse más de
        # una vez ya con el estado final (mismo motivo por el que
        # `MainWindow._notified_download_ids`, del punto 23, tampoco se
        # borra nunca) y, si se borrara aquí, ese segundo aviso volvería
        # a contar desde cero los bytes ya sumados, duplicándolos.
        if download.id is None:
            return
        previous = self._last_downloaded_bytes.get(download.id, 0)
        delta = download.downloaded_bytes - previous
        if delta > 0:
            self._last_downloaded_bytes[download.id] = download.downloaded_bytes
            database.add_network_stats(download.network, downloaded_delta=delta)

    def record_uploaded(self, network: Network, num_bytes: int) -> None:
        if num_bytes > 0:
            database.add_network_stats(network, uploaded_delta=num_bytes)

    def connection_opened(self, network: Network) -> None:
        self._connected_since[network] = datetime.utcnow()

    def flush_connected_time(self, network: Network) -> None:
        """Vuelca a la base de datos el tiempo conectado acumulado desde
        la última llamada (o desde que se abrió la conexión) y reinicia
        el contador desde ahora. Se llama periódicamente mientras la red
        siga conectada (pestaña de Estadísticas) y también al
        desconectar, para no perder tiempo de sesión si la app se cierra
        sin pasar por `disconnect_network()` (p.ej. un cierre forzado)."""
        since = self._connected_since.get(network)
        if since is None:
            return
        now = datetime.utcnow()
        elapsed = (now - since).total_seconds()
        self._connected_since[network] = now
        if elapsed > 0:
            database.add_network_stats(network, connected_seconds_delta=elapsed)

    def connection_closed(self, network: Network) -> None:
        self.flush_connected_time(network)
        self._connected_since.pop(network, None)


stats_tracker = StatsTracker()
