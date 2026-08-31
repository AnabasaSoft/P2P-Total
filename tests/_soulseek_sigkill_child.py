"""Proceso hijo auxiliar de test_soulseek_sigkill_survives_progress.py:
descarga vía `SoulseekBackend._handle_incoming_file_connection` de un
servidor local que envía los bytes muy despacio, y se queda esperando
hasta que el padre lo mate con SIGKILL a mitad de transferencia -sin
ninguna oportunidad de limpieza, el peor caso real posible al cerrar
la app en seco. No es un `test_*.py`: pytest no lo recoge como test,
solo se invoca como script vía subprocess."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backends.soulseek_backend import SoulseekBackend, _PeerConn
from core.models import Download, DownloadState, Network
from core.rate_limiter import RateLimiter

TOKEN = 99


async def main(port: int, dest_dir: str, progress_file: str, size_bytes: int) -> None:
    backend = SoulseekBackend("x", "y", download_dir=dest_dir)
    download = Download(
        id=None, network=Network.SOULSEEK, title="a.bin", source_id="peer\\a.bin",
        dest_path=dest_dir, size_bytes=size_bytes, state=DownloadState.DOWNLOADING,
    )
    entry_id = 1
    backend._active[entry_id] = {
        "download": download, "username": "peer", "remote_path": "a.bin",
        "candidates": [], "dest_path": dest_dir, "conns": [], "file_conn": None,
        "token": TOKEN, "size": size_bytes, "paused": False, "cancelled": False,
        "winner": None, "winner_conn": None, "last_error": None, "limiter": RateLimiter(),
    }
    backend._transfers_by_token[TOKEN] = entry_id

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    peer = _PeerConn(reader, writer, "peer")

    asyncio.ensure_future(backend._handle_incoming_file_connection(peer))

    while True:
        with open(progress_file, "w") as f:
            f.write(str(download.downloaded_bytes))
        await asyncio.sleep(0.05)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])))
