"""core/update_checker.py: comparación de versiones y la lógica de
"hay una versión más nueva" contra un cuerpo de la API de GitHub
simulado (sin llamar nunca a la red real, monkeypatch de http_get)."""

import json

import pytest

import core.update_checker as update_checker
from core.update_checker import UpdateInfo, _parse_version, check_for_update
from core.version import VERSION


def test_parse_version_with_v_prefix():
    assert _parse_version("v1.2.3") == (1, 2, 3)


def test_parse_version_without_prefix():
    assert _parse_version("1.2") == (1, 2)


def test_parse_version_ignores_non_numeric_suffix():
    assert _parse_version("v1.2.3-beta") == (1, 2, 3)


def test_parse_version_ordering():
    assert _parse_version("v1.10.0") > _parse_version("v1.9.0")
    assert _parse_version("v2.0.0") > _parse_version("v1.99.0")


def _fake_http_get_returning(body: bytes):
    async def fake(*args, **kwargs):
        return body
    return fake


@pytest.mark.asyncio
async def test_check_for_update_returns_none_when_no_newer_version(monkeypatch):
    body = json.dumps({"tag_name": f"v{VERSION}", "html_url": "https://example.invalid"}).encode()
    monkeypatch.setattr(update_checker, "http_get", _fake_http_get_returning(body))
    assert await check_for_update() is None


@pytest.mark.asyncio
async def test_check_for_update_returns_info_when_newer_version_available(monkeypatch):
    newer = f"{_parse_version(VERSION)[0] + 1}.0"  # cualquier versión mayor sirve para la comparación
    body = json.dumps({
        "tag_name": f"v{newer}",
        "html_url": "https://example.invalid/releases/latest",
        "assets": [{"name": "P2P-Total-x86_64.AppImage", "browser_download_url": "https://example.invalid/a"}],
    }).encode()
    monkeypatch.setattr(update_checker, "http_get", _fake_http_get_returning(body))
    info = await check_for_update()
    assert isinstance(info, UpdateInfo)
    assert info.version == newer
    assert info.release_url == "https://example.invalid/releases/latest"
    assert info.assets[0]["name"] == "P2P-Total-x86_64.AppImage"


@pytest.mark.asyncio
async def test_check_for_update_returns_none_on_network_error(monkeypatch):
    async def raise_error(*args, **kwargs):
        raise OSError("sin conexión")
    monkeypatch.setattr(update_checker, "http_get", raise_error)
    assert await check_for_update() is None


@pytest.mark.asyncio
async def test_check_for_update_returns_none_when_tag_missing(monkeypatch):
    body = json.dumps({"html_url": "https://example.invalid"}).encode()
    monkeypatch.setattr(update_checker, "http_get", _fake_http_get_returning(body))
    assert await check_for_update() is None
