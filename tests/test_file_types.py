"""core/file_types.py: filtro de tipo de archivo por extensión."""

from core.file_types import FILE_TYPE_EXTENSIONS, matches_file_type


def test_all_matches_anything():
    assert matches_file_type("sin_extension", "all")
    assert matches_file_type("cancion.mp3", "all")


def test_matches_known_extension_case_insensitive():
    assert matches_file_type("cancion.MP3", "audio")
    assert matches_file_type("pelicula.mkv", "video")


def test_does_not_match_wrong_category():
    assert not matches_file_type("cancion.mp3", "video")


def test_no_extension_never_matches_a_real_category():
    assert not matches_file_type("sin_extension", "audio")


def test_unknown_file_type_matches_nothing():
    assert not matches_file_type("cancion.mp3", "categoria_inexistente")


def test_every_extension_is_lowercase_and_unique_per_category_key():
    for category, extensions in FILE_TYPE_EXTENSIONS.items():
        for ext in extensions:
            assert ext == ext.lower()
