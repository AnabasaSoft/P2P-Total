"""backends/torrent_backend.py: funciones puras de resolución de
query/magnet (sin tocar libtorrent ni la red), más el punto 34.3 del
backlog (cifrado MSE/PE y µTP), que sí se prueba contra una
`lt.session` real -pero local, sin conectar a ningún peer- porque es
la propia librería `libtorrent` la que aplica esas políticas al
crear la sesión."""

import asyncio
import time

import libtorrent as lt
import pytest

from backends.torrent_backend import (
    TorrentBackend,
    _build_magnet,
    _infohash_from_magnet,
    _is_direct_reference,
    _is_torrent_file,
    _to_magnet,
    build_torrent_file,
)
from core.models import Download, DownloadState, Network

_INFOHASH = "0123456789abcdef0123456789abcdef01234567"[:40]


def test_build_magnet_includes_infohash_name_and_trackers():
    magnet = _build_magnet(_INFOHASH, "Mi Archivo")
    assert magnet.startswith(f"magnet:?xt=urn:btih:{_INFOHASH}")
    assert "dn=Mi%20Archivo" in magnet
    assert "&tr=" in magnet


def test_is_direct_reference_magnet():
    assert _is_direct_reference("magnet:?xt=urn:btih:" + _INFOHASH)


def test_is_direct_reference_infohash():
    assert _is_direct_reference(_INFOHASH)
    assert _is_direct_reference(_INFOHASH.upper())


def test_is_direct_reference_free_text_is_false():
    assert not _is_direct_reference("una busqueda de texto libre")


def test_is_direct_reference_invalid_length_hash_is_false():
    assert not _is_direct_reference(_INFOHASH[:39])


def test_to_magnet_passes_through_existing_magnet():
    magnet = "magnet:?xt=urn:btih:" + _INFOHASH
    assert _to_magnet(magnet) == magnet


def test_to_magnet_builds_from_infohash():
    assert _to_magnet(_INFOHASH) == f"magnet:?xt=urn:btih:{_INFOHASH}"


def test_to_magnet_rejects_free_text():
    with pytest.raises(ValueError):
        _to_magnet("esto no es ni un magnet ni un infohash")


def test_is_torrent_file_requires_existing_file(tmp_path):
    fake = tmp_path / "no_existe.torrent"
    assert not _is_torrent_file(str(fake))
    fake.write_bytes(b"d")
    assert _is_torrent_file(str(fake))


def test_is_torrent_file_requires_torrent_extension(tmp_path):
    other = tmp_path / "archivo.txt"
    other.write_bytes(b"d")
    assert not _is_torrent_file(str(other))


def test_infohash_from_magnet_extracts_lowercase_hex():
    magnet = f"magnet:?xt=urn:btih:{_INFOHASH.upper()}&dn=nombre"
    assert _infohash_from_magnet(magnet) == _INFOHASH.lower()


def test_infohash_from_magnet_none_when_missing():
    assert _infohash_from_magnet("magnet:?dn=sin_btih") is None


@pytest.mark.asyncio
async def test_connect_enables_mse_pe_encryption_and_utp():
    """Punto 34.3: la sesión debe quedar configurada para negociar
    cifrado (no forzarlo, para no perder peers que no lo soportan) y
    para permitir µTP en ambos sentidos, no solo TCP."""
    backend = TorrentBackend()
    await backend.connect()
    try:
        settings = backend._session.get_settings()
        assert settings["out_enc_policy"] == lt.enc_policy.pe_enabled
        assert settings["in_enc_policy"] == lt.enc_policy.pe_enabled
        assert settings["allowed_enc_level"] == lt.enc_level.pe_both
        assert settings["enable_outgoing_utp"] is True
        assert settings["enable_incoming_utp"] is True
    finally:
        await backend.disconnect()


