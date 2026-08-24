# -*- mode: python ; coding: utf-8 -*-
# Spec de PyInstaller compartido por los tres sistemas operativos (punto
# 33.8/33.9 del backlog, ver DEVLOG.md). Genera siempre un build "onedir"
# (una carpeta con el ejecutable y sus dependencias al lado, no un único
# .exe/binario "onefile"): más lento de arrancar que onefile pero mucho más
# fiable con una dependencia binaria pesada como libtorrent, y es lo que
# luego empaqueta cada plataforma (fpm/AppImage en Linux, Inno Setup en
# Windows, `hdiutil` en macOS).
#
# Los tres PNG de la raíz (IconoCuadrado.png, Logo.png, AnabasaSoft.png) se
# añaden como datos para que `gui/resources.py` los encuentre vía
# `sys._MEIPASS`, que PyInstaller define igual en build onefile y onedir.

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
BUNDLE_VERSION = os.environ.get("VERSION", "0.0.0-dev")

datas = [
    (str(ROOT / "IconoCuadrado.png"), "."),
    (str(ROOT / "Logo.png"), "."),
    (str(ROOT / "AnabasaSoft.png"), "."),
]

icon = None
if sys.platform == "win32":
    icon = str(ROOT / "packaging" / "windows" / "p2p-total.ico")
elif sys.platform == "darwin":
    icon = str(ROOT / "packaging" / "macos" / "p2p-total.icns")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["libtorrent"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="p2p-total",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="p2p-total",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="P2P Total.app",
        icon=icon,
        bundle_identifier="org.anabasasoft.p2ptotal",
        info_plist={
            "CFBundleShortVersionString": BUNDLE_VERSION,
            "CFBundleVersion": BUNDLE_VERSION,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "AnabasaSoft",
        },
    )
