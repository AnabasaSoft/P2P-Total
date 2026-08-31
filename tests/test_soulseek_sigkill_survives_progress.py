"""Verificación pedida por el usuario tras el bug real de BitTorrent
(ver test_torrent_disconnect_flushes_progress.py y la entrada
correspondiente en DEVLOG.md): "verifica que las 5 redes no tengan
pérdidas de datos entre sesiones". La causa de aquel bug era muy
específica de BitTorrent -un caché de escritura interno de libtorrent,
en C++, fuera del control de Python/el SO, que solo se vuelca a disco
en el destructor de `lt.session`- y no aplica a Soulseek, que escribe
cada trozo recibido directamente con `f.write(chunk)` de Python sobre
un fichero abierto con `open()`, es decir, entregando los datos al
caché de páginas del propio sistema operativo, que sobrevive a la
muerte del proceso (incluso a un `SIGKILL` en seco) porque el SO, no
el proceso que muere, decide cuándo vuelca esas páginas a disco de
verdad.

Este test reproduce el peor caso posible -mucho más agresivo que
cerrar la app de forma normal- para comprobarlo empíricamente en vez
de fiarse solo de la lectura del código: un proceso hijo aparte
descarga de verdad un fichero por loopback usando
`SoulseekBackend._handle_incoming_file_connection`, y en cuanto lleva
~15% se lo mata con `SIGKILL` (sin ninguna oportunidad de ejecutar
código de limpieza, ni siquiera un `finally`). Comprueba que lo que
haya quedado en disco sea correcto byte a byte contra el contenido
real -sin huecos ni corrupción- y que la única pérdida posible sea, a
lo sumo, la del último trozo aún en el buffer de escritura de Python
sin volcar (un problema de rendimiento menor, nada que ver con la
pérdida total de progreso que sí sufría BitTorrent."""

import asyncio
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

_CHILD_SCRIPT = Path(__file__).with_name("_soulseek_sigkill_child.py")
_SIZE_BYTES = 4_000_000


async def _serve_slowly(content: bytes, reader, writer) -> None:
    writer.write(struct.pack("<I", 99))
    await writer.drain()
    offset_bytes = await reader.readexactly(8)
    offset = struct.unpack("<Q", offset_bytes)[0]
    remaining = content[offset:]
    pos = 0
    while pos < len(remaining):
        chunk = remaining[pos:pos + 8192]
        writer.write(chunk)
        await writer.drain()
        pos += len(chunk)
        # despacio a propósito, para poder matar al hijo a mitad de la descarga
        await asyncio.sleep(0.02)
    writer.close()


@pytest.mark.asyncio
async def test_sigkill_mid_download_leaves_no_corruption(tmp_path):
    content = os.urandom(_SIZE_BYTES)
    dest_dir = tmp_path / "descarga"
    dest_dir.mkdir()
    progress_file = tmp_path / "progress.txt"

    server = await asyncio.start_server(
        lambda r, w: _serve_slowly(content, r, w), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    async with server:
        asyncio.ensure_future(server.serve_forever())

        child = subprocess.Popen([
            sys.executable, str(_CHILD_SCRIPT), str(port), str(dest_dir),
            str(progress_file), str(_SIZE_BYTES),
        ])
        try:
            deadline = time.monotonic() + 20
            last_progress = 0
            while time.monotonic() < deadline:
                if progress_file.exists():
                    txt = progress_file.read_text().strip()
                    if txt:
                        last_progress = int(txt)
                        if last_progress >= 600_000:
                            break
                await asyncio.sleep(0.05)
            assert last_progress >= 600_000, "el hijo no llegó a avanzar lo suficiente para probar el caso real"

            # "cierra la app en el peor caso posible": SIGKILL en seco, sin
            # darle ninguna oportunidad de ejecutar código de limpieza.
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    out_path = dest_dir / "a.bin"
    on_disk = out_path.read_bytes() if out_path.exists() else b""
    assert on_disk == content[:len(on_disk)], (
        "corrupción: los bytes en disco no coinciden con el contenido original hasta ese punto"
    )
    # la única pérdida tolerable es, a lo sumo, un trozo (8KB) que aún
    # estuviese en el buffer de escritura de Python sin volcar al SO -nada
    # que ver con perder todo el progreso, que era el bug real de BitTorrent.
    assert len(on_disk) >= last_progress - 8192, (
        f"pérdida mayor de la esperable: reportado {last_progress}, en disco {len(on_disk)}"
    )
