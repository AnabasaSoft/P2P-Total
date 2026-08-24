"""MD4 (core/md4.py, usado por eD2k) contra los vectores de prueba
oficiales del RFC 1320, apéndice A.5."""

from core.md4 import md4

RFC1320_VECTORS = [
    (b"", "31d6cfe0d16ae931b73c59d7e0c089c0"),
    (b"a", "bde52cb31de33e46245e05fbdbd6fb24"),
    (b"abc", "a448017aaf21d8525fc10ae87aa6729d"),
    (b"message digest", "d9130a8164549fe818874806e1c7014b"),
    (b"abcdefghijklmnopqrstuvwxyz", "d79e1c308aa5bbcdeea8ed63df412da9"),
    (b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
     "043f8582f241db351ce627e153e7f0e4"),
    (b"12345678901234567890123456789012345678901234567890123456789012345678901234567890",
     "e33b4ddc9c38f2199c3e7b164fcc0536"),
]


def test_rfc1320_vectors():
    for data, expected_hex in RFC1320_VECTORS:
        assert md4(data).hexdigest() == expected_hex


def test_update_incremental_matches_single_shot():
    h1 = md4(b"abcdefghijklmnopqrstuvwxyz")
    h2 = md4()
    for chunk in (b"abcde", b"fghij", b"klmnopqrstuvwxyz"):
        h2.update(chunk)
    assert h1.digest() == h2.digest()


def test_copy_is_independent():
    h1 = md4(b"abc")
    h2 = h1.copy()
    h2.update(b"def")
    assert h1.hexdigest() != h2.hexdigest()
    assert h1.hexdigest() == md4(b"abc").hexdigest()
