"""
Control remoto / API web (punto 34.6 del backlog): expone, sobre un
servidor HTTP mínimo escrito a mano con `asyncio.start_server` (sin
Flask/aiohttp/FastAPI ni ningún framework de terceros -- misma
restricción de diseño que el resto del proyecto, ver `core/http_client.py`
para el mismo enfoque del lado cliente), una API JSON y una página web
de una sola pantalla para consultar y gestionar las descargas (listar,
pausar, reanudar, cancelar, borrar, buscar y arrancar una descarga
nueva) sin tener que abrir la ventana de escritorio -- pensado para
usarse con la aplicación minimizada a la bandeja del sistema (punto 22)
o corriendo en una máquina sin entorno gráfico.

Desactivado por defecto y sin token configurado (ver
`core.config.RemoteControlConfig`): al activarlo hace falta fijar un
token propio desde Preferencias, que hay que mandar en cada petición a
la API (cabecera "Authorization: Bearer <token>" o "?token="), porque
de lo contrario cualquiera con acceso a la red/puerto podría gestionar
las descargas. Por el mismo motivo la dirección de escucha por defecto
es solo "127.0.0.1" (este mismo equipo); exponerlo a la LAN (host
"0.0.0.0") es una decisión explícita del usuario.
"""

import asyncio
import json
import re
from urllib.parse import parse_qs, urlparse

from core.config import load_config
from core.models import Network

_MAX_HEADER_BYTES = 16 * 1024
_MAX_BODY_BYTES = 1024 * 1024  # de sobra para un cuerpo JSON de esta API
_ACTION_RE = re.compile(r"^/api/downloads/(\d+)/(pause|resume|cancel)$")
_DOWNLOAD_ID_RE = re.compile(r"^/api/downloads/(\d+)$")


def _download_to_dict(download) -> dict:
    return {
        "id": download.id,
        "network": download.network.value,
        "title": download.title,
        "state": download.state.value,
        "size_bytes": download.size_bytes,
        "downloaded_bytes": download.downloaded_bytes,
        "progress": download.progress,
        "speed_bps": download.speed_bps,
        "connected_peers": download.connected_peers,
        "category": download.category,
        "error_message": download.error_message,
    }


def _search_result_to_dict(result) -> dict:
    return {
        "network": result.network.value,
        "title": result.title,
        "size_bytes": result.size_bytes,
        "source_id": result.source_id,
        "seeds_or_sources": result.seeds_or_sources,
        "extra": {k: v for k, v in result.extra.items() if isinstance(v, (str, int, float, bool, type(None)))},
    }


