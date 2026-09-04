"""Punto 44 del backlog, fase 2: integración de la descarga agregada
multired con `DownloadManager`/la GUI (`AggregatedDownloadSession` y
`encode_combined_source_id`/`decode_combined_source_id`, en
`core/aggregated_download.py`). Reutiliza el mismo patrón de
servidores G2/eMule locales por loopback que
`tests/test_aggregated_download.py`, ya que son las dos únicas redes
elegibles que soportan pausar/reanudar en esta fase (BitTorrent queda
fuera a propósito, ver el módulo)."""

import asyncio
import os
import socket
import struct

import pytest

from backends.emule_backend import EMuleBackend
from backends.g2_backend import G2Backend, sha1_to_urn_base32
from core import database
from core.aggregated_download import (
    AggregatedDownloadError,
    AggregatedDownloadSession,
    create_aggregated_session,
    decode_combined_source_id,
    encode_combined_source_id,
)
from core.backend_base import BackendRegistry
from core.download_manager import DownloadManager
from core.models import Download, DownloadState, Network, SearchResult


def _fake_result(network: Network, size: int) -> SearchResult:
    return SearchResult(
        network=network, title="x.bin", size_bytes=size, source_id="irrelevante",
        seeds_or_sources=1, extra={"clave": "valor"}, alt_source_ids=["otra_fuente"],
    )


def test_encode_decode_combined_source_id_roundtrip():
    sources = {
        Network.GNUTELLA2: _fake_result(Network.GNUTELLA2, 1000),
        Network.EMULE: _fake_result(Network.EMULE, 1000),
    }
    encoded = encode_combined_source_id(sources)
    assert encoded.startswith("aggregated:")
    decoded = decode_combined_source_id(encoded)
    assert decoded.keys() == sources.keys()
    for network, original in sources.items():
        rebuilt = decoded[network]
        assert rebuilt.network == original.network
        assert rebuilt.title == original.title
        assert rebuilt.size_bytes == original.size_bytes
        assert rebuilt.source_id == original.source_id
        assert rebuilt.seeds_or_sources == original.seeds_or_sources
        assert rebuilt.extra == original.extra
        assert rebuilt.alt_source_ids == original.alt_source_ids


@pytest.mark.asyncio
async def test_pause_and_resume_raise_when_torrent_participates():
    """Alcance deliberado de la fase 2 (ver el docstring del módulo):
    pausar/reanudar solo está soportado si BitTorrent no participa,
    porque `TorrentBackend.download_piece_range()` libera el handle de
    libtorrent al cancelarse. No hace falta un backend de verdad para
    comprobarlo: la comprobación es lo primero que hace cada método."""
    download = Download(
        id=1, network=Network.AGGREGATED, title="x", source_id="",
        dest_path="/tmp", size_bytes=1000, state=DownloadState.DOWNLOADING,
    )
    sources = {
        Network.TORRENT: _fake_result(Network.TORRENT, 1000),
        Network.GNUTELLA2: _fake_result(Network.GNUTELLA2, 1000),
    }
    session = AggregatedDownloadSession(download, sources, "/tmp/x.bin")
    with pytest.raises(AggregatedDownloadError, match="BitTorrent"):
        await session.pause()
    with pytest.raises(AggregatedDownloadError, match="BitTorrent"):
        await session.resume()


