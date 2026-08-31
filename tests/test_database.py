"""Punto 43 del backlog: tabla de correlación local de hashes entre
redes (sha1 <-> ed2k <-> infohash). No sustituye ni mezcla los hashes
de cada protocolo -son incompatibles entre sí, ver DEVLOG.md-, solo
guarda qué tripletas corresponden al mismo contenido ya visto con más
de un hash calculado."""

from core import database


def test_record_and_find_hash_correlation_by_any_known_hash():
    database.init_db()
    sha1 = b"s" * 20
    ed2k = b"e" * 16
    database.record_hash_correlation(1000, sha1=sha1, ed2k=ed2k)

    expected = {"size_bytes": 1000, "sha1": sha1, "ed2k": ed2k, "infohash": None}
    assert database.find_hash_correlation(sha1=sha1) == expected
    assert database.find_hash_correlation(ed2k=ed2k) == expected


def test_record_hash_correlation_fills_in_missing_hash_on_existing_row():
    database.init_db()
    sha1 = b"s" * 20
    infohash = b"i" * 20
    database.record_hash_correlation(1000, sha1=sha1)
    database.record_hash_correlation(1000, sha1=sha1, infohash=infohash)

    row = database.find_hash_correlation(sha1=sha1)
    assert row["infohash"] == infohash


def test_record_hash_correlation_never_overwrites_an_existing_different_hash():
    """Si el hash nuevo no coincide con el que ya había para ese
    tamaño+hash conocido, es que `size_bytes` ha coincidido por
    casualidad entre dos contenidos distintos -no el mismo fichero-,
    así que la fila existente no se toca."""
    database.init_db()
    sha1 = b"s" * 20
    database.record_hash_correlation(1000, sha1=sha1, ed2k=b"e" * 16)
    database.record_hash_correlation(1000, sha1=sha1, ed2k=b"x" * 16)

    row = database.find_hash_correlation(sha1=sha1)
    assert row["ed2k"] == b"e" * 16


def test_find_hash_correlation_returns_none_when_never_seen():
    database.init_db()
    assert database.find_hash_correlation(sha1=b"z" * 20) is None
