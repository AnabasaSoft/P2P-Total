"""Tiger/192 (core/tiger.py) y Tiger Tree Hash (core/tth.py, usado por
DC++ para identificar ficheros). El docstring de core/tiger.py indica
que las tablas S-box ya se validaron bit a bit contra la implementación
de referencia en C de RHash; estos tests son de regresión (golden
master) contra la propia salida ya validada de la implementación
actual, para detectar si un cambio futuro la rompe."""

from base64 import b32encode

from core.tiger import tiger
from core.tth import tth_of_bytes


def test_tiger_empty():
    assert tiger(b"").hexdigest() == "3293ac630c13f0245f92bbb1766e16167a4e58492dde73f3"


def test_tiger_golden_values():
    assert tiger(b"abc").hexdigest() == "2aab1484e8c158f2bfb8c5ff41b57a525129131c957b5f93"
    assert tiger(b"a" * 1000).hexdigest() == "42c18814a47b257c40160a80fbe604d949613ee029b31fd9"
    assert tiger(b"a" * 1024).hexdigest() == "96cd77a3d4a04d0cc85ee0297e2984c76ab723f8c5447f4d"
    assert tiger(b"a" * 1025).hexdigest() == "b1d1a021d0c0152681db180f9bd38d3720cd328a3e7a7fc4"


def test_tth_golden_values():
    cases = {
        0: "5d9ed00a030e638bdb753a6a24fb900e5a63b8e73e6c25b6",
        1024: "59ce1b3b30a5b94e2bf60ca89f754f21c2097c258bb05046",
        1025: "520c790a07eb46a676d82275e7dbe9c55546818fb4920ac9",
        2048: "f7c556e3fb93f421569f2e6e2b1fda273a95b4e66eb9810b",
        3000: "8617d6eda2bd9c131842f0d8ccb51dc43603dc0fb02f606d",
    }
    for size, expected_hex in cases.items():
        assert tth_of_bytes(b"x" * size).hex() == expected_hex


def test_tth_base32_round_trip_via_dcpp_helpers():
    from backends.dcpp_backend import tth_from_base32, tth_to_base32

    digest = tth_of_bytes(b"x" * 1024)
    b32 = tth_to_base32(digest)
    assert b32 == b32encode(digest).decode("ascii").rstrip("=")
    assert tth_from_base32(b32) == digest


def test_tth_different_sizes_give_different_hashes():
    assert tth_of_bytes(b"x" * 1024) != tth_of_bytes(b"x" * 1025)
