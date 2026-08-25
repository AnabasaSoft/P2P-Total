"""backends/emule_backend.py: framing de tags/paquetes del protocolo
eD2k, y el parseo de enlaces ed2k:// (todo sin abrir sockets)."""

import struct

from backends.emule_backend import (
    CT_NAME,
    OP_EDONKEYPROT,
    OP_KADEMLIAPACKEDPROT,
    OP_LOGINREQUEST,
    OP_PACKEDPROT,
    ST_DESCRIPTION,
    ST_HARDFILES,
    ST_MAXUSERS,
    ST_PING,
    ST_SERVERNAME,
    ST_SOFTFILES,
    TAGTYPE_STRING,
    TAGTYPE_UINT8,
    TAGTYPE_UINT32,
    TAGTYPE_UINT64,
    _read_wire_ip,
    build_tcp_packet,
    build_udp_packet,
    parse_ed2k_link,
    parse_server_met,
    parse_udp_packet,
    read_tag,
    write_tag,
)


def test_write_read_tag_string_round_trip():
    encoded = write_tag(CT_NAME, TAGTYPE_STRING, "Mi Nick")
    tag, offset = read_tag(encoded, 0)
    assert tag.name_id == CT_NAME
    assert tag.type == TAGTYPE_STRING
    assert tag.value == "Mi Nick"
    assert offset == len(encoded)


def test_write_read_tag_uint32_round_trip():
    encoded = write_tag(0x02, TAGTYPE_UINT32, 123456)
    tag, offset = read_tag(encoded, 0)
    assert tag.value == 123456
    assert offset == len(encoded)


def test_write_read_tag_uint64_round_trip():
    encoded = write_tag(0x03, TAGTYPE_UINT64, 9_876_543_210)
    tag, offset = read_tag(encoded, 0)
    assert tag.value == 9_876_543_210
    assert offset == len(encoded)


def test_write_read_tag_uint8_round_trip():
    encoded = write_tag(0x04, TAGTYPE_UINT8, 200)
    tag, offset = read_tag(encoded, 0)
    assert tag.value == 200
    assert offset == len(encoded)


def test_read_tag_compact_name_form():
    # Formato "compacto": el bit 0x80 del byte de tipo marca que a
    # continuación va un name_id de un solo byte, sin longitud de
    # nombre (usado por servidores/clientes reales, a diferencia del
    # formato "viejo" que generamos nosotros en write_tag).
    compact = bytes([TAGTYPE_UINT8 | 0x80, 0x05, 42])
    tag, offset = read_tag(compact, 0)
    assert tag.name_id == 0x05
    assert tag.value == 42
    assert offset == 3


def test_build_and_read_back_tcp_packet():
    payload = b"cuerpo del paquete"
    packet = build_tcp_packet(OP_EDONKEYPROT, OP_LOGINREQUEST, payload)
    assert packet[0] == OP_EDONKEYPROT
    import struct
    length = struct.unpack_from("<I", packet, 1)[0]
    assert length == 1 + len(payload)
    body = packet[5:]
    assert body[0] == OP_LOGINREQUEST
    assert body[1:] == payload


def test_build_tcp_packet_compressed_protocol_marker():
    packet = build_tcp_packet(OP_PACKEDPROT, OP_LOGINREQUEST, b"x")
    assert packet[0] == OP_PACKEDPROT


def test_udp_packet_round_trip_uncompressed():
    packet = build_udp_packet(OP_EDONKEYPROT, 0x92, b"datos udp")
    protocol, opcode, payload = parse_udp_packet(packet)
    assert (protocol, opcode, payload) == (OP_EDONKEYPROT, 0x92, b"datos udp")


def test_udp_packet_round_trip_kademlia_compressed():
    import zlib
    payload = b"datos kad sin comprimir" * 5
    packet = bytes([OP_KADEMLIAPACKEDPROT, 0x0B]) + zlib.compress(payload)
    protocol, opcode, decoded_payload = parse_udp_packet(packet)
    assert protocol == OP_KADEMLIAPACKEDPROT
    assert opcode == 0x0B
    assert decoded_payload == payload


def test_read_wire_ip_octets_in_wire_order():
    assert _read_wire_ip(bytes([192, 168, 1, 42]), 0) == "192.168.1.42"


def test_parse_ed2k_link_valid():
    file_hash_hex = "d41d8cd98f00b204e9800998ecf8427e"
    link = f"ed2k://|file|documento.pdf|1024|{file_hash_hex}|/"
    result = parse_ed2k_link(link)
    assert result == ("documento.pdf", 1024, file_hash_hex)


def test_parse_ed2k_link_url_encoded_title():
    file_hash_hex = "d41d8cd98f00b204e9800998ecf8427e"
    link = f"ed2k://|file|mi%20fichero.zip|2048|{file_hash_hex}|/"
    title, size, _hash = parse_ed2k_link(link)
    assert title == "mi fichero.zip"
    assert size == 2048


def test_parse_ed2k_link_rejects_wrong_scheme():
    assert parse_ed2k_link("magnet:?xt=urn:btih:abc") is None


def test_parse_ed2k_link_rejects_bad_hash_length():
    link = "ed2k://|file|x.zip|10|deadbeef|/"
    assert parse_ed2k_link(link) is None


def test_parse_ed2k_link_rejects_missing_parts():
    assert parse_ed2k_link("ed2k://|file|x.zip|10|") is None


def _build_server_met_entry(ip_octets: bytes, port: int, tags: bytes, tagcount: int) -> bytes:
    return ip_octets + struct.pack("<H", port) + struct.pack("<I", tagcount) + tags


def test_parse_server_met_extracts_host_port_and_known_tags():
    tags = (
        write_tag(ST_SERVERNAME, TAGTYPE_STRING, "Servidor de prueba")
        + write_tag(ST_DESCRIPTION, TAGTYPE_STRING, "Un servidor cualquiera")
        + write_tag(ST_PING, TAGTYPE_UINT32, 42)
        + write_tag(ST_MAXUSERS, TAGTYPE_UINT32, 50000)
        + write_tag(ST_SOFTFILES, TAGTYPE_UINT32, 2000000)
        + write_tag(ST_HARDFILES, TAGTYPE_UINT32, 2500000)
    )
    entry = _build_server_met_entry(bytes([85, 17, 116, 222]), 6082, tags, 6)
    data = bytes([0xE0]) + struct.pack("<I", 1) + entry

    servers = parse_server_met(data)

    assert servers == [{
        "host": "85.17.116.222",
        "port": 6082,
        "name": "Servidor de prueba",
        "description": "Un servidor cualquiera",
        "ping": 42,
        "max_users": 50000,
        "soft_files": 2000000,
        "hard_files": 2500000,
    }]


def test_parse_server_met_entry_without_tags_has_only_host_and_port():
    entry = _build_server_met_entry(bytes([1, 2, 3, 4]), 4661, b"", 0)
    data = bytes([0xE0]) + struct.pack("<I", 1) + entry

    servers = parse_server_met(data)

    assert servers == [{"host": "1.2.3.4", "port": 4661}]


def test_parse_server_met_rejects_wrong_magic_byte():
    assert parse_server_met(bytes([0x00, 0, 0, 0, 0])) == []