class RemoteControlServer:
    """La GUI solo habla con esta clase (mismo patrón que
    `WatchFolderManager`/`BandwidthScheduler`): `start()`/`stop()`
    arrancan y paran el servidor HTTP según `RemoteControlConfig`, y
    `reload()` lo reinicia con la configuración nueva tras guardar
    Preferencias (host/puerto/token pueden cambiar en caliente sin
    reiniciar la aplicación entera)."""

    def __init__(self, download_manager, connection_manager) -> None:
        self._download_manager = download_manager
        self._connection_manager = connection_manager
        self._server: asyncio.Server | None = None

    def start(self) -> None:
        asyncio.ensure_future(self._start_async())

    async def _start_async(self) -> None:
        config = load_config().remote_control
        if not config.enabled or not config.token:
            return
        try:
            self._server = await asyncio.start_server(self._handle_client, config.host, config.port)
        except OSError:
            self._server = None  # puerto ocupado o host inválido: se queda desactivado, sin tumbar la app

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    def reload(self) -> None:
        self.stop()
        self.start()

    # ---- Servidor HTTP mínimo ----

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle_request(reader, writer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ValueError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    async def _handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15.0)
        if len(head) > _MAX_HEADER_BYTES:
            await self._respond_json(writer, 431, {"error": "cabeceras demasiado largas"})
            return
        lines = head.decode("iso-8859-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError:
            await self._respond_json(writer, 400, {"error": "petición mal formada"})
            return

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()

        content_length = int(headers.get("content-length") or 0)
        if content_length > _MAX_BODY_BYTES:
            await self._respond_json(writer, 413, {"error": "cuerpo demasiado grande"})
            return
        body = await asyncio.wait_for(reader.readexactly(content_length), timeout=15.0) if content_length else b""

        parsed = urlparse(target)
        path = parsed.path
        query = parse_qs(parsed.query)

        if method == "GET" and path == "/":
            await self._respond_html(writer, 200, _INDEX_HTML)
            return

        config = load_config().remote_control
        auth_header = headers.get("authorization", "")
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else query.get("token", [""])[0]
        if not config.enabled or not config.token or token != config.token:
            await self._respond_json(writer, 401, {"error": "no autorizado"})
            return

        try:
            await self._route(method, path, query, body, writer)
        except Exception as exc:
            await self._respond_json(writer, 500, {"error": str(exc)})

    async def _route(self, method: str, path: str, query: dict, body: bytes, writer: asyncio.StreamWriter) -> None:
        if method == "GET" and path == "/api/downloads":
            downloads = self._download_manager.load_history()
            await self._respond_json(writer, 200, [_download_to_dict(d) for d in downloads])
            return

        if method == "GET" and path == "/api/networks":
            statuses = {n.value: self._connection_manager.status(n) for n in Network}
            await self._respond_json(writer, 200, statuses)
            return

        if method == "POST" and path == "/api/search":
            await self._handle_search(body, writer)
            return

        if method == "POST" and path == "/api/downloads":
            await self._handle_start_download(body, writer)
            return

        action_match = _ACTION_RE.match(path)
        if method == "POST" and action_match:
            await self._handle_action(int(action_match.group(1)), action_match.group(2), writer)
            return

        id_match = _DOWNLOAD_ID_RE.match(path)
        if method == "DELETE" and id_match:
            await self._handle_delete(int(id_match.group(1)), writer)
            return

        await self._respond_json(writer, 404, {"error": "no encontrado"})

    def _find_download(self, download_id: int):
        for download in self._download_manager.load_history():
            if download.id == download_id:
                return download
        return None

    async def _handle_search(self, body: bytes, writer: asyncio.StreamWriter) -> None:
        payload = json.loads(body or b"{}")
        query = (payload.get("query") or "").strip()
        if not query:
            await self._respond_json(writer, 400, {"error": "falta 'query'"})
            return
        networks = None
        if payload.get("networks"):
            try:
                networks = [Network(n) for n in payload["networks"]]
            except ValueError as exc:
                await self._respond_json(writer, 400, {"error": f"red desconocida: {exc}"})
                return
        timeout = min(float(payload.get("timeout", 20.0)), 60.0)
        try:
            results = await asyncio.wait_for(
                self._download_manager.search_all(query, networks=networks), timeout=timeout + 5.0
            )
        except asyncio.TimeoutError:
            results = []
        await self._respond_json(writer, 200, [_search_result_to_dict(r) for r in results])

    async def _handle_start_download(self, body: bytes, writer: asyncio.StreamWriter) -> None:
        from core.models import SearchResult

        payload = json.loads(body or b"{}")
        try:
            network = Network(payload["network"])
            source_id = payload["source_id"]
            title = payload.get("title") or source_id
        except (KeyError, ValueError) as exc:
            await self._respond_json(writer, 400, {"error": f"faltan campos o son inválidos ({exc})"})
            return
        config = load_config()
        dest_path = payload.get("dest_path") or config.default_download_dir
        category = payload.get("category")
        result = SearchResult(
            network=network, title=title, size_bytes=int(payload.get("size_bytes", 0)), source_id=source_id,
        )
        try:
            download = await self._download_manager.download(result, dest_path, category)
        except Exception as exc:
            await self._respond_json(writer, 400, {"error": str(exc)})
            return
        await self._respond_json(writer, 201, _download_to_dict(download))

    async def _handle_action(self, download_id: int, action: str, writer: asyncio.StreamWriter) -> None:
        download = self._find_download(download_id)
        if download is None:
            await self._respond_json(writer, 404, {"error": "descarga no encontrada"})
            return
        method = {"pause": self._download_manager.pause, "resume": self._download_manager.resume,
                  "cancel": self._download_manager.cancel}[action]
        await method(download)
        await self._respond_json(writer, 200, {"ok": True})

    async def _handle_delete(self, download_id: int, writer: asyncio.StreamWriter) -> None:
        download = self._find_download(download_id)
        if download is None:
            await self._respond_json(writer, 404, {"error": "descarga no encontrada"})
            return
        await self._download_manager.delete(download)
        await self._respond_json(writer, 200, {"ok": True})

    # ---- Respuestas ----

    @staticmethod
    async def _respond_json(writer: asyncio.StreamWriter, status: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        await RemoteControlServer._write_response(writer, status, "application/json; charset=utf-8", body)

    @staticmethod
    async def _respond_html(writer: asyncio.StreamWriter, status: int, html: str) -> None:
        await RemoteControlServer._write_response(writer, status, "text/html; charset=utf-8", html.encode("utf-8"))

    @staticmethod
    async def _write_response(writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes) -> None:
        reason = {200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                  413: "Payload Too Large", 431: "Request Header Fields Too Large",
                  500: "Internal Server Error"}.get(status, "OK")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(headers.encode("iso-8859-1") + body)
        await writer.drain()


_INDEX_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>P2P Total — Control remoto</title>
<style>
  body { font-family: sans-serif; background: #1e1e1e; color: #ddd; margin: 0; padding: 1rem; }
  h1 { font-size: 1.2rem; }
  input, select, button { font-size: 1rem; padding: .4rem; margin: .2rem 0; }
  input[type=text] { width: 100%; box-sizing: border-box; }
  table { width: 100%; border-collapse: collapse; margin-top: .5rem; }
  th, td { text-align: left; padding: .3rem .4rem; border-bottom: 1px solid #444; font-size: .9rem; }
  .bar { background: #333; border-radius: 3px; height: 10px; overflow: hidden; }
  .bar > div { background: #4c9; height: 100%; }
  button { cursor: pointer; background: #333; color: #ddd; border: 1px solid #555; border-radius: 3px; }
  button:hover { background: #444; }
  #token-bar { display: flex; gap: .4rem; margin-bottom: 1rem; }
  #token-bar input { flex: 1; }
  .err { color: #f66; }
  .muted { color: #999; font-size: .85rem; }
</style>
</head>
<body>
<h1>P2P Total — Control remoto</h1>
<div id="token-bar">
  <input id="token" type="text" placeholder="Token de acceso">
  <button onclick="saveToken()">Guardar token</button>
</div>
<p class="muted">Redes: <span id="networks">-</span></p>

<h2>Buscar y descargar</h2>
<input id="query" type="text" placeholder="Buscar...">
<button onclick="doSearch()">Buscar</button>
<div id="results"></div>

<h2>Descargas <button onclick="refresh()">Actualizar</button></h2>
<table>
  <thead><tr><th>Título</th><th>Red</th><th>Estado</th><th>Progreso</th><th>Velocidad</th><th>Acciones</th></tr></thead>
  <tbody id="downloads"></tbody>
</table>
<p id="error" class="err"></p>

<script>
function getToken() { return localStorage.getItem('p2p_total_token') || ''; }
function saveToken() {
  localStorage.setItem('p2p_total_token', document.getElementById('token').value.trim());
  refresh();
}
document.getElementById('token').value = getToken();

async function api(path, options) {
  options = options || {};
  options.headers = Object.assign({}, options.headers, {
    'Authorization': 'Bearer ' + getToken(),
    'Content-Type': 'application/json',
  });
  const resp = await fetch(path, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
  return data;
}

function fmtBytes(n) {
  if (!n) return '0 MB';
  return (n / 1048576).toFixed(1) + ' MB';
}
function fmtSpeed(n) { return (n / 1024).toFixed(1) + ' KB/s'; }

async function refresh() {
  document.getElementById('error').textContent = '';
  try {
    const downloads = await api('/api/downloads');
    const tbody = document.getElementById('downloads');
    tbody.innerHTML = '';
    for (const d of downloads) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${d.title}</td>
        <td>${d.network}</td>
        <td>${d.state}</td>
        <td><div class="bar"><div style="width:${(d.progress*100).toFixed(0)}%"></div></div>
            ${fmtBytes(d.downloaded_bytes)} / ${fmtBytes(d.size_bytes)}</td>
        <td>${fmtSpeed(d.speed_bps)}</td>
        <td>
          <button onclick="action(${d.id},'pause')">Pausar</button>
          <button onclick="action(${d.id},'resume')">Reanudar</button>
          <button onclick="action(${d.id},'cancel')">Cancelar</button>
          <button onclick="removeDownload(${d.id})">Borrar</button>
        </td>`;
      tbody.appendChild(tr);
    }
    const networks = await api('/api/networks');
    document.getElementById('networks').textContent =
      Object.entries(networks).map(([n, s]) => `${n}: ${s}`).join(', ');
  } catch (e) {
    document.getElementById('error').textContent = e.message;
  }
}

async function action(id, act) {
  try { await api(`/api/downloads/${id}/${act}`, { method: 'POST' }); refresh(); }
  catch (e) { document.getElementById('error').textContent = e.message; }
}
async function removeDownload(id) {
  try { await api(`/api/downloads/${id}`, { method: 'DELETE' }); refresh(); }
  catch (e) { document.getElementById('error').textContent = e.message; }
}

async function doSearch() {
  const query = document.getElementById('query').value.trim();
  if (!query) return;
  const div = document.getElementById('results');
  div.textContent = 'Buscando...';
  try {
    const results = await api('/api/search', { method: 'POST', body: JSON.stringify({ query }) });
    div.innerHTML = '';
    for (const r of results.slice(0, 100)) {
      const p = document.createElement('p');
      p.innerHTML = `${r.title} (${r.network}, ${fmtBytes(r.size_bytes)})
        <button onclick='startDownload(${JSON.stringify(r).replace(/'/g, "&apos;")})'>Descargar</button>`;
      div.appendChild(p);
    }
    if (!results.length) div.textContent = 'Sin resultados.';
  } catch (e) {
    div.textContent = '';
    document.getElementById('error').textContent = e.message;
  }
}

async function startDownload(result) {
  try {
    await api('/api/downloads', {
      method: 'POST',
      body: JSON.stringify({
        network: result.network, source_id: result.source_id,
        title: result.title, size_bytes: result.size_bytes,
      }),
    });
    refresh();
  } catch (e) {
    document.getElementById('error').textContent = e.message;
  }
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
