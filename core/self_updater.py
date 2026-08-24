"""Mecanismo real de auto-actualización (punto 34.1 del backlog, ver
DEVLOG.md): antes `core/update_checker.py` solo avisaba con un enlace a
la release de GitHub sin descargar ni instalar nada. Aquí se añaden la
descarga y la sustitución automática del paquete instalado, cuando el
tipo de instalación lo permite:

- AppImage (Linux): se sustituye el propio fichero `.AppImage` en su
  sitio (mismo mecanismo que usa AppImageUpdate) y se relanza.
- Instalador de Windows (Inno Setup, sobre el build "onedir"): se
  descarga el nuevo instalador y se ejecuta en modo silencioso (con el
  verbo "runas" de ShellExecute, para pedir la elevación de UAC que
  necesita instalar en Program Files), que cierra la app, reemplaza los
  ficheros y la vuelve a abrir sola
  (`CloseApplications`/`RestartApplications` en `installer.iss`). Si el
  usuario cancela el aviso de UAC, salta una excepción ahí mismo y la
  app que llama a esto nunca llega a cerrarse sin haber actualizado
  nada.
- macOS (`.app` dentro de un `.dmg`): se monta el `.dmg` con la propia
  herramienta del sistema `hdiutil` (no es un "cliente P2P externo",
  es parte del propio macOS), se sustituye el `.app` en `/Applications`
  y se relanza.

Los paquetes `.deb`/`.rpm` (instalados en `/usr`, gestionados por
`apt`/`dnf`) y el `.flatpak` autónomo (gestionado por Flatpak) NO se
pueden auto-actualizar de forma segura desde dentro de la propia app
— requerirían privilegios de root o pisar lo que gestiona otra
herramienta —, así que `can_self_update()` devuelve `False` para ellos
y la GUI cae de vuelta al aviso con enlace de siempre."""

import enum
import os
import shutil
import subprocess
import sys
from pathlib import Path


class InstallKind(enum.Enum):
    APPIMAGE = "appimage"
    WINDOWS_ONEDIR = "windows_onedir"
    MACOS_APP = "macos_app"
    LINUX_PACKAGE = "linux_package"  # .deb/.rpm instalados en /usr: no auto-actualizable
    FLATPAK = "flatpak"              # gestionado por Flatpak: no auto-actualizable
    SOURCE = "source"                # ejecutado desde código fuente (desarrollo): no auto-actualizable


def detect_install_kind() -> InstallKind:
    """Determina cómo se está ejecutando la app actual, para saber si
    (y cómo) se puede auto-actualizar."""
    if os.environ.get("APPIMAGE"):
        return InstallKind.APPIMAGE
    if os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists():
        return InstallKind.FLATPAK
    if not getattr(sys, "frozen", False):
        return InstallKind.SOURCE
    if sys.platform == "win32":
        return InstallKind.WINDOWS_ONEDIR
    if sys.platform == "darwin":
        return InstallKind.MACOS_APP
    return InstallKind.LINUX_PACKAGE


def can_self_update(kind: InstallKind) -> bool:
    return kind in (InstallKind.APPIMAGE, InstallKind.WINDOWS_ONEDIR, InstallKind.MACOS_APP)


def find_update_asset(assets: list[dict], kind: InstallKind) -> dict | None:
    """Busca en `assets` (tal cual los da la API de releases de
    GitHub) el fichero que corresponde a este tipo de instalación."""
    for asset in assets:
        name = asset.get("name", "")
        if kind is InstallKind.APPIMAGE and name.endswith("-x86_64.AppImage"):
            return asset
        if kind is InstallKind.WINDOWS_ONEDIR and name.startswith("P2P-Total-Setup-") and name.endswith(".exe"):
            return asset
        if kind is InstallKind.MACOS_APP and name.endswith(".dmg"):
            return asset
    return None


def apply_update_and_relaunch(kind: InstallKind, downloaded_path: Path) -> None:
    """Sustituye la instalación actual por `downloaded_path` y arranca
    el relanzamiento. Para `APPIMAGE` sustituye el proceso actual (vía
    `os.execv`) y por tanto no vuelve nunca si tiene éxito; para los
    otros dos casos lanza el instalador/relanzamiento como proceso
    aparte y sí que vuelve, dejando que quien la llame cierre la
    ventana con normalidad (guardando configuración, desconectando
    redes...) antes de que el proceso Python actual termine."""
    if kind is InstallKind.APPIMAGE:
        _apply_appimage(downloaded_path)
    elif kind is InstallKind.WINDOWS_ONEDIR:
        _apply_windows_installer(downloaded_path)
    elif kind is InstallKind.MACOS_APP:
        _apply_macos_dmg(downloaded_path)
    else:
        raise ValueError(f"Tipo de instalación no auto-actualizable: {kind}")


def _apply_appimage(downloaded_path: Path) -> None:
    current_path = Path(os.environ["APPIMAGE"])
    downloaded_path.chmod(0o755)
    os.replace(downloaded_path, current_path)
    os.execv(str(current_path), [str(current_path)])


def _apply_windows_installer(downloaded_path: Path) -> None:
    # El instalador se instala en Program Files (DefaultDirName en
    # installer.iss), así que necesita privilegios de administrador.
    # `subprocess.Popen` (CreateProcess) NO pide elevación por sí solo:
    # fallaría directamente con "WinError 740: se requiere elevación"
    # sin llegar a mostrar el aviso de Control de cuentas de usuario. Hay
    # que lanzarlo con el verbo "runas" de ShellExecute (`os.startfile`),
    # que sí muestra el aviso de UAC y, si el usuario lo cancela, lanza
    # una excepción ahí mismo — la app que llama a esta función ya lo
    # captura y nunca llega a cerrarse sin haber actualizado nada.
    args = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS"
    os.startfile(str(downloaded_path), "runas", args)


def _apply_macos_dmg(downloaded_path: Path) -> None:
    mount_point = Path("/Volumes/P2P Total (actualización)")
    subprocess.run(
        ["hdiutil", "attach", str(downloaded_path), "-nobrowse", "-mountpoint", str(mount_point), "-quiet"],
        check=True,
    )
    dest_app = None
    try:
        app_bundle = next(mount_point.glob("*.app"))
        dest_app = Path("/Applications") / app_bundle.name
        if dest_app.exists():
            shutil.rmtree(dest_app)
        shutil.copytree(app_bundle, dest_app)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount_point), "-quiet"], check=False)
    subprocess.Popen(["open", "-n", str(dest_app)])