@pytest.mark.asyncio
async def test_report_progress_computes_and_resets_speed_bps(monkeypatch):
    """Fallo real encontrado tras completar la fase 2: `_report_progress()`
    solo actualizaba `download.downloaded_bytes`, nunca `speed_bps` -las
    primitivas de descarga por rango (`download_range`/
    `download_piece_range`) solo informan de bytes, no de velocidad, así
    que sin este cálculo la columna Velocidad se quedaba siempre en
    blanco (mismo bug ya visto y arreglado antes para Soulseek).
    Arreglado con una ventana de tiempo igual que la que ya usa cada
    `NetworkBackend` con sus propias descargas."""
    download = Download(
        id=1, network=Network.AGGREGATED, title="x", source_id="",
        dest_path="/tmp", size_bytes=1000, state=DownloadState.DOWNLOADING,
    )
    sources = {
        Network.GNUTELLA2: _fake_result(Network.GNUTELLA2, 1000),
        Network.EMULE: _fake_result(Network.EMULE, 1000),
    }
    session = AggregatedDownloadSession(download, sources, "/tmp/x.bin")

    fake_time = [100.0]
    monkeypatch.setattr("core.aggregated_download.time.monotonic", lambda: fake_time[0])
    session._speed_window_start = fake_time[0]

    # Dentro de la ventana (<0.5s): no recalcula todavía.
    session._done_in_range[Network.GNUTELLA2] = 100
    fake_time[0] += 0.2
    session._report_progress()
    assert download.speed_bps == 0.0

    # Al superar la ventana: recalcula bytes/segundo desde el último corte.
    session._done_in_range[Network.GNUTELLA2] = 600
    fake_time[0] += 0.5
    session._report_progress()
    assert download.speed_bps == pytest.approx(600 / 0.7)

    # pause()/cancel() dejan la velocidad a 0, igual que hace cada backend
    # real al pausar/cancelar una descarga normal.
    await session.pause()
    assert download.speed_bps == 0.0


