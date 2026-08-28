"""core/proxy.py: codificación de direcciones SOCKS5 (sin conectar a
ningún proxy real)."""

import pytest

from core.ip_filter import ip_filter
from core.proxy import _encode_socks5_address, open_connection


def test_encode_ipv4_uses_atyp_1():
    encoded = _encode_socks5_address("192.168.1.1")
    assert encoded[0] == 0x01
    assert encoded[1:] == bytes([192, 168, 1, 1])


def test_encode_ipv6_uses_atyp_4():
    encoded = _encode_socks5_address("::1")
    assert encoded[0] == 0x04
    assert len(encoded) == 1 + 16


def test_encode_domain_uses_atyp_3_with_length_prefix():
    encoded = _encode_socks5_address("example.org")
    assert encoded[0] == 0x03
    assert encoded[1] == len(b"example.org")
    assert encoded[2:] == b"example.org"


@pytest.mark.asyncio
async def test_open_connection_refuses_ip_blocked_by_filter(tmp_path):
    path = tmp_path / "ipfilter.dat"
    path.write_text("1.2.3.0 - 1.2.3.255 , 050 , bloqueado\n", encoding="utf-8")
    ip_filter.load(str(path))
    ip_filter.configure(enabled=True, level_threshold=127)
    try:
        with pytest.raises(ConnectionRefusedError):
            await open_connection("1.2.3.42", 12345)
    finally:
        ip_filter.configure(enabled=False)
