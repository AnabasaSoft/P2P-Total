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
import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from backends.emule_backend import EMuleBackend
from backends.g2_backend import G2Backend
from backends.torrent_backend import TorrentBackend
from core import database
from core.async_utils import run_in_daemon_thread
from core.models import DownloadState, Network, SearchResult
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
    total_size = sizes.pop()

    os.makedirs(dest_dir, exist_ok=True)

    bt_handle = None
    bt_infohash: bytes | None = None
    bt_piece_length: int | None = None
    filename: str

    if Network.TORRENT in sources:
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
    else:
        filename = _sanitize_filename(next(iter(sources.values())).title)

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

    error = await _verify_and_correlate(agg, sources, total_size, bt_infohash)
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


async def _verify_and_correlate(agg: AggregatedDownload, sources: dict[Network, SearchResult],
                                  total_size: int, bt_infohash: bytes | None) -> str | None:
    """Verifica el fichero combinado contra el/los hash(es) reales que
    traigan las fuentes usadas -siempre hay al menos uno, porque G2 y
    eD2k participan con su propio hash nativo (SHA1/eD2k) o si no la
    combinación no habría pasado la comprobación de redes elegibles-,
    y de paso, si se llega a conocer la tripleta completa, la registra
    en la tabla de correlación del punto 43. Devuelve un mensaje de
    error si algún hash no coincide, o `None` si todo cuadra."""
    hashed = await run_in_daemon_thread(hash_file_for_correlation, Path(agg.dest_file), name="aggregated-verify")
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