@pytest.mark.asyncio
async def test_connect_applies_ip_filter_rules_to_session():
    """Punto 39: los rangos del filtro global (`core.ip_filter.ip_filter`)
    se vuelcan al `lt.ip_filter` nativo de la sesión al conectar, de forma
    que libtorrent rechace por sí mismo cualquier peer de esos rangos."""
    import ipaddress

    from core.ip_filter import ip_filter

    start = int(ipaddress.IPv4Address("1.2.3.0"))
    end = int(ipaddress.IPv4Address("1.2.3.255"))
    ip_filter._ranges = [(start, end, 50)]
    ip_filter._starts = [start]
    ip_filter.configure(enabled=True, level_threshold=127)
    try:
        backend = TorrentBackend()
        await backend.connect()
        try:
            lt_filter = backend._session.get_ip_filter()
            assert lt_filter.access("1.2.3.42") == 1
            assert lt_filter.access("8.8.8.8") == 0
        finally:
            await backend.disconnect()
    finally:
        ip_filter.configure(enabled=False)
        ip_filter._ranges = []
        ip_filter._starts = []


@pytest.mark.asyncio
async def test_get_stats_reports_peer_encryption_and_utp_counters():
    """Sin peers conectados, los contadores nuevos deben existir y
    partir de cero -la prueba con tráfico real (peers de verdad
    cifrando/usando µTP) se hace a mano por CLI, como el resto de
    validación de protocolo contra infraestructura real del proyecto."""
    backend = TorrentBackend()
    await backend.connect()
    try:
        stats = backend.get_stats()
        assert stats["connected_peers"] == 0
        assert stats["encrypted_peers"] == 0
        assert stats["utp_connections"] == 0
    finally:
        await backend.disconnect()


@pytest.mark.asyncio
async def test_get_stats_reports_dht_global_nodes_and_session_totals():
    """Punto 35: dht_global_nodes (tamaño estimado de toda la red DHT,
    no solo los nodos con los que tenemos contacto directo) y los
    totales de sesión -sin tráfico real todavía deben partir de 0."""
    backend = TorrentBackend()
    await backend.connect()
    try:
        stats = backend.get_stats()
        assert stats["dht_global_nodes"] == 0
        assert stats["total_downloaded"] == 0
        assert stats["total_uploaded"] == 0
    finally:
        await backend.disconnect()


def test_get_stats_empty_dict_when_not_connected():
    backend = TorrentBackend()
    assert backend.get_stats() == {}


@pytest.mark.asyncio
async def test_list_trackers_reports_url_from_magnet(tmp_path):
    """Punto 35: aunque no haya habido ningún anuncio real todavía
    (sesión local, sin red), libtorrent ya expone en trackers() la URL
    de cada tracker declarado en el magnet -es lo mínimo que la
    subpestaña de BitTorrent puede mostrar sin depender de tráfico
    real, que se valida a mano por CLI como el resto del proyecto."""
    backend = TorrentBackend()
    await backend.connect()
    try:
        magnet = _build_magnet(_INFOHASH, "Mi Archivo")
        download = Download(
            id=None,
            network=Network.TORRENT,
            title="Mi Archivo",
            source_id=magnet,
            dest_path=str(tmp_path),
        )
        await backend.reattach_download(download)
        trackers = backend.list_trackers(download)
        assert trackers is not None
        assert any("tracker" in t["url"] for t in trackers)
        assert all({"url", "working", "message", "seeds", "peers"} <= t.keys() for t in trackers)
    finally:
        await backend.disconnect()


def test_list_trackers_reports_working_if_any_endpoint_succeeds():
    """Bug real reportado por el usuario: dos descargas avanzando a
    buena velocidad (con peers reales) mostraban TODOS sus trackers
    "con errores" en la pestaña Red. Desde libtorrent 2.x, cada tracker
    se anuncia por separado desde cada interfaz de red local
    (loopback, la IP real...) y esos resultados van en "endpoints"; los
    campos de nivel superior del dict (`fails`/`scrape_*`) solo
    reflejan el primer endpoint -normalmente el de loopback, que nunca
    tiene salida real a internet y por tanto siempre falla- así que
    mirarlos directamente hacía ver "con errores" un tracker que en
    realidad funcionaba bien por la interfaz real."""
    loopback_endpoint = {"message": "", "fails": 1, "scrape_complete": -1, "scrape_incomplete": -1}
    real_endpoint = {"message": "", "fails": 0, "scrape_complete": 3, "scrape_incomplete": 1}
    tracker_dict = {
        "url": "udp://tracker.example.org:1337/announce",
        "fails": 1,  # nivel superior = el primer endpoint (loopback, en fallo)
        "message": "",
        "scrape_complete": -1,
        "scrape_incomplete": -1,
        "endpoints": [loopback_endpoint, real_endpoint],
    }

    class _FakeTrackersHandle:
        def trackers(self):
            return [tracker_dict]

    backend = TorrentBackend()
    download = _fake_download()
    backend._active["fakehash"] = {"handle": _FakeTrackersHandle(), "download": download}

    trackers = backend.list_trackers(download)
    assert trackers == [{
        "url": "udp://tracker.example.org:1337/announce",
        "working": True,
        "message": "",
        "seeds": 3,
        "peers": 1,
    }]


