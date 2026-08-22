"""
Carpeta vigilada (punto 26 del backlog): comprueba periódicamente si ha
aparecido algún fichero `.torrent` nuevo en una carpeta configurada
(`Config.watched_torrent_dir`, vacío = desactivado) y lo añade
automáticamente a las descargas, sin tener que abrirlo a mano -- mismo
patrón de bucle en segundo plano en background que ya usa
`SavedSearchManager` (punto 8).

Para no volver a añadir el mismo fichero en cada barrido se recuerda
qué ficheros ya se procesaron (ruta -> fecha de modificación) en una
pequeña caché JSON en `_config_dir()`; así, si se sustituye un
`.torrent` por otro con el mismo nombre, se detecta como nuevo otra
vez. El fichero original del usuario no se borra ni se mueve.
"""

import asyncio
import json
from pathlib import Path
from typing import Callable

from core.backend_base import BackendRegistry
from core.config import _config_dir, load_config
from core.download_manager import DownloadManager
from core.models import Download, Network

_POLL_SECONDS = 10
_CACHE_PATH = _config_dir() / "watch_folder_cache.json"


def _load_cache() -> dict[str, float]:
    if not _CACHE_PATH.exists():
        return {}
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict[str, float]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f)


class WatchFolderManager:
    """La GUI solo habla con esta clase (mismo patrón que
    `SavedSearchManager`/`DownloadManager`)."""

    def __init__(self, download_manager: DownloadManager) -> None:
        self._download_manager = download_manager
        self._task: asyncio.Task | None = None
        self._added_listeners: list[Callable[[str, Download | None, Exception | None], None]] = []

    def on_added(self, callback: Callable[[str, Download | None, Exception | None], None]) -> None:
        """`callback(filename, download, error)` se invoca tras intentar
        añadir cada fichero nuevo encontrado: `error` es `None` y
        `download` lleva la descarga arrancada si fue bien; al revés si
        falló (p.ej. la red BitTorrent no está conectada). Lo usa
        `MainWindow` para el aviso nativo (mismo icono de bandeja del
        punto 23) y para añadir la descarga a la pestaña Transferencias."""
        self._added_listeners.append(callback)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(_POLL_SECONDS)

    async def _check_once(self) -> None:
        config = load_config()
        if not config.watched_torrent_dir:
            return
        directory = Path(config.watched_torrent_dir)
        if not directory.is_dir():
            return

        cache = _load_cache()
        cache_changed = False
        for entry in sorted(directory.glob("*.torrent")):
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            key = str(entry)
            if cache.get(key) == mtime:
                continue
            if await self._add_torrent_file(entry, config.default_download_dir):
                cache[key] = mtime
                cache_changed = True
        if cache_changed:
            _save_cache(cache)

    async def _add_torrent_file(self, path: Path, dest_dir: str) -> bool:
        """Devuelve `True` si hay que marcar el fichero como ya
        procesado en la caché (se añadió bien, o falló por un motivo
        que no se va a arreglar solo con reintentar, como un `.torrent`
        corrupto) y `False` si hay que volver a intentarlo en el
        próximo barrido sin avisar todavía de nada -- caso de la red
        BitTorrent sin conectar aún, muy normal si el fichero se deja
        caer en la carpeta antes de arrancar la app o de conectar esa
        red a mano."""
        backend = BackendRegistry.get(Network.TORRENT)
        if backend is None or not await backend.is_connected():
            return False
        try:
            results = await backend.search(str(path))
            if not results:
                raise RuntimeError("No se pudo leer el .torrent (metadata vacía)")
            download = await self._download_manager.download(results[0], dest_dir)
        except Exception as exc:
            self._notify(path.name, None, exc)
            return True
        self._notify(path.name, download, None)
        return True

    def _notify(self, filename: str, download: Download | None, error: Exception | None) -> None:
        for listener in self._added_listeners:
            listener(filename, download, error)
