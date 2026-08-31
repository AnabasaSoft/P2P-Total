"""Punto 44 del backlog (v1): coordinador de descarga agregada
multired (`core/aggregated_download.py`). Cubre el reparto de tramos
de bytes (`_plan_ranges`), las validaciones de entrada, y una
integración real de extremo a extremo combinando Gnutella2 + eMule
-las dos redes cuyo servidor de subida se puede levantar en local sin
depender de una `lt.session` de BitTorrent, que ya tiene su propia
cobertura dedicada en `tests/test_torrent_probe_and_range.py`."""

import os
import socket
import struct

import pytest

from backends.emule_backend import EMuleBackend
from backends.g2_backend import G2Backend, sha1_to_urn_base32
from core import database
from core.aggregated_download import (
    AggregatedDownloadError,
    _plan_ranges,
    download_aggregated,
)
from core.models import DownloadState, Network, SearchResult
from core.sharing import SharedLibrary


def test_plan_ranges_three_networks_bt_gets_piece_aligned_tail():
    ranges = _plan_ranges(1_000_000, [Network.TORRENT, Network.GNUTELLA2, Network.EMULE], bt_piece_length=16384)

    bt_start, bt_end = ranges[Network.TORRENT]
    assert bt_end == 1_000_000
    assert bt_start % 16384 == 0

    all_ranges = sorted(ranges.values())
    assert all_ranges[0][0] == 0
    for (_, end), (next_start, _) in zip(all_ranges, all_ranges[1:]):
        assert end == next_start  # contiguos, sin huecos ni solapes
    assert all_ranges[-1][1] == 1_000_000


def test_plan_ranges_without_torrent_splits_evenly_no_alignment():
    ranges = _plan_ranges(1_000_000, [Network.GNUTELLA2, Network.EMULE], bt_piece_length=None)
    assert ranges[Network.GNUTELLA2] == (0, 500_000)
    assert ranges[Network.EMULE] == (500_000, 1_000_000)


def _fake_result(network: Network, size: int) -> SearchResult:
    return SearchResult(network=network, title="x", size_bytes=size, source_id="irrelevante", seeds_or_sources=1)


@pytest.mark.asyncio
async def test_rejects_network_not_eligible_for_aggregation(tmp_path):
    sources = {
        Network.SOULSEEK: _fake_result(Network.SOULSEEK, 1000),
        Network.GNUTELLA2: _fake_result(Network.GNUTELLA2, 1000),
    }
    with pytest.raises(AggregatedDownloadError, match="no válidas"):
        await download_aggregated(sources, str(tmp_path))


@pytest.mark.asyncio
async def test_rejects_single_source(tmp_path):
    sources = {Network.GNUTELLA2: _fake_result(Network.GNUTELLA2, 1000)}
    with pytest.raises(AggregatedDownloadError, match="al menos dos"):
        await download_aggregated(sources, str(tmp_path))


@pytest.mark.asyncio
async def test_rejects_mismatched_sizes(tmp_path):
    sources = {
        Network.GNUTELLA2: _fake_result(Network.GNUTELLA2, 1000),
        Network.EMULE: _fake_result(Network.EMULE, 2000),
    }
    with pytest.raises(AggregatedDownloadError, match="tamaño"):
        await download_aggregated(sources, str(tmp_path))


@pytest.mark.asyncio
async def test_download_aggregated_combines_g2_and_emule_sources(tmp_path, monkeypatch):
    """Extremo a extremo real: un mismo fichero servido a la vez por un
    G2Backend y un EMuleBackend locales, combinado en una sola
    descarga -sin backend falso alguno, y sin más mock que la
    localización de fuentes eD2k (que requeriría un servidor ed2k real
    de por medio, algo ajeno a lo que prueba este test: el transporte
    real del tramo, ya con la fuente conocida)- con el fichero final
    verificado byte a byte y sus hashes SHA1/eD2k comprobados contra
    los que declaran las propias fuentes."""
    database.init_db()
    shared_dir = tmp_path / "compartido"
    shared_dir.mkdir()
    content = os.urandom(1_000_000)
    (shared_dir / "a.bin").write_bytes(content)

    lib = SharedLibrary([str(shared_dir)])
    lib.rescan()
    shared = lib.list_files()[0]

    g2_server = G2Backend(listen_port=0, shared_library=lib)
    await g2_server.connect()
    emule_server = EMuleBackend(listen_port=0, kad_udp_port=0, shared_library=lib, obfuscation="disabled")
    await emule_server.connect()
    g2_client = G2Backend(listen_port=0)
    emule_client = EMuleBackend(listen_port=0, kad_udp_port=0, obfuscation="disabled")
    try:
        g2_port = g2_server._share_server.sockets[0].getsockname()[1]
        emule_port = emule_server._tcp_server.sockets[0].getsockname()[1]

        loopback_cid = struct.unpack("<I", socket.inet_aton("127.0.0.1"))[0]

        async def _fake_get_sources_from_server(file_hash, timeout):
            return [(loopback_cid, emule_port)]

        monkeypatch.setattr(emule_client, "_get_sources_from_server", _fake_get_sources_from_server)
        monkeypatch.setattr(emule_client, "_server_writer", object())  # solo para pasar el "¿hay servidor?" de download_range

        sha1_b32 = sha1_to_urn_base32(shared.sha1)
        sources = {
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

        dest_dir = tmp_path / "descarga"
        progress_updates = []
        result = await download_aggregated(
            sources, str(dest_dir), g2_backend=g2_client, emule_backend=emule_client,
            progress_callback=lambda agg: progress_updates.append(agg.downloaded_bytes),
        )

        assert result.state == DownloadState.COMPLETED
        combined = (dest_dir / "a.bin").read_bytes()
        assert combined == content
        assert progress_updates[-1] == 1_000_000
    finally:
        await g2_server.disconnect()
        await emule_server.disconnect()
