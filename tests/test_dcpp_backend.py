"""backends/dcpp_backend.py: funciones puras de parseo/codificación del
protocolo NMDC (sin abrir ningún socket)."""

from backends.dcpp_backend import (
    escape_nmdc,
    lock_to_key,
    parse_dchub_link,
    parse_search_string,
    parse_sr,
    tth_from_base32,
    tth_to_base32,
)


def test_lock_to_key_matches_algorithm_traced_by_hand():
    # lock = [1,2,3,4,5]: key[0] = 1^5^4^5 = 5, key[i] = lock[i]^lock[i-1]
    # para i=1..4 -> [3,1,7,1]; tras el intercambio de nibbles de cada
    # byte (ninguno cae en los que hay que escapar) sale [80,48,16,112,16].
    lock = bytes([1, 2, 3, 4, 5])
    assert lock_to_key(lock) == bytes([80, 48, 16, 112, 16])


def test_lock_to_key_escapes_special_bytes():
    # lock = [5,5]: key[0] = 5^5^5^5 = 0, key[1] = 5^5 = 0. El
    # intercambio de nibbles de un byte 0 lo deja en 0, que está en el
    # mapa de escape (_ESCAPE_MAP) y debe salir como "/%DCN000%/" para
    # cada uno de los dos bytes de la key.
    key = lock_to_key(bytes([5, 5]))
    assert key == b"/%DCN000%//%DCN000%/"


def test_lock_to_key_too_short_raises():
    import pytest
    with pytest.raises(ValueError):
        lock_to_key(b"A")


def test_lock_to_key_deterministic():
    lock = b"SOME_TEST_LOCK_VALUE_1234567890"
    assert lock_to_key(lock) == lock_to_key(lock)


def test_escape_nmdc():
    assert escape_nmdc("precio: $5 | gratis") == "precio: &#36;5 &#124; gratis"
    assert escape_nmdc("A & B") == "A &amp; B"


def test_parse_search_string_replaces_spaces_with_dollar():
    assert parse_search_string("hello world") == "F?F?0?1?hello$world"


def test_parse_search_string_strips_surrounding_whitespace():
    assert parse_search_string("  hola  ") == "F?F?0?1?hola"


def test_parse_sr_valid_line():
    line = "$SR nick ponies.txt\x05437 3/4\x05Testhub (192.168.1.1:411)|"
    result = parse_sr(line)
    assert result == {
        "nick": "nick",
        "filename": "ponies.txt",
        "filesize": 437,
        "free_slots": 3,
        "total_slots": 4,
        "hub_info": "Testhub (192.168.1.1:411)",
    }


def test_parse_sr_not_an_sr_line():
    assert parse_sr("$MyNick alguien|") is None


def test_parse_sr_malformed_returns_none():
    assert parse_sr("$SR nick sin el resto del formato|") is None


def test_tth_base32_round_trip():
    tth = bytes(range(24))
    b32 = tth_to_base32(tth)
    assert tth_from_base32(b32) == tth


def test_parse_dchub_link_with_port():
    assert parse_dchub_link("dchub://example.org:4111") == ("example.org", 4111)


def test_parse_dchub_link_without_port_defaults_to_411():
    assert parse_dchub_link("dchub://example.org") == ("example.org", 411)


def test_parse_dchub_link_ipv6_literal():
    assert parse_dchub_link("dchub://[2001:db8::1]:411") == ("2001:db8::1", 411)


def test_parse_dchub_link_rejects_other_schemes():
    assert parse_dchub_link("http://example.org") is None


def test_parse_dchub_link_rejects_empty_address():
    assert parse_dchub_link("dchub://") is None