def test_list_trackers_none_when_no_active_download():
    backend = TorrentBackend()
    download = Download(
        id=None, network=Network.TORRENT, title="x",
        source_id="magnet:?xt=urn:btih:" + _INFOHASH, dest_path="/tmp",
    )
    assert backend.list_trackers(download) is None


def test_build_torrent_file_from_single_file(tmp_path):
    """Punto 37: crear un .torrent nuevo a partir de un archivo local
    -sin ningún socket, solo lectura a disco para hashear las piezas-
    debe producir un .torrent bencoded válido con el nombre, tamaño,
    trackers, comentario y flag de privado correctos."""
    source = tmp_path / "contenido.bin"
    source.write_bytes(b"hola mundo" * 5000)

    data = build_torrent_file(
        str(source),
        trackers=["udp://tracker.example.org:1337/announce"],
        comment="comentario de prueba",
        private=True,
    )

    info = lt.torrent_info(lt.bdecode(data))
    assert info.name() == "contenido.bin"
    assert info.total_size() == len(b"hola mundo" * 5000)
    assert info.priv() is True
    assert info.comment() == "comentario de prueba"
    trackers = [tr.url for tr in info.trackers()]
    assert "udp://tracker.example.org:1337/announce" in trackers


def test_build_torrent_file_from_folder_includes_all_files(tmp_path):
    """libtorrent puede intercalar archivos `.pad/` de relleno para
    alinear piezas en un torrent híbrido v1+v2 (comportamiento propio
    de la librería, no algo que este código decida) -por eso se
    comprueba que los dos archivos reales están, sin asumir un
    `num_files()` exacto."""
    folder = tmp_path / "carpeta"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"a" * 100)
    (folder / "b.txt").write_bytes(b"b" * 200)

    data = build_torrent_file(str(folder))

    info = lt.torrent_info(lt.bdecode(data))
    assert info.name() == "carpeta"
    paths = {info.files().file_path(i) for i in range(info.num_files())}
    assert any(p.endswith("a.txt") for p in paths)
    assert any(p.endswith("b.txt") for p in paths)
    assert info.priv() is False


def test_build_torrent_file_rejects_empty_folder(tmp_path):
    empty = tmp_path / "vacia"
    empty.mkdir()
    with pytest.raises(ValueError):
        build_torrent_file(str(empty))


@pytest.mark.asyncio
async def test_create_torrent_writes_file_and_starts_seeding(tmp_path):
    """Punto 37: tras crear el .torrent, el backend lo añade a la
    sesión con save_path en el propio directorio de origen -las piezas
    ya están en disco, así que debe reconocerlas como completas de
    inmediato y empezar a sembrar sin descargar nada."""
    source = tmp_path / "compartido.bin"
    source.write_bytes(b"x" * 50000)
    dest_torrent = tmp_path / "compartido.torrent"

    backend = TorrentBackend()
    await backend.connect()
    try:
        download = await backend.create_torrent(str(source), str(dest_torrent))
        assert dest_torrent.exists()
        assert download.title == "compartido.bin"
        assert download.size_bytes == 50000

        for _ in range(50):
            entry = backend._find_entry(download)
            if entry is not None and entry["handle"].status().is_seeding:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("El torrent creado no llegó a is_seeding=True")
    finally:
        await backend.disconnect()


