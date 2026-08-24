"""core/self_updater.py (punto 34.1 del backlog): detección del tipo de
instalación, elección del asset correcto y aplicación de la
actualización para cada plataforma, con las llamadas al sistema
operativo (`os.execv`, `os.startfile`, `subprocess`, `hdiutil`)
sustituidas por dobles (monkeypatch) — nunca se lanza un proceso ni se
toca una instalación real desde estos tests."""

import sys

import pytest

import core.self_updater as self_updater
from core.self_updater import InstallKind, can_self_update, detect_install_kind, find_update_asset


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delenv("FLATPAK_ID", raising=False)


def test_detect_appimage(monkeypatch):
    monkeypatch.setenv("APPIMAGE", "/tmp/P2P-Total-x86_64.AppImage")
    assert detect_install_kind() == InstallKind.APPIMAGE


def test_detect_flatpak_by_env_var(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "com.anabasasoft.P2PTotal")
    assert detect_install_kind() == InstallKind.FLATPAK


def test_detect_flatpak_by_info_file(monkeypatch):
    monkeypatch.setattr(self_updater.Path, "exists", lambda self: str(self) == "/.flatpak-info")
    assert detect_install_kind() == InstallKind.FLATPAK


def test_detect_source_when_not_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert detect_install_kind() == InstallKind.SOURCE


def test_detect_windows_onedir_when_frozen_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert detect_install_kind() == InstallKind.WINDOWS_ONEDIR


def test_detect_macos_app_when_frozen_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert detect_install_kind() == InstallKind.MACOS_APP


def test_detect_linux_package_when_frozen_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert detect_install_kind() == InstallKind.LINUX_PACKAGE


@pytest.mark.parametrize("kind,expected", [
    (InstallKind.APPIMAGE, True),
    (InstallKind.WINDOWS_ONEDIR, True),
    (InstallKind.MACOS_APP, True),
    (InstallKind.LINUX_PACKAGE, False),
    (InstallKind.FLATPAK, False),
    (InstallKind.SOURCE, False),
])
def test_can_self_update(kind, expected):
    assert can_self_update(kind) is expected


_REALISTIC_ASSETS = [
    {"name": "P2P-Total-1.1-x86_64.AppImage", "browser_download_url": "https://example.invalid/a"},
    {"name": "P2P-Total-Setup-1.1.exe", "browser_download_url": "https://example.invalid/b"},
    {"name": "P2P-Total-1.1.dmg", "browser_download_url": "https://example.invalid/c"},
    {"name": "p2p-total_1.1_amd64.deb", "browser_download_url": "https://example.invalid/d"},
    {"name": "p2p-total-1.1.x86_64.rpm", "browser_download_url": "https://example.invalid/e"},
    {"name": "P2P-Total.flatpak", "browser_download_url": "https://example.invalid/f"},
]


def test_find_update_asset_matches_appimage():
    asset = find_update_asset(_REALISTIC_ASSETS, InstallKind.APPIMAGE)
    assert asset["name"] == "P2P-Total-1.1-x86_64.AppImage"


def test_find_update_asset_matches_windows_installer():
    asset = find_update_asset(_REALISTIC_ASSETS, InstallKind.WINDOWS_ONEDIR)
    assert asset["name"] == "P2P-Total-Setup-1.1.exe"


def test_find_update_asset_matches_macos_dmg():
    asset = find_update_asset(_REALISTIC_ASSETS, InstallKind.MACOS_APP)
    assert asset["name"] == "P2P-Total-1.1.dmg"


def test_find_update_asset_none_for_non_self_updatable_kinds():
    assert find_update_asset(_REALISTIC_ASSETS, InstallKind.LINUX_PACKAGE) is None
    assert find_update_asset(_REALISTIC_ASSETS, InstallKind.FLATPAK) is None


def test_find_update_asset_none_when_nothing_matches():
    assert find_update_asset([{"name": "otra-cosa.txt"}], InstallKind.APPIMAGE) is None


def test_apply_appimage_replaces_file_and_relaunches(monkeypatch, tmp_path):
    current = tmp_path / "P2P-Total-current.AppImage"
    current.write_bytes(b"version vieja")
    downloaded = tmp_path / "P2P-Total-new.AppImage"
    downloaded.write_bytes(b"version nueva")
    monkeypatch.setenv("APPIMAGE", str(current))

    execv_calls = []
    monkeypatch.setattr(self_updater.os, "execv", lambda path, args: execv_calls.append((path, args)))

    self_updater.apply_update_and_relaunch(InstallKind.APPIMAGE, downloaded)

    assert current.read_bytes() == b"version nueva"
    assert not downloaded.exists()
    assert execv_calls == [(str(current), [str(current)])]


def test_apply_windows_installer_uses_runas_verb_for_uac(monkeypatch, tmp_path):
    downloaded = tmp_path / "P2P-Total-Setup-1.1.exe"
    downloaded.write_bytes(b"instalador falso")

    startfile_calls = []
    monkeypatch.setattr(
        self_updater.os, "startfile",
        lambda path, verb, args: startfile_calls.append((path, verb, args)),
        raising=False,
    )

    self_updater.apply_update_and_relaunch(InstallKind.WINDOWS_ONEDIR, downloaded)

    assert len(startfile_calls) == 1
    path, verb, args = startfile_calls[0]
    assert path == str(downloaded)
    assert verb == "runas"  # el verbo que pide elevación de UAC


def test_apply_windows_installer_propagates_uac_cancellation(monkeypatch, tmp_path):
    downloaded = tmp_path / "P2P-Total-Setup-1.1.exe"
    downloaded.write_bytes(b"instalador falso")

    def fake_startfile(path, verb, args):
        raise OSError(1223, "El usuario canceló la operación")

    monkeypatch.setattr(self_updater.os, "startfile", fake_startfile, raising=False)

    with pytest.raises(OSError):
        self_updater.apply_update_and_relaunch(InstallKind.WINDOWS_ONEDIR, downloaded)


def test_apply_update_and_relaunch_rejects_non_self_updatable_kind(tmp_path):
    with pytest.raises(ValueError):
        self_updater.apply_update_and_relaunch(InstallKind.LINUX_PACKAGE, tmp_path / "x")
