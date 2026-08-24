"""backends/torrent_backend.py: funciones puras de resolución de
query/magnet (sin tocar libtorrent ni la red), más el punto 34.3 del
backlog (cifrado MSE/PE y µTP), que sí se prueba contra una
`lt.session` real -pero local, sin conectar a ningún peer- porque es
la propia librería `libtorrent` la que aplica esas políticas al
crear la sesión."""

import libtorrent as lt
import pytest

from backends.torrent_backend import (
    TorrentBackend,
    _build_magnet,
    _infohash_from_magnet,
    _is_direct_reference,
    _is_torrent_file,
    _to_magnet,
)

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


def test_get_stats_empty_dict_when_not_connected():
    backend = TorrentBackend()
    assert backend.get_stats() == {}
