"""backends/g2_backend.py: códec de trama G2 (encode/decode_packet),
vlint y direcciones de 6 bytes, sin abrir ningún socket."""

import pytest

from backends.g2_backend import (
    G2Packet,
    _decode_vlint,
    _encode_g2_address,
    _encode_length,
    _encode_vlint,
    _parse_address,
    _sanitize_filename,
    decode_packet,
    encode_packet,
    sha1_to_urn_base32,
    whole_packet_length,
)


def test_encode_length_zero_bytes_when_zero():
    assert _encode_length(0) == b""


def test_encode_length_uses_fewest_bytes_needed():
    assert len(_encode_length(0xFF)) == 1
    assert len(_encode_length(0x100)) == 2
    assert len(_encode_length(0x10000)) == 3


def test_encode_length_too_big_raises():
    with pytest.raises(ValueError):
        _encode_length(0xFFFFFFFF)


def test_packet_name_length_validation():
    with pytest.raises(ValueError):
        G2Packet("")
    with pytest.raises(ValueError):
        G2Packet("DEMASIADOLARGO")


def test_encode_decode_round_trip_leaf_packet():
    pkt = G2Packet("DN", payload=b"busqueda de prueba")
    encoded = encode_packet(pkt)
    decoded, consumed = decode_packet(encoded)
    assert decoded == pkt
    assert consumed == len(encoded)


def test_encode_decode_round_trip_with_children():
    child1 = G2Packet("DN", payload=b"nombre")
    child2 = G2Packet("I", payload=b"*")
    pkt = G2Packet("Q2", payload=b"0123456789012345", children=[child1, child2])
    encoded = encode_packet(pkt)
    decoded, consumed = decode_packet(encoded)
    assert decoded == pkt
    assert consumed == len(encoded)


def test_encode_decode_round_trip_no_payload_no_children():
    pkt = G2Packet("PI")
    encoded = encode_packet(pkt)
    decoded, consumed = decode_packet(encoded)
    assert decoded == pkt
    assert consumed == len(encoded)


def test_decode_packet_end_of_stream_marker():
    decoded, pos = decode_packet(b"\x00resto")
    assert decoded is None
    assert pos == 1


def test_whole_packet_length_matches_encoded_size():
    pkt = G2Packet("Q2", payload=b"0123456789012345", children=[G2Packet("DN", payload=b"x")])
    encoded = encode_packet(pkt)
    assert whole_packet_length(encoded) == len(encoded)


def test_whole_packet_length_none_when_incomplete_header():
    assert whole_packet_length(b"") is None


def test_whole_packet_length_none_when_body_not_fully_available():
    pkt = G2Packet("DN", payload=b"algo mas largo de lo que cabe en poco")
    encoded = encode_packet(pkt)
    assert whole_packet_length(encoded[:2]) is None


def test_vlint_round_trip():
    for value in (0, 1, 255, 256, 65535, 65536, 123456789):
        assert _decode_vlint(_encode_vlint(value)) == value


def test_encode_vlint_zero_is_empty():
    assert _encode_vlint(0) == b""


def test_g2_address_round_trip():
    ip, port = _parse_address(_encode_g2_address("192.168.1.42", 6346))
    assert ip == "192.168.1.42"
    assert port == 6346


def test_parse_address_wrong_length_returns_none():
    assert _parse_address(b"\x01\x02\x03") is None


def test_sanitize_filename_strips_path_separators_and_traversal():
    # Los separadores de ruta se sustituyen por "_" (no se intenta
    # quedar solo con el último componente): el resultado nunca puede
    # contener "/" ni "\\", así que no hay forma de que un nombre
    # malicioso escriba fuera de dest_path.
    assert _sanitize_filename("../../etc/passwd") == ".._.._etc_passwd"
    assert _sanitize_filename("C:\\Windows\\evil.exe") == "C:_Windows_evil.exe"
    assert "/" not in _sanitize_filename("../../etc/passwd")
    assert "\\" not in _sanitize_filename("C:\\Windows\\evil.exe")


def test_sanitize_filename_strips_null_bytes():
    assert _sanitize_filename("archivo\x00.txt") == "archivo.txt"


def test_sanitize_filename_empty_falls_back_to_default():
    assert _sanitize_filename("") == "descarga_sin_nombre"
    assert _sanitize_filename("   ") == "descarga_sin_nombre"


def test_sha1_to_urn_base32_no_padding():
    b32 = sha1_to_urn_base32(b"\x00" * 20)
    assert "=" not in b32
    assert len(b32) == 32