class _G2AndEMuleSessionFixture:
    """Levanta un G2Backend y un EMuleBackend locales sirviendo el mismo
    contenido -mismo montaje que
    `test_download_aggregated_combines_g2_and_emule_sources`- y arranca
    una `AggregatedDownloadSession` real sobre ambos."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    async def __aenter__(self):
        database.init_db()
        shared_dir = self.tmp_path / "compartido"
        shared_dir.mkdir()
        self.content = os.urandom(3_000_000)
        (shared_dir / "a.bin").write_bytes(self.content)

        from core.sharing import SharedLibrary
        lib = SharedLibrary([str(shared_dir)])
        lib.rescan()
        shared = lib.list_files()[0]

        self.g2_server = G2Backend(listen_port=0, shared_library=lib)
        await self.g2_server.connect()
        self.emule_server = EMuleBackend(listen_port=0, kad_udp_port=0, shared_library=lib, obfuscation="disabled")
        await self.emule_server.connect()
        self.g2_client = G2Backend(listen_port=0)
        self.emule_client = EMuleBackend(listen_port=0, kad_udp_port=0, obfuscation="disabled")

        g2_port = self.g2_server._share_server.sockets[0].getsockname()[1]
        emule_port = self.emule_server._tcp_server.sockets[0].getsockname()[1]
        loopback_cid = struct.unpack("<I", socket.inet_aton("127.0.0.1"))[0]

        async def _fake_get_sources_from_server(file_hash, timeout):
            return [(loopback_cid, emule_port)]

        self.emule_client._get_sources_from_server = _fake_get_sources_from_server
        self.emule_client._server_writer = object()

        sha1_b32 = sha1_to_urn_base32(shared.sha1)
        self.sources = {
            Network.GNUTELLA2: SearchResult(
                network=Network.GNUTELLA2, title="a.bin", size_bytes=shared.size,
                source_id=f"127.0.0.1:{g2_port}:::{sha1_b32}:::{'0' * 32}:::a.bin",
                seeds_or_sources=1,
            ),
            Network.EMULE: SearchResult(
                network=Network.EMULE, title="a.bin", size_bytes=shared.size,
                source_id=f"{shared.ed2k.hex()}:::{shared.size}:::a.bin",
                seeds_or_sources=1,
            ),
        }
        self.dest_dir = self.tmp_path / "descarga"
        return self

    async def __aexit__(self, *exc_info):
        await self.g2_server.disconnect()
        await self.emule_server.disconnect()

    async def start_session(self, on_progress) -> tuple[Download, AggregatedDownloadSession]:
        download = Download(
            id=None, network=Network.AGGREGATED, title="", source_id="",
            dest_path=str(self.dest_dir), size_bytes=0, state=DownloadState.SEARCHING_SOURCES,
        )
        session = await create_aggregated_session(
            download, self.sources, str(self.dest_dir),
            g2_backend=self.g2_client, emule_backend=self.emule_client, on_progress=on_progress,
        )
        return download, session


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async def _poll():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_session_pause_mid_transfer_then_resume_completes(tmp_path):
    async with _G2AndEMuleSessionFixture(tmp_path) as fixture:
        pause_task: list[asyncio.Task] = []

        def on_progress(download: Download) -> None:
            if not pause_task and download.downloaded_bytes > download.size_bytes // 4:
                pause_task.append(asyncio.ensure_future(session.pause()))

        download, session = await fixture.start_session(on_progress)

        await _wait_until(lambda: download.state == DownloadState.PAUSED)
        paused_progress = download.downloaded_bytes
        assert 0 < paused_progress < download.size_bytes

        await session.resume()
        await _wait_until(lambda: download.state in (DownloadState.COMPLETED, DownloadState.ERROR))

        assert download.state == DownloadState.COMPLETED, download.error_message
        assert download.downloaded_bytes == download.size_bytes
        combined = (fixture.dest_dir / "a.bin").read_bytes()
        assert combined == fixture.content


@pytest.mark.asyncio
async def test_session_cancel_mid_transfer_stops_without_completing(tmp_path):
    async with _G2AndEMuleSessionFixture(tmp_path) as fixture:
        cancel_task: list[asyncio.Task] = []

        def on_progress(download: Download) -> None:
            if not cancel_task and download.downloaded_bytes > download.size_bytes // 4:
                cancel_task.append(asyncio.ensure_future(session.cancel()))

        download, session = await fixture.start_session(on_progress)

        await _wait_until(lambda: download.state == DownloadState.CANCELLED)
        assert download.downloaded_bytes < download.size_bytes
        # Deja correr un instante más: no debe reanudarse solo ni acabar en COMPLETED.
        await asyncio.sleep(0.1)
        assert download.state == DownloadState.CANCELLED


@pytest.mark.asyncio
async def test_download_manager_routes_aggregated_pause_resume_cancel_by_network(tmp_path, monkeypatch):
    """`DownloadManager` debe reconocer `Network.AGGREGATED` y usar la
    sesión guardada en `_aggregated_sessions` en vez de pedirle un
    backend al `BackendRegistry` (que nunca tiene uno para esta red
    virtual)."""
    monkeypatch.setattr(BackendRegistry, "get", lambda network: None)
    manager = DownloadManager()

    calls = []

    class _FakeSession:
        async def pause(self):
            calls.append("pause")

        async def resume(self):
            calls.append("resume")

        async def cancel(self):
            calls.append("cancel")

    download = Download(
        id=42, network=Network.AGGREGATED, title="x", source_id="aggregated:|||{}",
        dest_path=str(tmp_path), size_bytes=100, state=DownloadState.DOWNLOADING,
    )
    manager._aggregated_sessions[42] = _FakeSession()

    await manager.pause(download)
    await manager.resume(download)
    await manager.cancel(download)
    assert calls == ["pause", "resume", "cancel"]
    assert 42 not in manager._aggregated_sessions  # cancel() la retira


@pytest.mark.asyncio
async def test_download_manager_aggregated_restart_requires_from_scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(BackendRegistry, "get", lambda network: None)
    manager = DownloadManager()
    download = Download(
        id=1, network=Network.AGGREGATED, title="x", source_id="aggregated:|||{}",
        dest_path=str(tmp_path), size_bytes=100, state=DownloadState.CANCELLED,
    )
    with pytest.raises(RuntimeError, match="cero"):
        await manager.restart(download, from_scratch=False)


@pytest.mark.asyncio
async def test_download_manager_aggregated_pause_without_session_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(BackendRegistry, "get", lambda network: None)
    manager = DownloadManager()
    download = Download(
        id=99, network=Network.AGGREGATED, title="x", source_id="aggregated:|||{}",
        dest_path=str(tmp_path), size_bytes=100, state=DownloadState.DOWNLOADING,
    )
    with pytest.raises(RuntimeError):
        await manager.pause(download)
