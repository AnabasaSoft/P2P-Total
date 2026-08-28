"""
Punto único de entrada que usa la GUI (o una futura CLI).
No sabe nada de libtorrent, Soulseek, DC++, etc. — solo habla
con NetworkBackend a través del BackendRegistry.
"""

import asyncio
import shutil
from pathlib import Path
from typing import Callable

from core.backend_base import BackendRegistry
from core.config import load_config
from core.models import Download, DownloadState, Network, SearchHistoryEntry, SearchResult
from core import database
from core.stats_tracker import stats_tracker

_ACTIVE_STATES = (
    DownloadState.QUEUED, DownloadState.SEARCHING_SOURCES,
    DownloadState.DOWNLOADING, DownloadState.PAUSED,
)


class DownloadManager:
    def __init__(self) -> None:
        database.init_db()
        self._progress_listeners: list[Callable[[Download], None]] = []
        self._verify_listeners: list[Callable[[Download, bool | None, Exception | None], None]] = []
        self._retry_attempts: dict[int, int] = {}  # download.id -> reintentos automáticos ya hechos
        self._auto_verified_ids: set[int] = set()  # download.id ya cubiertos por el auto-verificado (punto 27)
        for backend in BackendRegistry.all():
            backend.subscribe_progress(self._on_backend_progress)

    def register_backend(self, backend) -> None:
        """Registra un backend que se conecta después de crear el
        DownloadManager (p.ej. la GUI, que conecta cada red bajo
        demanda). El bucle de __init__ solo cubre los backends que ya
        estuvieran registrados al construirse (caso de la CLI, que
        registra antes de crear el manager)."""
        BackendRegistry.register(backend)
        backend.subscribe_progress(self._on_backend_progress)

    # ---- Búsqueda ----

    async def search_all(self, query: str, networks: list[Network] | None = None,
                          on_result: Callable[[SearchResult], None] | None = None,
                          torrent_timeout: float | None = None,
                          torrent_max_results: int | None = None,
                          soulseek_timeout: float | None = None,
                          soulseek_max_results: int | None = None,
                          gnutella2_timeout: float | None = None,
                          gnutella2_max_results: int | None = None,
                          emule_timeout: float | None = None,
                          emule_max_results: int | None = None,
                          dcpp_timeout: float | None = None,
                          dcpp_max_results: int | None = None,
                          file_type: str | None = None) -> list[SearchResult]:
        """Busca en paralelo en todas las redes conectadas (o en las
        indicadas). `on_result`, si se pasa, se invoca con cada resultado
        de Soulseek nada más llegar (ese backend soporta streaming
        incremental — ver `SoulseekBackend.search`); las demás redes
        siguen devolviendo su lote de golpe al terminar como hasta ahora.
        `torrent_timeout`/`torrent_max_results`,
        `soulseek_timeout`/`soulseek_max_results`,
        `gnutella2_timeout`/`gnutella2_max_results`,
        `emule_timeout`/`emule_max_results` y `dcpp_timeout`/
        `dcpp_max_results` se aplican solo al backend correspondiente, los
        únicos con tiempo de búsqueda y límite de resultados configurables
        en Preferencias. `file_type` (una de las claves de
        `_FILE_TYPE_EXTENSIONS` en `gui/widgets/search_tab.py`) solo se
        usa server-side en eMule (`EMuleBackend.search()`, único
        protocolo soportado con filtro de tipo nativo en el propio
        `OP_SEARCHREQUEST`); en el resto de redes el filtrado sigue
        siendo client-side, en la propia pestaña de búsqueda."""
        backends = [
            b for b in BackendRegistry.all()
            if networks is None or b.network in networks
        ]

        def _search(backend) -> "asyncio.Future":
            if backend.network == Network.TORRENT:
                kwargs = {}
                if torrent_timeout is not None:
                    kwargs["timeout"] = torrent_timeout
                if torrent_max_results is not None:
                    kwargs["max_results"] = torrent_max_results
                return backend.search(query, **kwargs)
            if backend.network == Network.SOULSEEK:
                kwargs = {}
                if on_result is not None:
                    kwargs["on_result"] = on_result
                if soulseek_timeout is not None:
                    kwargs["timeout"] = soulseek_timeout
                if soulseek_max_results is not None:
                    kwargs["max_results"] = soulseek_max_results
                return backend.search(query, **kwargs)
            if backend.network == Network.GNUTELLA2:
                kwargs = {}
                if gnutella2_timeout is not None:
                    kwargs["timeout"] = gnutella2_timeout
                if gnutella2_max_results is not None:
                    kwargs["max_results"] = gnutella2_max_results
                return backend.search(query, **kwargs)
            if backend.network == Network.EMULE:
                kwargs = {}
                if emule_timeout is not None:
                    kwargs["timeout"] = emule_timeout
                if emule_max_results is not None:
                    kwargs["max_results"] = emule_max_results
                if file_type is not None:
                    kwargs["file_type"] = file_type
                return backend.search(query, **kwargs)
            if backend.network == Network.DCPP:
                kwargs = {}
                if dcpp_timeout is not None:
                    kwargs["timeout"] = dcpp_timeout
                if dcpp_max_results is not None:
                    kwargs["max_results"] = dcpp_max_results
                return backend.search(query, **kwargs)
            return backend.search(query)

        tasks = [_search(b) for b in backends]
        results_per_backend = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[SearchResult] = []
        for backend, res in zip(backends, results_per_backend):
            if isinstance(res, Exception):
                # Una red caída no debe tumbar la búsqueda en las demás
                continue
            results.extend(res)
        return results

    # ---- Historial de búsquedas (punto 7 del backlog) ----

    def save_search(self, query: str, networks: list[Network], file_type: str) -> None:
        database.insert_search_history(query, networks, file_type)

    def load_search_history(self) -> list[SearchHistoryEntry]:
        return database.load_search_history()

    def clear_search_history(self) -> None:
        database.clear_search_history()

    # ---- Descargas ----

    async def download(self, result: SearchResult, dest_path: str, category: str | None = None) -> Download:
        backend = BackendRegistry.get(result.network)
        if backend is None:
            raise RuntimeError(f"No hay backend registrado para {result.network}")

        # Bug real reportado por el usuario: añadir dos veces el mismo
        # torrent/fuente (p.ej. al reintentar uno que parecía atascado)
        # creaba una segunda fila en Transferencias con el mismo
        # `source_id`. En BitTorrent ambas comparten info_hash, así que
        # `TorrentBackend` acababa sobrescribiendo en silencio la entrada
        # activa de la primera con la de la segunda: la primera dejaba de
        # recibir actualizaciones de `_poll_loop` y quedaba congelada para
        # siempre con un estado (ni DOWNLOADING ni PAUSED) que ya no
        # ofrecía pausar/reanudar en el menú contextual.
        for existing in database.load_all_downloads():
            if (
                existing.network == result.network
                and existing.source_id == result.source_id
                and existing.state in _ACTIVE_STATES
            ):
                raise RuntimeError(f"Ya hay una descarga activa de \"{existing.title}\"")

        download = await backend.start_download(result, dest_path)
        download.category = category
        download.id = database.insert_download(download)
        return download

    async def create_torrent(
        self,
        source_path: str,
        dest_torrent_path: str,
        trackers: list[str] | None = None,
        comment: str = "",
        private: bool = False,
    ) -> Download:
        """Punto 37 del backlog: crear un `.torrent` nuevo a partir de
        contenido propio y empezar a sembrarlo de inmediato."""
        backend = BackendRegistry.get(Network.TORRENT)
        if backend is None:
            raise RuntimeError("No hay backend registrado para BitTorrent")
        download = await backend.create_torrent(source_path, dest_torrent_path, trackers, comment, private)
        download.id = database.insert_download(download)
        return download

    async def reattach_active_downloads(self, network: Network) -> None:
        """Reengancha en el backend las descargas de `network` que
        quedaran activas (QUEUED, SEARCHING_SOURCES, DOWNLOADING o
        PAUSED) en el historial persistido. Cada backend guarda su
        seguimiento de "descarga activa" solo en memoria, así que tras
        reiniciar la app esas descargas seguían apareciendo en la
        pestaña Transferencias pero no arrancaban solas ni con el botón
        Reanudar: se llama aquí justo después de conectar la red."""
        backend = BackendRegistry.get(network)
        if backend is None:
            return
        for download in database.load_all_downloads():
            if download.network != network or download.state not in _ACTIVE_STATES:
                continue
            try:
                await backend.reattach_download(download)
            except Exception:
                pass

    async def pause(self, download: Download) -> None:
        backend = BackendRegistry.get(download.network)
        await backend.pause_download(download)

    async def resume(self, download: Download) -> None:
        backend = BackendRegistry.get(download.network)
        await backend.resume_download(download)

    async def cancel(self, download: Download) -> None:
        backend = BackendRegistry.get(download.network)
        await backend.cancel_download(download)

    async def restart(self, download: Download, from_scratch: bool = False) -> None:
        """Reengancha una descarga cancelada, reutilizando exactamente el
        mismo `backend.reattach_download()` ya validado que usa
        `reattach_active_downloads` tras reconectar una red — solo que
        aquí se dispara a demanda desde el menú contextual de la pestaña
        Transferencias ("Iniciar"/"Reiniciar") en vez de al reconectar.
        Con `from_scratch=False` ("Iniciar") se deja tal cual el
        contenido ya descargado en disco, para retomarlo desde donde se
        quedó (cancelar ya no borra archivos, ver
        `NetworkBackend.cancel_download`); con `from_scratch=True`
        ("Reiniciar") se borra antes ese contenido y se pone
        `downloaded_bytes` a 0, para empezar de cero."""
        backend = BackendRegistry.get(download.network)
        if backend is None:
            raise RuntimeError(f"La red {download.network.value} no está conectada")
        if from_scratch:
            target = Path(download.dest_path) / download.title
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                target.unlink(missing_ok=True)
            download.downloaded_bytes = 0
        download.state = DownloadState.SEARCHING_SOURCES
        if download.id is not None:
            database.update_download_progress(download.id, download.downloaded_bytes, download.state)
            self._retry_attempts.pop(download.id, None)
        await backend.reattach_download(download)

    def set_download_limit(self, download: Download, rate_bps: int) -> None:
        backend = BackendRegistry.get(download.network)
        if backend is not None:
            download.speed_limit_bps = rate_bps
            backend.set_download_limit(download, rate_bps)

    async def browse_user(self, network: Network, username: str) -> list[tuple[str, list[SearchResult]]] | None:
        """Navega los archivos compartidos de un usuario (punto 9 del
        backlog, solo soportado en Soulseek). `None` si la red no soporta
        el concepto o el backend no está conectado."""
        backend = BackendRegistry.get(network)
        if backend is None:
            return None
        return await backend.browse_user(username)

    async def browse_host(self, network: Network, host: str, port: int) -> list[SearchResult] | None:
        """Browse Host (punto 10 del backlog, solo soportado en
        Gnutella2): lista todo lo que comparte un nodo/hub concreto.
        `None` si la red no soporta el concepto o el backend no está
        conectado."""
        backend = BackendRegistry.get(network)
        if backend is None:
            return None
        return await backend.browse_host(host, port)

    def list_torrent_files(self, download: Download) -> list[dict] | None:
        """Solo tiene sentido en BitTorrent (torrents multi-archivo);
        `None` en el resto de redes o si aún no hay metadatos."""
        backend = BackendRegistry.get(download.network)
        return backend.list_files(download) if backend is not None else None

    def list_active_torrent_trackers(self) -> list[dict]:
        """Estado de los trackers de todas las descargas BitTorrent
        activas (subpestaña de Red, punto 35 del backlog): una entrada
        por tracker con el título del torrent al que pertenece, para
        poder mostrarlos todos juntos en una sola tabla."""
        backend = BackendRegistry.get(Network.TORRENT)
        if backend is None:
            return []
        entries = []
        for download in database.load_all_downloads():
            if download.network != Network.TORRENT or download.state not in _ACTIVE_STATES:
                continue
            trackers = backend.list_trackers(download)
            if not trackers:
                continue
            for tracker in trackers:
                entries.append({"title": download.title, **tracker})
        return entries

    def set_file_priorities(self, download: Download, priorities: dict[int, int]) -> None:
        """Aplica la nueva selección de archivos al backend, la persiste
        para que sobreviva a reiniciar la app (antes se perdía y
        `reattach_download` volvía a marcar todos los archivos como
        seleccionados) y borra del disco el contenido ya descargado de los
        archivos que se acaban de desmarcar en esta misma llamada (no los
        que ya estaban desmarcados de antes, para no repetir el borrado en
        cada retoque de la selección)."""
        backend = BackendRegistry.get(download.network)
        if backend is None:
            return
        previous = download.file_priorities or {}
        backend.set_file_priorities(download, priorities)
        newly_deselected = [
            index for index, priority in priorities.items()
            if priority == 0 and previous.get(index, 1) != 0
        ]
        if newly_deselected:
            backend.delete_deselected_files(download, newly_deselected)
        download.file_priorities = dict(priorities)
        if download.id is not None:
            database.update_download_file_priorities(download.id, download.file_priorities)

    def set_sequential_download(self, download: Download, enabled: bool) -> None:
        backend = BackendRegistry.get(download.network)
        if backend is not None:
            backend.set_sequential_download(download, enabled)

    def is_sequential_download(self, download: Download) -> bool:
        backend = BackendRegistry.get(download.network)
        return backend.is_sequential_download(download) if backend is not None else False

    def supports_verify(self, download: Download) -> bool:
        backend = BackendRegistry.get(download.network)
        return backend is not None and backend.supports_verify()

    async def verify(self, download: Download) -> bool:
        """Verificación a demanda (punto 27 del backlog): dispara la
        comprobación y espera su resultado, propagando cualquier error
        del backend tal cual (para tests/CLI). La GUI usa en su lugar
        `request_verify`, que además avisa a los `on_verify_result`."""
        backend = BackendRegistry.get(download.network)
        if backend is None or not backend.supports_verify():
            raise RuntimeError(f"La red {download.network.value} no soporta verificar contenido descargado")
        if download.id is not None:
            self._auto_verified_ids.add(download.id)  # no repetir sola justo después por el auto-verificado
        return await backend.verify_download(download)

    def request_verify(self, download: Download) -> None:
        """Verificación a demanda desde la GUI (menú contextual
        "Verificar archivo" de la pestaña Transferencias): la lanza en
        segundo plano y notifica el resultado a `on_verify_result`, igual
        que el auto-verificado al completar."""
        backend = BackendRegistry.get(download.network)
        if backend is None or not backend.supports_verify():
            raise RuntimeError(f"La red {download.network.value} no soporta verificar contenido descargado")
        if download.id is not None:
            self._auto_verified_ids.add(download.id)
        asyncio.ensure_future(self._verify_and_notify(download, backend))

    async def delete(self, download: Download) -> None:
        """Cancela la descarga si sigue activa, borra su registro del
        historial y elimina del disco lo que se hubiera llegado a
        descargar (fichero o carpeta, según el backend)."""
        if download.state in _ACTIVE_STATES:
            backend = BackendRegistry.get(download.network)
            if backend is not None:
                try:
                    await backend.cancel_download(download)
                except Exception:
                    pass
        if download.id is not None:
            database.delete_download(download.id)
            self._retry_attempts.pop(download.id, None)
        target = Path(download.dest_path) / download.title
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.is_file():
            target.unlink(missing_ok=True)

    def clear_completed(self) -> list[int]:
        """Borra del historial (no del disco) las descargas ya
        completadas. Devuelve los IDs borrados."""
        return database.delete_downloads_by_state(DownloadState.COMPLETED)

    def load_history(self) -> list[Download]:
        return database.load_all_downloads()

    def reorder(self, downloads: list[Download]) -> None:
        """Persiste el nuevo orden de la cola tras arrastrar filas o usar
        subir/bajar en el menú contextual de la pestaña Transferencias.
        `downloads` debe venir ya en el orden deseado, de arriba a abajo."""
        pairs = []
        for index, download in enumerate(downloads):
            download.priority = index
            if download.id is not None:
                pairs.append((download.id, index))
        database.reorder_downloads(pairs)

    # ---- Notificaciones a la GUI ----

    def on_progress(self, callback: Callable[[Download], None]) -> None:
        self._progress_listeners.append(callback)

    def on_verify_result(self, callback: Callable[[Download, bool | None, Exception | None], None]) -> None:
        """Registra un callback para el resultado de una verificación
        (a demanda o automática): `(download, es_integro, error)`, con
        exactamente uno de los dos últimos en `None`."""
        self._verify_listeners.append(callback)

    def _on_backend_progress(self, download: Download) -> None:
        if download.id is not None:
            database.update_download_progress(download.id, download.downloaded_bytes, download.state)
        stats_tracker.record_download_progress(download)
        if download.state == DownloadState.ERROR:
            self._maybe_auto_retry(download)
        elif download.state == DownloadState.COMPLETED:
            self._retry_attempts.pop(download.id, None)
            self._maybe_auto_verify(download)
        elif download.state == DownloadState.CANCELLED:
            self._retry_attempts.pop(download.id, None)
        for listener in self._progress_listeners:
            listener(download)

    def _maybe_auto_retry(self, download: Download) -> None:
        """Reintento automático configurable (punto 6 del backlog): en vez
        de dejar la descarga en ERROR sin más, si `auto_retry_max_attempts`
        (Preferencias, 0 = desactivado) no se ha agotado todavía para esta
        descarga, se reprograma un `backend.resume_download()` pasado
        `auto_retry_delay_seconds`. Los backends ya guardan la entrada
        activa aunque acaben en ERROR (solo la quitan al cancelar), así
        que `resume_download()` sirve tal cual para relanzar la búsqueda
        de fuentes sin necesidad de lógica extra en cada uno."""
        if download.id is None:
            return
        config = load_config()
        if config.auto_retry_max_attempts <= 0:
            return
        attempts = self._retry_attempts.get(download.id, 0)
        if attempts >= config.auto_retry_max_attempts:
            return
        self._retry_attempts[download.id] = attempts + 1
        asyncio.ensure_future(self._retry_after_delay(download, config.auto_retry_delay_seconds))

    async def _retry_after_delay(self, download: Download, delay: float) -> None:
        await asyncio.sleep(delay)
        if download.state != DownloadState.ERROR:
            return  # el usuario ya intervino (pausó, canceló, borró...) mientras esperábamos
        backend = BackendRegistry.get(download.network)
        if backend is None:
            return
        try:
            await backend.resume_download(download)
        except Exception:
            pass

    def _maybe_auto_verify(self, download: Download) -> None:
        """Verificación automática al completar (punto 27 del backlog):
        si `auto_verify_on_complete` está activo en Preferencias (`False`
        por defecto) y la red de esta descarga soporta verificar
        contenido, se lanza sola una vez por descarga -- se marca en
        `_auto_verified_ids` antes de arrancarla porque el backend sigue
        notificando progreso mientras siembra/reseedea con el torrent ya
        `COMPLETED`, y si no se marcara ya de entrada se relanzaría en
        cada notificación mientras dura la propia verificación."""
        if download.id is None or download.id in self._auto_verified_ids:
            return
        config = load_config()
        if not config.auto_verify_on_complete:
            return
        backend = BackendRegistry.get(download.network)
        if backend is None or not backend.supports_verify():
            return
        self._auto_verified_ids.add(download.id)
        asyncio.ensure_future(self._verify_and_notify(download, backend))

    async def _verify_and_notify(self, download: Download, backend) -> None:
        try:
            is_intact = await backend.verify_download(download)
        except Exception as exc:
            for listener in self._verify_listeners:
                listener(download, None, exc)
            return
        for listener in self._verify_listeners:
            listener(download, is_intact, None)
