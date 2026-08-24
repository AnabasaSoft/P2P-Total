"""core/proxy.py: codificación de direcciones SOCKS5 (sin conectar a
ningún proxy real)."""

from core.proxy import _encode_socks5_address


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
