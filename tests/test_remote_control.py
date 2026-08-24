"""core/remote_control.py: servidor HTTP mínimo del control remoto /
API web (punto 34.6 del backlog) -- extremo a extremo contra un
`RemoteControlServer` real levantado en 127.0.0.1 con un puerto libre
asignado por el sistema operativo (mismo enfoque que test_http_client.py,
solo que aquí el servidor es el propio código de producción y el
cliente es un socket a mano), con un `DownloadManager`/
`ConnectionManager` de mentira para no tocar la base de datos real ni
ninguna red -- así se comprueba el enrutado HTTP, la autenticación por
token y las respuestas JSON tal cual las vería un navegador real."""

import json

import pytest

from core.config import Config
from core.models import Download, DownloadState, Network, SearchResult
from core.remote_control import RemoteControlServer

TOKEN = "test-token-123"


class _FakeDownloadManager:
    def __init__(self, downloads=None):
        self.downloads = list(downloads or [])
        self.calls = []

    def load_history(self):
        return list(self.downloads)

    async def pause(self, download):
        self.calls.append(("pause", download.id))

    async def resume(self, download):
        self.calls.append(("resume", download.id))

    async def cancel(self, download):
        self.calls.append(("cancel", download.id))

    async def delete(self, download):
        self.calls.append(("delete", download.id))
        self.downloads = [d for d in self.downloads if d.id != download.id]

    async def download(self, result, dest_path, category=None):
        download = Download(
            id=99, network=result.network, title=result.title, source_id=result.source_id,
            dest_path=dest_path, size_bytes=result.size_bytes, category=category,
        )
        self.downloads.append(download)
        return download

    async def search_all(self, query, networks=None):
        return [SearchResult(network=Network.TORRENT, title=f"resultado de {query}", size_bytes=123, source_id="abc")]


class _FakeConnectionManager:
    def status(self, network):
        return "connected" if network == Network.TORRENT else "disconnected"


async def _request(port: int, method: str, path: str, headers: dict | None = None, body: bytes = b"") -> tuple[int, bytes]:
    import asyncio

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body
    writer.write(request)
    await writer.drain()
    raw = await reader.read(-1)
    writer.close()
    head, _, resp_body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split(b" ")[1])
    return status, resp_body


@pytest.fixture
async def running_server(monkeypatch):
    download = Download(
        id=1, network=Network.TORRENT, title="pelicula.mp4", source_id="magnet:?xt=urn:btih:abc",
        dest_path="/tmp/descargas", size_bytes=1000, downloaded_bytes=500, state=DownloadState.DOWNLOADING,
    )
    manager = _FakeDownloadManager([download])
    connection_manager = _FakeConnectionManager()

    config = Config()
    config.remote_control.enabled = True
    config.remote_control.token = TOKEN
    config.remote_control.host = "127.0.0.1"
    config.remote_control.port = 0
    monkeypatch.setattr("core.remote_control.load_config", lambda: config)

    server = RemoteControlServer(manager, connection_manager)
    await server._start_async()
    port = server._server.sockets[0].getsockname()[1]
    try:
        yield server, manager, port
    finally:
        server.stop()


async def test_root_page_served_without_authentication(running_server):
    _, _, port = running_server
    status, body = await _request(port, "GET", "/")
    assert status == 200
    assert b"<html" in body.lower()


async def test_api_rejects_request_without_token(running_server):
    _, _, port = running_server
    status, _ = await _request(port, "GET", "/api/downloads")
    assert status == 401


async def test_api_rejects_wrong_token(running_server):
    _, _, port = running_server
    status, _ = await _request(port, "GET", "/api/downloads", headers={"Authorization": "Bearer incorrecto"})
    assert status == 401