@pytest.mark.asyncio
async def test_pause_download_stays_paused_across_poll_ticks(tmp_path):
    """Punto 38: bug real descubierto al implementar el límite de ratio/
    tiempo de siembra. Sin `unset_flags(auto_managed)`, el gestor de cola
    de libtorrent deshacía un `pause()` manual al cabo de 1-2 segundos por
    su cuenta, y `_poll_loop` además pisaba `download.state` de vuelta a
    DOWNLOADING/COMPLETED en el siguiente tick por no comprobar
    `status.paused` antes de aplicar `LT_STATE_MAP` -así que una descarga
    "pausada" desde la GUI en realidad seguía transfiriendo."""
    source = tmp_path / "compartido.bin"
    source.write_bytes(b"x" * 50000)
    dest_torrent = tmp_path / "compartido.torrent"

    backend = TorrentBackend()
    await backend.connect()
    try:
        download = await backend.create_torrent(str(source), str(dest_torrent))
        entry = backend._find_entry(download)
        for _ in range(50):
            if entry["handle"].status().is_seeding:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("El torrent creado no llegó a is_seeding=True")

        await backend.pause_download(download)
        await asyncio.sleep(2.5)  # más de dos vueltas de _poll_loop (1 s cada una)
        assert entry["handle"].status().paused is True
        assert download.state == DownloadState.PAUSED

        await backend.resume_download(download)
        await asyncio.sleep(1.5)
        assert entry["handle"].status().paused is False
    finally:
        await backend.disconnect()


class _FakeStatus:
    """Sustituto mínimo de `lt.torrent_status` para probar la lógica de
    límite de ratio/tiempo de siembra de `_poll_loop` sin depender de
    tráfico real de subida entre peers (algo que una `lt.session` local
    sin conexión a la red no puede generar de forma determinista)."""

    def __init__(self, all_time_upload=0, total_wanted=1000, paused=False,
                 state=lt.torrent_status.states.seeding):
        self.all_time_upload = all_time_upload
        self.total_wanted = total_wanted
        self.total_wanted_done = total_wanted
        self.download_rate = 0
        self.num_peers = 0
        self.paused = paused
        self.state = state
        self.errc = lt.error_code()


class _FakeHandle:
    def __init__(self, status: _FakeStatus):
        self._status = status
        self.paused_called = False

    def status(self):
        return self._status

    def unset_flags(self, _flag):
        pass

    def set_flags(self, _flag):
        pass

    def pause(self):
        self.paused_called = True
        self._status.paused = True

    def info_hash(self):
        return "fakehash"


def _fake_download() -> Download:
    return Download(
        id=None, network=Network.TORRENT, title="x",
        source_id="magnet:?xt=urn:btih:" + _INFOHASH, dest_path="/tmp",
    )


@pytest.mark.asyncio
async def test_seed_ratio_limit_auto_pauses_when_exceeded():
    backend = TorrentBackend()
    await backend.connect()
    try:
        backend.set_seed_limits(0.5, 0)
        status = _FakeStatus(all_time_upload=600, total_wanted=1000)  # ratio 0.6 >= 0.5
        handle = _FakeHandle(status)
        download = _fake_download()
        backend._active["fakehash"] = {"handle": handle, "download": download}

        await asyncio.sleep(1.5)

        assert handle.paused_called is True
        assert download.state == DownloadState.PAUSED
    finally:
        await backend.disconnect()


@pytest.mark.asyncio
async def test_seed_time_limit_auto_pauses_when_exceeded():
    backend = TorrentBackend()
    await backend.connect()
    try:
        backend.set_seed_limits(0.0, 1)  # 1 minuto
        status = _FakeStatus(all_time_upload=0, total_wanted=1000)
        handle = _FakeHandle(status)
        download = _fake_download()
        backend._active["fakehash"] = {"handle": handle, "download": download}
        backend._seed_started_at["fakehash"] = time.time() - 120  # llevan 2 min sembrando

        await asyncio.sleep(1.5)

        assert handle.paused_called is True
        assert download.state == DownloadState.PAUSED
    finally:
        await backend.disconnect()


@pytest.mark.asyncio
async def test_seed_limits_disabled_by_default_do_not_pause():
    backend = TorrentBackend()
    await backend.connect()
    try:
        status = _FakeStatus(all_time_upload=10_000, total_wanted=1000)  # ratio 10x
        handle = _FakeHandle(status)
        download = _fake_download()
        backend._active["fakehash"] = {"handle": handle, "download": download}
        backend._seed_started_at["fakehash"] = time.time() - 3600  # llevan 1 hora sembrando

        await asyncio.sleep(1.5)

        assert handle.paused_called is False
    finally:
        await backend.disconnect()
