"""backends/soulseek_backend.py: codificación/decodificación de campos
del protocolo binario de Soulseek (sin abrir ningún socket)."""

from backends.soulseek_backend import (
    _decode_source_id,
    _encode_source_id,
    _Unpacker,
    pack_bool,
    pack_string,
    pack_uint8,
    pack_uint32,
    pack_uint64,
)


def test_encode_decode_source_id_round_trip():
    encoded = _encode_source_id("usuario", "carpeta\\archivo.mp3")
    assert _decode_source_id(encoded) == ("usuario", "carpeta\\archivo.mp3")


def test_pack_uint8_little_endian_single_byte():
    assert pack_uint8(200) == bytes([200])


def test_pack_uint32_and_unpacker_round_trip():
    data = pack_uint32(0x01020304)
    up = _Unpacker(data)
    assert up.uint32() == 0x01020304


def test_pack_uint64_and_unpacker_round_trip():
    data = pack_uint64(9_876_543_210)
    up = _Unpacker(data)
    assert up.uint64() == 9_876_543_210


def test_pack_bool_true_and_false():
    assert pack_bool(True) == bytes([1])
    assert pack_bool(False) == bytes([0])


def test_pack_string_and_unpacker_round_trip():
    data = pack_string("hola mundo")
    up = _Unpacker(data)
    assert up.string() == "hola mundo"


def test_unpacker_sequential_reads():
    data = pack_uint32(3) + pack_string("abc") + pack_uint8(1)
    up = _Unpacker(data)
    assert up.uint32() == 3
    assert up.string() == "abc"
    assert up.boolean() is True


def test_unpacker_ip_reverses_byte_order():
    up = _Unpacker(bytes([1, 2, 3, 4]))
    assert up.ip() == "4.3.2.1"


def test_unpacker_login_success_reply_sequence():
    # Formato real de la respuesta de login con éxito: booleano, mensaje
    # de bienvenida (MOTD) y la IP externa propia tal como la ve el
    # servidor -la secuencia que lee SoulseekBackend.connect() tras el
    # booleano de éxito para rellenar get_stats()["external_ip"].
    data = pack_bool(True) + pack_string("bienvenido") + bytes([1, 2, 3, 4])
    up = _Unpacker(data)
    assert up.boolean() is True
    assert up.string() == "bienvenido"
    assert up.ip() == "4.3.2.1"


def test_unpacker_file_size_normal():
    up = _Unpacker((5_000_000_000).to_bytes(8, "little"))
    assert up.file_size() == 5_000_000_000


def test_unpacker_file_size_soulseek_ns_bug_workaround():
    # Bug histórico de Soulseek NS: para >2GiB el byte más
    # significativo vale 0xFF y el resto son 8 bytes de basura salvo
    # los primeros 4, que sí llevan el tamaño real.
    raw = (123456).to_bytes(4, "little") + b"\xAA\xAA\xAA\xFF"
    up = _Unpacker(raw)
    assert up.file_size() == 123456
