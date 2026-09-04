"""Punto 44 del backlog: descarga agregada multired (v1).

Combina fuentes de un mismo contenido encontradas en BitTorrent,
Gnutella2 y/o eD2k/Kad -las tres únicas redes de este proyecto con una
primitiva real de "pedir bytes X-Y" a nivel de protocolo: BitTorrent
por piezas de tamaño fijo, G2 por 'Range:' HTTP y eD2k por
OP_REQUESTPARTS con un rango arbitrario- en una única descarga que
reparte tramos de bytes contiguos del mismo fichero de destino entre
las redes disponibles, descargando de todas a la vez.

Alcance deliberado de esta primera versión (ver DEVLOG.md, sección
"Punto 44 del backlog" para el porqué de cada límite):

- Semántica todo-o-nada: si el tramo de cualquiera de las redes falla,
  toda la descarga agregada falla con un mensaje claro. No hay reparto
  dinámico ni reintento cruzado entre redes en v1.
- Solo torrents de un único archivo (misma limitación que el punto 43).
- G2: solo conexión directa, sin el plan B /PUSH que sí tiene
  `start_download`. eD2k: solo fuentes directamente alcanzables
  (HighID) vía servidor/Kad, sin el plan B de callback para LowID.
- No persiste nada en la base de datos ni se integra todavía con
  `DownloadManager`/la GUI (pausar/reanudar/cancelar, la pestaña
  Transferencias...): eso queda para una fase 2 aparte, una vez
  validado este motor núcleo. Se usa por ahora vía CLI
  (`python main.py download-combined`) o directamente desde código.
"""

import asyncio
import json
import os
import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from backends.emule_backend import EMuleBackend
from backends.g2_backend import G2Backend
from backends.torrent_backend import TorrentBackend
from core import database
from core.async_utils import run_in_daemon_thread
from core.models import (
    Download, DownloadState, Network, SearchResult,
    decode_combined_source_id, encode_combined_source_id,
)
from core.sharing import hash_file_for_correlation

_ELIGIBLE_NETWORKS = (Network.TORRENT, Network.GNUTELLA2, Network.EMULE)


class AggregatedDownloadError(Exception):
    """Fallo de la descarga agregada en sí (reparto, verificación final)
    -no el de un tramo concreto, que se reenvía tal cual desde el
    backend correspondiente."""


@dataclass
class AggregatedDownload:
    """Progreso combinado de una descarga agregada: no es un
    `core.models.Download` -esos siempre pertenecen a una única red,
    y esta descarga por definición no- sino un resumen en memoria de
    los sub-tramos por red, pensado para pasarse a un callback de
    progreso o consultarse por CLI mientras dura."""

    title: str
    dest_file: str          # ruta completa al fichero final ya combinado
    size_bytes: int
    networks: list[Network]
    state: DownloadState = DownloadState.DOWNLOADING
    error_message: str | None = None
    ranges: dict[Network, tuple[int, int]] = field(default_factory=dict)
    sub_downloaded: dict[Network, int] = field(default_factory=dict)

    @property
    def downloaded_bytes(self) -> int:
        return min(sum(self.sub_downloaded.values()), self.size_bytes)

    @property
    def progress(self) -> float:
        return self.downloaded_bytes / self.size_bytes if self.size_bytes else 0.0


