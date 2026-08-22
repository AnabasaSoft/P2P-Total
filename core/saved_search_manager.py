"""
Búsquedas guardadas / alertas (punto 8 del backlog): reejecuta cada
búsqueda guardada en segundo plano, cada `interval_minutes`, y avisa
solo de los resultados que no se habían visto en la comprobación
anterior. La primera comprobación de una búsqueda recién guardada solo
establece la "foto" de partida (todo lo que hay ahora se marca como ya
visto) para no bombardear con lo que ya existía al guardarla.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Callable

from core import database
from core.download_manager import DownloadManager
from core.file_types import matches_file_type
from core.models import Network, SavedSearch, SearchResult

_POLL_SECONDS = 30  # cada cuánto se comprueba si alguna búsqueda guardada tiene que ejecutarse ya


def _result_key(result: SearchResult) -> str:
    return f"{result.network.value}|{result.title}|{result.size_bytes}"


class SavedSearchManager:
    """La GUI solo habla con esta clase, nunca con `core.database`
    directamente (mismo patrón que `DownloadManager`)."""

    def __init__(self, download_manager: DownloadManager) -> None:
        self._download_manager = download_manager
        self._alert_listeners: list[Callable[[SavedSearch, list[SearchResult]], None]] = []
        self._task: asyncio.Task | None = None

    # ---- CRUD ----

    def add(self, query: str, networks: list[Network], file_type: str, interval_minutes: int) -> SavedSearch:
        saved_search_id = database.insert_saved_search(query, networks, file_type, interval_minutes)
        return database.load_saved_search(saved_search_id)

    def load(self) -> list[SavedSearch]:
        return database.load_saved_searches()

    def remove(self, saved_search: SavedSearch) -> None:
        database.delete_saved_search(saved_search.id)

    def set_enabled(self, saved_search: SavedSearch, enabled: bool) -> None:
        database.set_saved_search_enabled(saved_search.id, enabled)
        saved_search.enabled = enabled

    def load_alerts(self, saved_search: SavedSearch) -> list[SearchResult]:
        return database.load_saved_search_alerts(saved_search.id)

    def dismiss_alerts(self, saved_search: SavedSearch) -> None:
        database.clear_saved_search_alerts(saved_search.id)

    def alert_counts(self) -> dict[int, int]:
        return database.count_alerts_by_saved_search()

    # ---- Notificaciones a la GUI ----

    def on_alert(self, callback: Callable[[SavedSearch, list[SearchResult]], None]) -> None:
        """`callback` se invoca con la búsqueda guardada y la lista de
        resultados nuevos cada vez que una comprobación en segundo plano
        encuentra algo que no estaba la vez anterior."""
        self._alert_listeners.append(callback)

    # ---- Bucle en segundo plano ----

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def check_now(self, saved_search: SavedSearch) -> list[SearchResult]:
        """Fuerza la comprobación de una búsqueda guardada concreta ya
        mismo (menú contextual "Comprobar ahora" de la GUI), sin esperar
        a que le toque por `interval_minutes`."""
        return await self._check_one(saved_search)

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(_POLL_SECONDS)

    async def _check_due(self) -> None:
        now = datetime.utcnow()
        for saved_search in database.load_saved_searches():
            if not saved_search.enabled:
                continue
            if saved_search.last_checked_at is not None:
                due_at = saved_search.last_checked_at + timedelta(minutes=saved_search.interval_minutes)
                if now < due_at:
                    continue
            await self._check_one(saved_search)

    async def _check_one(self, saved_search: SavedSearch) -> list[SearchResult]:
        results = await self._download_manager.search_all(saved_search.query, networks=saved_search.networks)
        if saved_search.file_type != "all":
            results = [r for r in results if matches_file_type(r.title, saved_search.file_type)]

        current_keys = {_result_key(r) for r in results}
        is_first_run = saved_search.last_checked_at is None
        new_results = [] if is_first_run else [r for r in results if _result_key(r) not in saved_search.seen_keys]

        checked_at = datetime.utcnow()
        database.update_saved_search_run(saved_search.id, current_keys, checked_at)
        saved_search.seen_keys = current_keys
        saved_search.last_checked_at = checked_at

        if new_results:
            database.insert_saved_search_alerts(saved_search.id, new_results)
            for listener in self._alert_listeners:
                listener(saved_search, new_results)

        return new_results
