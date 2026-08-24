"""gui/i18n.py: todos los idiomas deben llevar el mismo conjunto de
claves que español (la convención del proyecto, según su propio
docstring, es traducir 1:1 en vez de depender del fallback a español
para idiomas ya soportados) y `t()` debe resolver correctamente."""

import pytest

from gui.i18n import LANGUAGES, TRANSLATIONS, get_language, set_language, t


def test_all_languages_have_the_same_keys_as_spanish():
    spanish_keys = set(TRANSLATIONS["es"].keys())
    for code, _name in LANGUAGES:
        assert code in TRANSLATIONS, f"falta el idioma {code!r} en TRANSLATIONS"
        missing = spanish_keys - set(TRANSLATIONS[code].keys())
        extra = set(TRANSLATIONS[code].keys()) - spanish_keys
        assert not missing, f"{code}: faltan claves {sorted(missing)}"
        assert not extra, f"{code}: claves de más que no existen en es {sorted(extra)}"


def test_no_language_has_empty_translations():
    for code, _name in LANGUAGES:
        for key, value in TRANSLATIONS[code].items():
            assert value != "", f"{code}.{key} está vacío"


def test_t_resolves_active_language(monkeypatch):
    original = get_language()
    try:
        set_language("en")
        assert t("app_title") == TRANSLATIONS["en"]["app_title"]
    finally:
        set_language(original)


def test_t_falls_back_to_spanish_for_unknown_language():
    original = get_language()
    try:
        set_language("xx_no_existe")
        assert get_language() == "es"
        assert t("app_title") == TRANSLATIONS["es"]["app_title"]
    finally:
        set_language(original)


def test_t_returns_key_itself_when_missing_everywhere():
    assert t("clave_que_no_existe_en_ningun_idioma") == "clave_que_no_existe_en_ningun_idioma"


def test_t_formats_kwargs():
    set_language("es")
    assert "{" not in t("update_dialog_text_auto", version="9.9")