def _round_down(value: int, multiple: int) -> int:
    return (value // multiple) * multiple


def _plan_ranges(total_size: int, networks: list[Network], bt_piece_length: int | None) -> dict[Network, tuple[int, int]]:
    """Reparte `[0, total_size)` en tramos contiguos, uno por red. A
    BitTorrent -si participa- le toca siempre la cola del fichero,
    redondeada hacia abajo al tamaño de pieza (BitTorrent no puede
    pedir bytes sueltos, solo piezas completas); el resto de redes se
    reparten a partes iguales el tramo restante al principio del
    fichero, sin ninguna restricción de alineado porque tanto G2 como
    eD2k sí admiten pedir un rango de bytes arbitrario."""
    ranges: dict[Network, tuple[int, int]] = {}
    others = [n for n in networks if n != Network.TORRENT]

    if Network.TORRENT in networks:
        ideal_bt_size = total_size // len(networks)
        bt_start = _round_down(max(total_size - ideal_bt_size, 0), bt_piece_length)
        ranges[Network.TORRENT] = (bt_start, total_size)
        remaining_end = bt_start
    else:
        remaining_end = total_size

    n_other = len(others)
    if n_other:
        share = remaining_end // n_other
        pos = 0
        for i, net in enumerate(others):
            end = remaining_end if i == n_other - 1 else pos + share
            ranges[net] = (pos, end)
            pos = end

    return ranges


def _g2_expected_sha1(result: SearchResult) -> bytes:
    parts = result.source_id.split(":::", 3)
    sha1_b32 = parts[1]
    padded = sha1_b32 + "=" * (-len(sha1_b32) % 8)
    return base64.b32decode(padded.upper())


def _emule_expected_ed2k(result: SearchResult) -> bytes:
    file_hash_hex = result.source_id.split(":::", 2)[0]
    return bytes.fromhex(file_hash_hex)


def _validate_sources(sources: dict[Network, SearchResult]) -> int:
    """Comprobaciones de entrada comunes a `download_aggregated()` y a
    `create_aggregated_session()` (fase 2): redes elegibles, mínimo de
    dos fuentes y que todas declaren el mismo tamaño. Devuelve el
    tamaño total ya validado."""
    unknown = set(sources) - set(_ELIGIBLE_NETWORKS)
    if unknown:
        raise AggregatedDownloadError(
            f"Redes no válidas para descarga agregada: {[n.value for n in unknown]} "
            f"(solo {[n.value for n in _ELIGIBLE_NETWORKS]})"
        )
    if len(sources) < 2:
        raise AggregatedDownloadError("Hacen falta al menos dos redes para una descarga agregada")

    sizes = {r.size_bytes for r in sources.values()}
    if len(sizes) != 1:
        raise AggregatedDownloadError(
            f"Los resultados no coinciden en tamaño ({sorted(sizes)}): no parecen ser el mismo contenido"
        )
    return sizes.pop()


async def _probe_bt_and_pick_filename(
    sources: dict[Network, SearchResult], dest_dir: str, total_size: int,
    torrent_backend: TorrentBackend | None,
):
    """Si BitTorrent participa, sondea sus metadatos (fase previa a
    bajar nada, ver `TorrentBackend.probe_torrent_metadata`) y usa su
    nombre de fichero real; si no, deriva el nombre del título de
    cualquiera de las fuentes. Común a `download_aggregated()` y
    `create_aggregated_session()`. Devuelve
    `(bt_handle, filename, bt_piece_length, bt_infohash)`, los tres
    últimos `None`/`filename` derivado del título si no hay BitTorrent."""
    if Network.TORRENT not in sources:
        return None, _sanitize_filename(next(iter(sources.values())).title), None, None

    if torrent_backend is None:
        raise AggregatedDownloadError("Falta el backend de BitTorrent")
    bt_handle, filename, bt_piece_length, bt_total_size, bt_infohash = await torrent_backend.probe_torrent_metadata(
        sources[Network.TORRENT].source_id, dest_dir,
    )
    if bt_total_size != total_size:
        torrent_backend._session.remove_torrent(bt_handle)
        raise AggregatedDownloadError(
            "El tamaño del torrent no coincide con el resto de fuentes: no parecen ser el mismo contenido"
        )
    return bt_handle, filename, bt_piece_length, bt_infohash


async def download_aggregated(
    sources: dict[Network, SearchResult],
    dest_dir: str,
    torrent_backend: TorrentBackend | None = None,
    g2_backend: G2Backend | None = None,
    emule_backend: EMuleBackend | None = None,
    progress_callback: Callable[[AggregatedDownload], None] | None = None,
) -> AggregatedDownload:
    """Descarga `sources` -un resultado de búsqueda por red, todos del
    mismo contenido- combinando sus tramos en un único fichero dentro
    de `dest_dir`. Lanza `AggregatedDownloadError` si el reparto o la
    verificación final fallan, o la excepción que sea que lance el
    backend de la primera red cuyo tramo falle (semántica todo-o-nada,
    ver el docstring del módulo)."""
    total_size = _validate_sources(sources)
    os.makedirs(dest_dir, exist_ok=True)
    bt_handle, filename, bt_piece_length, bt_infohash = await _probe_bt_and_pick_filename(
        sources, dest_dir, total_size, torrent_backend,
    )

    dest_file = os.path.join(dest_dir, filename)
    Path(dest_file).touch(exist_ok=True)  # nunca trunca: BitTorrent puede haber creado ya el fichero real

    ranges = _plan_ranges(total_size, list(sources), bt_piece_length)
    agg = AggregatedDownload(
        title=filename, dest_file=dest_file, size_bytes=total_size,
        networks=list(sources), ranges=ranges,
        sub_downloaded={net: 0 for net in sources},
    )

    def _make_progress_cb(network: Network) -> Callable[[int], None]:
        def _cb(downloaded_in_range: int) -> None:
            agg.sub_downloaded[network] = downloaded_in_range
            if progress_callback:
                progress_callback(agg)
        return _cb

    async def _run_network(network: Network) -> None:
        start, end = ranges[network]
        if start >= end:
            return  # tramo vacío (ficheros muy pequeños repartidos entre más redes que piezas/bytes disponibles)
        if network == Network.TORRENT:
            await torrent_backend.download_piece_range(bt_handle, start, end, on_progress=_make_progress_cb(network))
        elif network == Network.GNUTELLA2:
            if g2_backend is None:
                raise AggregatedDownloadError("Falta el backend de Gnutella2")
            await g2_backend.download_range(sources[network], dest_file, start, end,
                                              on_progress=_make_progress_cb(network))
        elif network == Network.EMULE:
            if emule_backend is None:
                raise AggregatedDownloadError("Falta el backend de eMule")
            await emule_backend.download_range(sources[network], dest_file, start, end,
                                                 on_progress=_make_progress_cb(network))

    tasks = [asyncio.ensure_future(_run_network(network)) for network in sources]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        agg.state = DownloadState.ERROR
        agg.error_message = "Descarga agregada fallida: al menos un tramo no se pudo completar"
        if progress_callback:
            progress_callback(agg)
        raise

    error = await _verify_and_correlate(dest_file, sources, total_size, bt_infohash)
    if error:
        agg.state = DownloadState.ERROR
        agg.error_message = error
        if progress_callback:
            progress_callback(agg)
        raise AggregatedDownloadError(error)

    agg.state = DownloadState.COMPLETED
    if progress_callback:
        progress_callback(agg)
    return agg


async def _verify_and_correlate(dest_file: str, sources: dict[Network, SearchResult],
                                  total_size: int, bt_infohash: bytes | None) -> str | None:
    """Verifica el fichero combinado contra el/los hash(es) reales que
    traigan las fuentes usadas -siempre hay al menos uno, porque G2 y
    eD2k participan con su propio hash nativo (SHA1/eD2k) o si no la
    combinación no habría pasado la comprobación de redes elegibles-,
    y de paso, si se llega a conocer la tripleta completa, la registra
    en la tabla de correlación del punto 43. Devuelve un mensaje de
    error si algún hash no coincide, o `None` si todo cuadra. Recibe
    `dest_file` como parámetro suelto (no como parte de un
    `AggregatedDownload`) para poder reutilizarse también desde
    `AggregatedDownloadSession` (fase 2), que hace su propio
    seguimiento de progreso sobre un `core.models.Download` normal en
    vez de sobre ese dataclass -pensado solo para el uso de un único
    `await` de la CLI/`download_aggregated()`-."""
    hashed = await run_in_daemon_thread(hash_file_for_correlation, Path(dest_file), name="aggregated-verify")
    if hashed is None:
        return "no se pudo verificar el fichero final combinado (¿desapareció de disco?)"
    sha1, ed2k = hashed

    if Network.GNUTELLA2 in sources:
        expected = _g2_expected_sha1(sources[Network.GNUTELLA2])
        if sha1 != expected:
            return "verificación SHA1 fallida (fichero combinado corrupto o incompleto)"
    if Network.EMULE in sources:
        expected = _emule_expected_ed2k(sources[Network.EMULE])
        if ed2k != expected:
            return "verificación eD2k fallida (fichero combinado corrupto o incompleto)"

    if bt_infohash is not None:
        database.record_hash_correlation(total_size, sha1=sha1, ed2k=ed2k, infohash=bt_infohash)
    return None


def _sanitize_filename(name: str) -> str:
    """Duplica a propósito `g2_backend._sanitize_filename` -mismo
    criterio simple (fuera separadores de ruta), pero es una función
    privada de ese módulo y este coordinador no depende de G2 estar
    presente en la combinación."""
    return name.replace("/", "_").replace("\\", "_").strip() or "descarga"


# ---------------------------------------------------------------------------
# Punto 44 del backlog, fase 2: integración con `DownloadManager`/la GUI
# ---------------------------------------------------------------------------
#
# A diferencia de `download_aggregated()` -pensada para un único `await` de
# la CLI, sin estado que sobreviva a la llamada- `AggregatedDownloadSession`
# mantiene su progreso entre llamadas para poder pausar/reanudar/cancelar
# desde fuera (menú contextual de la pestaña Transferencias), igual que ya
# hace cada `NetworkBackend` con sus propias descargas activas. Vive solo en
# memoria, en `DownloadManager._aggregated_sessions`; el `core.models.Download`
# que representa hacia la base de datos/GUI usa `Network.AGGREGATED`.
#
# Pausar/reanudar solo está soportado si BitTorrent NO participa en la
# combinación: `TorrentBackend.download_piece_range()` libera el handle de
# libtorrent en su `finally` al cancelarse, así que interrumpir esa corrutina
# para "pausar" se llevaría por delante la sesión sin forma de retomarla
# luego -arriesgar esa lógica ya probada de v1 no compensa para esta fase-.
# Cancelar sí funciona siempre, con o sin BitTorrent: ese mismo `finally` es
# exactamente la limpieza que se quiere al cancelar de verdad.
#
# Reiniciar tras cerrar la aplicación queda fuera de esta fase: la sesión
# solo vive en memoria, así que una descarga agregada huérfana tras
# reiniciar la app se puede cancelar (marca CANCELLED en la base de datos
# aunque no haya sesión viva) y "reiniciar desde cero" (se reconstruyen los
# `SearchResult` desde el `source_id` combinado -ver
# `encode_combined_source_id`/`decode_combined_source_id`- y se arranca una
# sesión nueva desde el byte 0), pero no reanudar a medias: el progreso por
# red no se persiste.

# `encode_combined_source_id`/`decode_combined_source_id` viven ahora en
# `core/models.py` (sin dependencias de backends concretos), reexportadas
# aquí tal cual para no romper el resto de este módulo ni el código que ya
# las importaba desde aquí -ver el comentario junto a su definición para el
# porqué del traslado (permitir que la GUI las use sin arrastrar libtorrent).


class AggregatedDownloadSession:
    """Contraparte con estado de `download_aggregated()` para la fase 2:
    reparte los mismos tramos de bytes entre redes, pero expone
    `pause()`/`resume()`/`cancel()` en vez de devolver todo de golpe al
    terminar, y reporta el progreso escribiendo directamente sobre un
    `core.models.Download` real (`network=Network.AGGREGATED`) en vez
    de sobre el dataclass `AggregatedDownload` -pensado solo para la
    CLI-. Semántica todo-o-nada igual que v1: si el tramo de una red
    falla por un motivo real (no pausa/cancelación), se cancelan las
    demás y toda la descarga agregada pasa a ERROR."""

    def __init__(
        self,
        download: Download,
        sources: dict[Network, SearchResult],
        dest_file: str,
        torrent_backend: TorrentBackend | None = None,
        g2_backend: G2Backend | None = None,
        emule_backend: EMuleBackend | None = None,
        on_progress: Callable[[Download], None] | None = None,
    ) -> None:
        self.download = download
        self._sources = sources
        self._dest_file = dest_file
        self._torrent_backend = torrent_backend
        self._g2_backend = g2_backend
        self._emule_backend = emule_backend
        self._on_progress = on_progress
        self._ranges: dict[Network, tuple[int, int]] = {}
        self._done_in_range: dict[Network, int] = {network: 0 for network in sources}
        self._tasks: dict[Network, asyncio.Task] = {}
        self._bt_handle = None
        self._bt_infohash: bytes | None = None
        self._first_error: BaseException | None = None
        # Ventana de velocidad (mismo criterio que cada `NetworkBackend`
        # con sus propias descargas, p.ej. `G2Backend.download_range`):
        # sin esto `download.speed_bps` se queda siempre a 0 -bug real,
        # visto antes con Soulseek- porque las primitivas de descarga por
        # rango (`download_range`/`download_piece_range`) solo informan
        # de bytes descargados, nunca de velocidad.
        self._speed_window_start = time.monotonic()
        self._speed_window_bytes = 0

    @property
    def has_torrent(self) -> bool:
        return Network.TORRENT in self._sources

    async def start(self, bt_piece_length: int | None, bt_handle, bt_infohash: bytes | None) -> None:
        """Arranca la sesión ya con BitTorrent sondeado si participa
        -hecho aparte en `create_aggregated_session()`, porque sondear
        metadatos requiere `await` antes de poder repartir tramos, y
        `DownloadManager.start_aggregated_download()` necesita el
        `Download` ya construido (con su tamaño real) antes de
        insertarlo en la base de datos-."""
        self._bt_handle = bt_handle
        self._bt_infohash = bt_infohash
        self._ranges = _plan_ranges(self.download.size_bytes, list(self._sources), bt_piece_length)
        self.download.state = DownloadState.DOWNLOADING
        for network in self._sources:
            self._launch_one(network)

    def _launch_one(self, network: Network, resume_from: int | None = None) -> None:
        start, end = self._ranges[network]
        if resume_from is not None:
            start = resume_from
        if start >= end:
            return  # tramo vacío o ya completado del todo (al reanudar)
        self._tasks[network] = asyncio.ensure_future(self._run_one(network, start, end))

    async def _run_one(self, network: Network, start: int, end: int) -> None:
        range_start = self._ranges[network][0]

        def _on_progress(downloaded_in_call: int) -> None:
            self._done_in_range[network] = (start - range_start) + downloaded_in_call
            self._report_progress()

        try:
            if network == Network.TORRENT:
                await self._torrent_backend.download_piece_range(self._bt_handle, start, end, on_progress=_on_progress)
            elif network == Network.GNUTELLA2:
                await self._g2_backend.download_range(self._sources[network], self._dest_file, start, end,
                                                        on_progress=_on_progress)
            elif network == Network.EMULE:
                await self._emule_backend.download_range(self._sources[network], self._dest_file, start, end,
                                                           on_progress=_on_progress)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self._first_error is None:
                self._first_error = exc
                self.download.state = DownloadState.ERROR
                self.download.error_message = f"Descarga agregada fallida en {network.value}: {exc}"
                await self._cancel_siblings(except_network=network)
                self.download.speed_bps = 0.0
                self._report_progress()
            return

        # `task.done()` de la propia tarea en curso siempre da `False`
        # aquí -la corrutina todavía no ha retornado, seguimos dentro de
        # ella-, así que se excluye de la comprobación: si se ha llegado
        # hasta aquí sin excepción es que el tramo de `network` ya ha
        # terminado, solo falta comprobar el resto.
        if (
            self.download.state == DownloadState.DOWNLOADING
            and self._first_error is None
            and all(t.done() for other, t in self._tasks.items() if other != network)
        ):
            asyncio.ensure_future(self._finish())

    async def _cancel_siblings(self, except_network: Network) -> None:
        siblings = [task for network, task in self._tasks.items() if network != except_network]
        for task in siblings:
            task.cancel()
        if siblings:
            await asyncio.gather(*siblings, return_exceptions=True)

    async def _finish(self) -> None:
        error = await _verify_and_correlate(self._dest_file, self._sources, self.download.size_bytes, self._bt_infohash)
        if error:
            self.download.state = DownloadState.ERROR
            self.download.error_message = error
        else:
            self.download.state = DownloadState.COMPLETED
            self.download.downloaded_bytes = self.download.size_bytes
        self.download.speed_bps = 0.0
        self._report_progress()

    def _report_progress(self) -> None:
        self.download.downloaded_bytes = min(sum(self._done_in_range.values()), self.download.size_bytes)
        now = time.monotonic()
        elapsed = now - self._speed_window_start
        if elapsed >= 0.5:
            self.download.speed_bps = (self.download.downloaded_bytes - self._speed_window_bytes) / elapsed
            self._speed_window_start = now
            self._speed_window_bytes = self.download.downloaded_bytes
        if self._on_progress:
            self._on_progress(self.download)

    async def pause(self) -> None:
        if self.has_torrent:
            raise AggregatedDownloadError(
                "No se puede pausar una descarga agregada que incluye BitTorrent (solo cancelar)"
            )
        self.download.state = DownloadState.PAUSED
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.download.speed_bps = 0.0
        self._report_progress()

    async def resume(self) -> None:
        if self.has_torrent:
            raise AggregatedDownloadError(
                "No se puede reanudar una descarga agregada que incluye BitTorrent (solo cancelar)"
            )
        self.download.state = DownloadState.DOWNLOADING
        # Reinicia la ventana de velocidad: si no, el primer cálculo tras
        # reanudar mediría bytes reales entre un `elapsed` que incluye el
        # tiempo entero en pausa, dando una velocidad falsa casi nula.
        self._speed_window_start = time.monotonic()
        self._speed_window_bytes = self.download.downloaded_bytes
        for network in self._sources:
            start = self._ranges[network][0] + self._done_in_range[network]
            self._launch_one(network, resume_from=start)
        self._report_progress()

    async def cancel(self) -> None:
        self.download.state = DownloadState.CANCELLED
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.download.speed_bps = 0.0
        self._report_progress()


async def create_aggregated_session(
    download: Download,
    sources: dict[Network, SearchResult],
    dest_dir: str,
    torrent_backend: TorrentBackend | None = None,
    g2_backend: G2Backend | None = None,
    emule_backend: EMuleBackend | None = None,
    on_progress: Callable[[Download], None] | None = None,
) -> AggregatedDownloadSession:
    """Valida `sources` (mismas reglas que `download_aggregated()`),
    sondea BitTorrent si participa y arranca una `AggregatedDownloadSession`
    nueva. Ajusta `download.size_bytes`/`title` con los valores reales ya
    validados antes de devolverla, para que `DownloadManager` los
    persista tal cual en la base de datos."""
    total_size = _validate_sources(sources)
    os.makedirs(dest_dir, exist_ok=True)
    bt_handle, filename, bt_piece_length, bt_infohash = await _probe_bt_and_pick_filename(
        sources, dest_dir, total_size, torrent_backend,
    )
    dest_file = os.path.join(dest_dir, filename)
    Path(dest_file).touch(exist_ok=True)  # nunca trunca: BitTorrent puede haber creado ya el fichero real

    download.title = filename
    download.size_bytes = total_size
    download.source_id = encode_combined_source_id(sources)

    session = AggregatedDownloadSession(
        download, sources, dest_file,
        torrent_backend=torrent_backend, g2_backend=g2_backend, emule_backend=emule_backend,
        on_progress=on_progress,
    )
    await session.start(bt_piece_length, bt_handle, bt_infohash)
    return session
