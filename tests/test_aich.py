"""AICH (core/aich.py, verificación de sub-bloque en eD2k)."""

import hashlib

from core.aich import EMBLOCKSIZE, PARTSIZE, block_count, block_sha1, combine_sha1, levels_to_part


def test_block_sha1_matches_hashlib():
    data = b"contenido de prueba"
    assert block_sha1(data) == hashlib.sha1(data).digest()


def test_combine_sha1_matches_hashlib_of_concatenation():
    left, right = b"\x01" * 20, b"\x02" * 20
    assert combine_sha1(left, right) == hashlib.sha1(left + right).digest()


def test_block_count_ceiling_division():
    assert block_count(0) == 0
    assert block_count(1) == 1
    assert block_count(EMBLOCKSIZE) == 1
    assert block_count(EMBLOCKSIZE + 1) == 2
    assert block_count(EMBLOCKSIZE * 3) == 3


def test_levels_to_part_single_part_file_is_zero():
    # Un fichero que cabe entero en una sola parte no tiene "verifying
    # hashes" por encima de esa parte: es la raíz del árbol.
    assert levels_to_part(PARTSIZE, 0) == 0
    assert levels_to_part(100, 0) == 0


def test_levels_to_part_two_equal_parts_both_at_depth_one():
    file_size = PARTSIZE * 2
    assert levels_to_part(file_size, 0) == 1
    assert levels_to_part(file_size, 1) == 1