async def test_list_downloads_with_bearer_token(running_server):
    _, _, port = running_server
    status, body = await _request(port, "GET", "/api/downloads", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200
    data = json.loads(body)
    assert len(data) == 1
    assert data[0]["title"] == "pelicula.mp4"
    assert data[0]["state"] == "downloading"
    assert data[0]["progress"] == 0.5


async def test_query_token_also_works(running_server):
    _, _, port = running_server
    status, body = await _request(port, "GET", f"/api/downloads?token={TOKEN}")
    assert status == 200
    assert len(json.loads(body)) == 1


async def test_networks_status(running_server):
    _, _, port = running_server
    status, body = await _request(port, "GET", "/api/networks", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200
    data = json.loads(body)
    assert data["torrent"] == "connected"
    assert data["soulseek"] == "disconnected"


async def test_pause_resume_cancel_actions(running_server):
    server, manager, port = running_server
    headers = {"Authorization": f"Bearer {TOKEN}"}
    for action in ("pause", "resume", "cancel"):
        status, body = await _request(port, "POST", f"/api/downloads/1/{action}", headers=headers)
        assert status == 200
        assert json.loads(body) == {"ok": True}
    assert manager.calls == [("pause", 1), ("resume", 1), ("cancel", 1)]


async def test_action_on_unknown_download_returns_404(running_server):
    _, _, port = running_server
    status, _ = await _request(port, "POST", "/api/downloads/999/pause", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 404


async def test_delete_download(running_server):
    server, manager, port = running_server
    headers = {"Authorization": f"Bearer {TOKEN}"}
    status, body = await _request(port, "DELETE", "/api/downloads/1", headers=headers)
    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert manager.downloads == []


async def test_search_returns_results(running_server):
    _, _, port = running_server
    headers = {"Authorization": f"Bearer {TOKEN}"}
    body_bytes = json.dumps({"query": "algo"}).encode("utf-8")
    status, body = await _request(port, "POST", "/api/search", headers=headers, body=body_bytes)
    assert status == 200
    data = json.loads(body)
    assert data[0]["title"] == "resultado de algo"
    assert data[0]["network"] == "torrent"


async def test_search_without_query_is_a_bad_request(running_server):
    _, _, port = running_server
    headers = {"Authorization": f"Bearer {TOKEN}"}
    status, body = await _request(port, "POST", "/api/search", headers=headers, body=b"{}")
    assert status == 400


async def test_start_download_creates_entry(running_server):
    server, manager, port = running_server
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = json.dumps({
        "network": "torrent", "source_id": "magnet:?xt=urn:btih:nuevo", "title": "otra.iso", "size_bytes": 2048,
    }).encode("utf-8")
    status, body = await _request(port, "POST", "/api/downloads", headers=headers, body=payload)
    assert status == 201
    data = json.loads(body)
    assert data["title"] == "otra.iso"
    assert any(d.id == 99 for d in manager.downloads)


async def test_start_download_missing_fields_is_bad_request(running_server):
    _, _, port = running_server
    headers = {"Authorization": f"Bearer {TOKEN}"}
    status, _ = await _request(port, "POST", "/api/downloads", headers=headers, body=b"{}")
    assert status == 400


async def test_unknown_path_returns_404(running_server):
    _, _, port = running_server
    status, _ = await _request(port, "GET", "/api/no-existe", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 404


async def test_server_does_not_start_without_token(monkeypatch):
    config = Config()
    config.remote_control.enabled = True
    config.remote_control.token = ""  # sin token: no debe levantar el servidor
    config.remote_control.port = 0
    monkeypatch.setattr("core.remote_control.load_config", lambda: config)

    server = RemoteControlServer(_FakeDownloadManager(), _FakeConnectionManager())
    await server._start_async()
    assert server._server is None


async def test_server_does_not_start_when_disabled(monkeypatch):
    config = Config()
    config.remote_control.enabled = False
    config.remote_control.token = TOKEN
    config.remote_control.port = 0
    monkeypatch.setattr("core.remote_control.load_config", lambda: config)

    server = RemoteControlServer(_FakeDownloadManager(), _FakeConnectionManager())
    await server._start_async()
    assert server._server is None
