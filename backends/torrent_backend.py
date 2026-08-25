"""
Backend BitTorrent sobre `libtorrent` (librería embebida, sin procesos
externos). Usa una única `lt.session` para toda la vida de la app.

Particularidad de esta red frente a las demás: BitTorrent no tiene
búsqueda de texto libre nativa dentro del propio protocolo (a
diferencia de Soulseek/DC++/Gnutella2/eD2k, que sí la llevan
incorporada). Aquí "buscar" cubre dos casos bien distintos:
  - Query ya identificada (magnet link, infohash de 40 hex o ruta a un
    .torrent local): se resuelve contra la DHT para obtener sus
    metadatos (nombre, tamaño, nº de peers) sin descargar aún los
    datos — igual que siempre.
  - Texto libre (punto 29 del backlog): se consulta el indexador
    público apibay.org (API JSON de The Pirate Bay), que no requiere
    ni cuenta ni cliente aparte, y se construye un magnet link propio
    con trackers públicos por cada resultado (mismo enfoque que usan
    clientes de referencia como qBittorrent con sus plugins de
    búsqueda). En ambos casos la descarga real se dispara después con
    `start_download()`, reutilizando el magnet como `source_id`.
"""

import asyncio
import json
import os
import re
import threading
import time
import urllib.parse
from typing import Callable

import libtorrent as lt

from core.backend_base import NetworkBackend
from core.http_client import http_get
from core.models import Download, DownloadState, Network, SearchResult
from core.proxy import ProxyConfig
from core.stats_tracker import stats_tracker

_INFOHASH_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_MAGNET_BTIH_RE = re.compile(r"btih:([a-fA-F0-9]{40})", re.IGNORECASE)


def _destroy_session(session: "lt.session") -> None:
    del session

_FILE_PREFIX = "file:"  # prefijo interno para distinguir source_id de archivo .torrent vs magnet

# Indexador público usado para la búsqueda por texto libre (punto 29 del
# backlog): misma API JSON de apibay.org (The Pirate Bay) usada por el
# script de referencia que dio el usuario. Solo hace falta un GET, sin
# cuenta ni cliente aparte.
_APIBAY_SEARCH_URL = "https://apibay.org/q.php"

# Trackers públicos habituales para adjuntar a cada magnet construido a
# partir de un resultado de apibay.org (que solo da el infohash, sin
# trackers propios) — misma lista que trae el script de referencia.
_DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://tracker.zer0day.to:1337/announce",
    "udp://tracker.leechers-paradise.org:6969/announce",
    "udp://coppersurfer.tk:6969/announce",
    "udp://tracker.internetwarriors.net:1337/announce",
    "wss://tracker.openwebtorrent.com",
)


def _build_magnet(info_hash: str, name: str) -> str:
    """Magnet URI estándar (`xt=urn:btih:` + nombre + trackers) a partir
    del infohash que devuelve apibay.org, para que apunte a los mismos
    peers que ya están anunciados en la DHT/esos trackers sin depender
    del propio apibay.org para nada más."""
    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}"
    for tracker in _DEFAULT_TRACKERS:
        magnet += f"&tr={urllib.parse.quote(tracker)}"
    return magnet


def _is_direct_reference(query: str) -> bool:
    """True si `query` ya identifica un torrent concreto (magnet, infohash
    o ruta .torrent) en vez de ser un texto libre a buscar por nombre."""
    query = query.strip()
    return query.startswith("magnet:") or bool(_INFOHASH_RE.match(query)) or _is_torrent_file(query)


async def _search_by_name(query: str, timeout: float, max_results: int | None,
                           proxy: ProxyConfig | None) -> list[SearchResult]:
    """Búsqueda por texto libre contra apibay.org: sin esto, BitTorrent
    era la única red de las cinco donde 'buscar' exigía ya conocer el
    magnet/infohash de antemano."""
    url = f"{_APIBAY_SEARCH_URL}?{urllib.parse.urlencode({'q': query})}"
    body = await http_get(url, timeout=timeout, proxy=proxy)
    try:
        items = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise RuntimeError(f"Respuesta de apibay.org no es JSON válido: {exc}") from exc

    # apibay.org devuelve [{"id": "0", "name": "No results returned", ...}]
    # cuando no hay coincidencias, en vez de una lista vacía.
    if not items or (len(items) == 1 and items[0].get("id") == "0"):
        return []

    results = []
    for item in items:
        info_hash = item.get("info_hash", "")
        if not info_hash:
            continue
        name = item.get("name", "Sin título")
        try:
            seeders = int(item.get("seeders", 0))
        except (TypeError, ValueError):
            seeders = 0
        try:
            leechers = int(item.get("leechers", 0))
        except (TypeError, ValueError):
            leechers = 0
        try:
            size_bytes = int(item.get("size", 0))
        except (TypeError, ValueError):
            size_bytes = 0
        results.append(SearchResult(
            network=Network.TORRENT,
            title=name,
            size_bytes=size_bytes,
            source_id=_build_magnet(info_hash, name),
            seeds_or_sources=seeders,
            extra={"leechers": leechers, "added": item.get("added", "0")},
        ))

    # Más seeds primero: es la mejor aproximación disponible a "más
    # probable de descargar rápido" sin tener que resolver metadatos de
    # cada candidato contra la DHT solo para decidir el orden.
    results.sort(key=lambda r: r.seeds_or_sources, reverse=True)
    if max_results:
        results = results[:max_results]
    return results


def _to_magnet(query: str) -> str:
    """Acepta tanto un magnet link completo como un infohash de 40 hex."""
    query = query.strip()
    if query.startswith("magnet:"):
        return query
    if _INFOHASH_RE.match(query):
        return f"magnet:?xt=urn:btih:{query}"
    raise ValueError(
        "Para torrent, 'query' debe ser un magnet link, un infohash de 40 "
        "caracteres hex, o la ruta a un archivo .torrent existente"
    )


def _is_torrent_file(query: str) -> bool:
    query = query.strip()
    return query.lower().endswith(".torrent") and os.path.isfile(query)


def _infohash_from_magnet(magnet: str) -> str | None:
    """Extrae el infohash en hex minúsculas directamente del texto del
    magnet, sin depender de libtorrent. Se usa como clave para no perder
    un handle ya conectado a peers cuando pasamos de 'buscar' a 'descargar'."""
    m = _MAGNET_BTIH_RE.search(magnet)
    return m.group(1).lower() if m else None


class TorrentBackend(NetworkBackend):
    network = Network.TORRENT

    def __init__(self, proxy: ProxyConfig | None = None) -> None:
        self._session: lt.session | None = None
        self._proxy = proxy
        self._progress_callback: Callable[[Download], None] | None = None
        self._active: dict[str, dict] = {}   # info_hash (hex) -> {"handle": ..., "download": Download}
        self._pending: dict[str, "lt.torrent_handle"] = {}  # info_hash -> handle encontrado en search(), aún no convertido en descarga
        self._poll_task: asyncio.Task | None = None
        self._last_session_upload_bytes = 0  # para sumar solo el delta al stats_tracker (punto 24)

    # ---- ciclo de vida ----

    async def connect(self) -> None:
        if self._session is not None:
            return
        settings = {
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            # Punto 34.3 del backlog: cifrado de protocolo (MSE/PE) y µTP.
            # libtorrent ya los trae encendidos por defecto aunque no se
            # toque `settings`, pero se dejan explícitos aquí a propósito
            # -para que sea una decisión visible y documentada, no un
            # efecto colateral de no tocar nada- y con la política
            # concreta que interesa: "enabled" (no "forced") negocia
            # cifrado con los peers que lo soportan pero sigue aceptando
            # conexión en claro con los que no, que son todavía muchos en
            # redes públicas reales; forzarlo tiraría peers válidos por la
            # borda sin necesidad. `pe_both` acepta tanto RC4 como el modo
            # "solo handshake ofuscado, payload en claro" que negocian
            # algunos clientes.
            "out_enc_policy": lt.enc_policy.pe_enabled,
            "in_enc_policy": lt.enc_policy.pe_enabled,
            "allowed_enc_level": lt.enc_level.pe_both,
            "enable_outgoing_utp": True,
            "enable_incoming_utp": True,
            "alert_mask": lt.alert.category_t.status_notification
            | lt.alert.category_t.error_notification
            | lt.alert.category_t.storage_notification,
        }
        if self._proxy is not None and self._proxy.enabled and self._proxy.host:
            # Soporte de proxy nativo de libtorrent (a diferencia del resto
            # de backends, que usan core/proxy.py): también proxea DHT/LSD/
            # UPnP si "proxy_peer_connections"/"proxy_tracker_connections"
            # están activos, que es el valor por defecto.
            has_auth = bool(self._proxy.username)
            if self._proxy.kind == "socks5":
                proxy_type = lt.proxy_type_t.socks5_pw if has_auth else lt.proxy_type_t.socks5
            elif self._proxy.kind == "http":
                proxy_type = lt.proxy_type_t.http_pw if has_auth else lt.proxy_type_t.http
            else:
                raise ValueError(f"Tipo de proxy desconocido: {self._proxy.kind!r}")
            settings.update({
                "proxy_type": proxy_type,
                "proxy_hostname": self._proxy.host,
                "proxy_port": self._proxy.port,
                "proxy_username": self._proxy.username,
                "proxy_password": self._proxy.password,
                "force_proxy": True,
            })
        self._session = lt.session(settings)
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def disconnect(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        session = self._session
        self._session = None
        self._active.clear()
        self._pending.clear()
        if session is not None:
            # El destructor de `lt.session` hace un apagado "educado" de
            # DHT/LSD/UPnP/NAT-PMP con idas y vueltas de red que pueden
            # tardar varios segundos (a veces bastantes más), bloqueando
            # de forma síncrona el hilo que lo llame. Se destruye en un
            # hilo daemon aparte para que cerrar la app nunca se quede
            # esperando a que termine (al morir el proceso, el hilo se
            # descarta sin más, sin dejarlo "zombi").
            threading.Thread(target=_destroy_session, args=(session,), daemon=True).start()

    async def is_connected(self) -> bool:
        return self._session is not None

    # ---- búsqueda (resolución de metadatos vía DHT) ----

    async def search(self, query: str, timeout: float = 15.0, debug: bool = False,
                      max_results: int | None = None) -> list[SearchResult]:
        if self._session is None:
            raise RuntimeError("Backend de torrent no conectado")

        if debug:
            self._print_diagnostics()

        # Texto libre (no magnet/infohash/.torrent): búsqueda por nombre
        # contra apibay.org en vez de resolución DHT de un torrent ya
        # identificado (punto 29 del backlog).
        if not _is_direct_reference(query):
            return await _search_by_name(query, timeout=timeout, max_results=max_results, proxy=self._proxy)

        # Caso .torrent local: los metadatos ya vienen dentro del archivo,
        # no hace falta esperar nada ni tocar la DHT para esta parte.
        if _is_torrent_file(query):
            path = os.path.abspath(query.strip())
            info = lt.torrent_info(path)
            return [SearchResult(
                network=self.network,
                title=info.name(),
                size_bytes=info.total_size(),
                source_id=f"{_FILE_PREFIX}{path}",
                seeds_or_sources=0,   # aún no sabemos peers hasta que se añada a la sesión
                extra={"num_files": info.num_files(), "trackers": len(list(info.trackers()))},
            )]

        magnet = _to_magnet(query)
        info_hash = _infohash_from_magnet(magnet)

        # Si ya teníamos este torrent en 'pending' (de una búsqueda anterior),
        # reutilizamos el mismo handle en vez de tirar la conexión a peers
        # ya establecida y empezar de cero.
        if info_hash and info_hash in self._pending:
            handle = self._pending[info_hash]
        else:
            params = lt.parse_magnet_uri(magnet)
            params.save_path = "/tmp"          # temporal: aún no sabemos si se descargará
            params.flags |= lt.torrent_flags.upload_mode
            handle = self._session.add_torrent(params)
            if info_hash:
                self._pending[info_hash] = handle

        deadline = time.monotonic() + timeout
        last_report = time.monotonic()
        while time.monotonic() < deadline:
            if debug:
                for alert in self._session.pop_alerts():
                    if alert.category() & lt.alert.category_t.error_notification:
                        print(f"  [alerta] {type(alert).__name__}: {alert.message()}")

            if handle.status().has_metadata:
                info = handle.torrent_file()
                status = handle.status()
                return [SearchResult(
                    network=self.network,
                    title=info.name(),
                    size_bytes=info.total_size(),
                    source_id=magnet,
                    seeds_or_sources=status.num_seeds,
                    extra={"num_files": info.num_files()},
                )]

            if debug and time.monotonic() - last_report > 5:
                s = self._session.status()
                hstatus = handle.status()
                print(
                    f"  [debug] dht_nodes={s.dht_nodes} "
                    f"num_peers(torrent)={hstatus.num_peers} "
                    f"state={hstatus.state} "
                    f"num_seeds={hstatus.num_seeds}"
                )
                last_report = time.monotonic()

            await asyncio.sleep(0.25)

        # Se acaba el timeout sin metadatos: ahora sí, no tiene sentido
        # mantener el handle vivo indefinidamente si nadie lo reclama
        if info_hash:
            self._pending.pop(info_hash, None)
        self._session.remove_torrent(handle)
        raise TimeoutError(
            f"No se obtuvieron metadatos del torrent en {timeout}s (¿pocos peers en la DHT?)"
        )

    def _print_diagnostics(self) -> None:
        """Vuelca el estado de la sesión: puerto real de escucha, si DHT
        está corriendo, nº de nodos conocidos, etc. Para saber si el
        problema es de red (firewall/NAT) antes de esperar al timeout."""
        s = self._session
        print("  [debug] --- estado de la sesión libtorrent ---")
        print(f"  [debug] puerto de escucha: {s.listen_port()}")
        print(f"  [debug] dht running: {s.is_dht_running()}")
        print("  [debug] -----------------------------------------")

    # ---- descarga ----

    async def start_download(self, result: SearchResult, dest_path: str) -> Download:
        if self._session is None:
            raise RuntimeError("Backend de torrent no conectado")

        if result.source_id.startswith(_FILE_PREFIX):
            # Viene de un .torrent local: metadatos completos ya en mano,
            # se añade directo con save_path real, sin pasar por magnet.
            path = result.source_id[len(_FILE_PREFIX):]
            info = lt.torrent_info(path)
            handle = self._session.add_torrent({"ti": info, "save_path": dest_path})
        else:
            magnet = _to_magnet(result.source_id)
            info_hash_str = _infohash_from_magnet(magnet)

            pending_handle = self._pending.pop(info_hash_str, None) if info_hash_str else None
            if pending_handle is not None:
                # Reutilizamos el handle que ya encontró peers durante search():
                # solo hace falta moverlo a la carpeta destino real y quitarle
                # el modo "solo metadatos" para que empiece a bajar datos de verdad.
                pending_handle.move_storage(dest_path)
                pending_handle.set_upload_mode(False)
                pending_handle.resume()
                handle = pending_handle
            else:
                # No venía de una búsqueda previa (o expiró): arrancamos de cero.
                params = lt.parse_magnet_uri(magnet)
                params.save_path = dest_path
                handle = self._session.add_torrent(params)

        info_hash = str(handle.info_hash())

        download = Download(
            id=None,
            network=self.network,
            title=result.title,
            source_id=result.source_id,
            dest_path=dest_path,
            size_bytes=result.size_bytes,
            state=DownloadState.SEARCHING_SOURCES,  # aún resolviendo metadatos/peers
        )
        self._active[info_hash] = {"handle": handle, "download": download}
        return download

    async def reattach_download(self, download: Download) -> None:
        """Reengancha un torrent tras reiniciar la app: la sesión de
        libtorrent se crea desde cero en cada `connect()`, así que hay
        que volver a añadir el torrent (magnet o .torrent local) con el
        `save_path` real. Al no pasar datos de fast-resume, libtorrent
        hace un recheck automático de las piezas ya en disco (rápido,
        solo hashes) antes de seguir bajando el resto — no hace falta
        reimplementar esa lógica a mano."""
        if self._session is None or self._find_entry(download) is not None:
            return

        if download.source_id.startswith(_FILE_PREFIX):
            path = download.source_id[len(_FILE_PREFIX):]
            info = lt.torrent_info(path)
            handle = self._session.add_torrent({"ti": info, "save_path": download.dest_path})
        else:
            magnet = _to_magnet(download.source_id)
            params = lt.parse_magnet_uri(magnet)
            params.save_path = download.dest_path
            handle = self._session.add_torrent(params)

        if download.state == DownloadState.PAUSED:
            handle.pause()
        else:
            handle.resume()
            download.state = DownloadState.SEARCHING_SOURCES

        info_hash = str(handle.info_hash())
        self._active[info_hash] = {"handle": handle, "download": download}

    async def pause_download(self, download: Download) -> None:
        entry = self._find_entry(download)
        if entry:
            entry["handle"].pause()
            download.state = DownloadState.PAUSED

    async def resume_download(self, download: Download) -> None:
        entry = self._find_entry(download)
        if entry:
            entry["handle"].resume()
            download.state = DownloadState.DOWNLOADING

    async def cancel_download(self, download: Download) -> None:
        """No borra los datos ya descargados del disco (a diferencia de
        antes, que pasaba `lt.session.delete_files`): igual que en las
        otras cuatro redes, cancelar solo detiene la transferencia y
        deja el contenido parcial tal cual, para poder "Iniciar" después
        y retomarlo desde donde se quedó en vez de perderlo. Borrar de
        verdad el contenido en disco es cosa de la acción explícita
        "Borrar descarga (y archivos)" (`DownloadManager.delete`)."""
        entry = self._find_entry(download)
        if entry and self._session:
            self._session.remove_torrent(entry["handle"])
            del self._active[str(entry["handle"].info_hash())]
            download.state = DownloadState.CANCELLED

    def set_global_limits(self, download_bps: int, upload_bps: int) -> None:
        """A diferencia de las otras cuatro redes, BitTorrent no usa los
        limitadores compartidos de `core.rate_limiter`: libtorrent ya trae
        su propio limitador nativo a nivel de sesión (0 = sin límite,
        igual convención que en el resto de la app)."""
        if self._session is not None:
            self._session.apply_settings({
                "download_rate_limit": download_bps,
                "upload_rate_limit": upload_bps,
            })

    def set_download_limit(self, download: Download, rate_bps: int) -> None:
        """Límite por torrent: libtorrent expone uno para cada sentido
        por handle, así que aquí "por descarga" cubre tanto la bajada
        como la subida de ese torrent (coherente con que, en BitTorrent,
        la propia descarga sigue "subiendo" piezas ya recibidas a otros
        peers mientras está en marcha)."""
        entry = self._find_entry(download)
        if entry:
            entry["handle"].set_download_limit(rate_bps)
            entry["handle"].set_upload_limit(rate_bps)

    def supports_verify(self) -> bool:
        return True

    async def verify_download(self, download: Download, timeout: float = 300.0) -> bool:
        """`force_recheck()` hace que libtorrent relea del disco todo lo
        ya descargado y lo recompruebe pieza a pieza contra el SHA1 de
        referencia del propio `.torrent` -- exactamente el mismo
        recheck/hash-check nativo que usan clientes como qBittorrent al
        pedir "Force re-check". Cualquier pieza que no cuadre (dato
        corrupto, o que faltase por completo) se marca como no
        descargada; si la descarga no está pausada, libtorrent la vuelve
        a pedir solo, sin ninguna lógica extra por nuestra parte."""
        entry = self._find_entry(download)
        if entry is None:
            raise RuntimeError("Descarga no encontrada")
        handle = entry["handle"]
        # Antes de arrancar el recheck en sí, libtorrent puede pasar
        # brevemente por queued_for_checking/checking_resume_data (aún sin
        # tocar datos todavía): hay que esperar a que salga de cualquiera
        # de los tres estados de "comprobando", no solo de checking_files,
        # o se puede leer un total_wanted_done todavía a 0 de mentira.
        checking_states = {
            lt.torrent_status.states.checking_files,
            lt.torrent_status.states.checking_resume_data,
            lt.torrent_status.states.queued_for_checking,
        }
        handle.force_recheck()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if handle.status().state not in checking_states:
                break
            await asyncio.sleep(0.25)
        else:
            raise TimeoutError(f"La verificación no terminó en {timeout}s")
        status = handle.status()
        return status.total_wanted > 0 and status.total_wanted_done >= status.total_wanted

    def list_files(self, download: Download) -> list[dict] | None:
        """Lista de archivos del torrent con su índice, ruta, tamaño,
        prioridad actual (0-7) y bytes ya descargados de cada uno. `None`
        si el handle aún no tiene metadatos (torrent recién añadido por
        magnet, esperando a la DHT) o si no hay descarga activa con ese
        info_hash."""
        entry = self._find_entry(download)
        if entry is None or not entry["handle"].status().has_metadata:
            return None
        handle = entry["handle"]
        info = handle.torrent_file()
        files = info.files()
        priorities = handle.file_priorities()
        progress = handle.file_progress()
        return [
            {
                "index": i,
                "path": files.file_path(i),
                "size_bytes": files.file_size(i),
                "priority": priorities[i],
                "downloaded_bytes": progress[i],
            }
            for i in range(files.num_files())
        ]

    def list_trackers(self, download: Download) -> list[dict] | None:
        """Estado de cada tracker del torrent (punto 35 del backlog, para
        la pestaña Red): URL, si está funcionando (respondió bien al
        último anuncio), su último mensaje de error/estado y los
        seeds/peers que reportó en el último scrape. `None` si no hay
        descarga activa con ese info_hash."""
        entry = self._find_entry(download)
        if entry is None:
            return None
        # handle.trackers() devuelve dicts (no objetos announce_entry
        # navegables con atributos), pese a que la clase lt.announce_entry
        # sí expone esos campos como propiedades/métodos -es la propia
        # librería la que serializa así, no una elección nuestra.
        return [
            {
                "url": t["url"],
                "working": t["fails"] == 0,
                "message": t["message"],
                "seeds": t["scrape_complete"],
                "peers": t["scrape_incomplete"],
            }
            for t in entry["handle"].trackers()
        ]

    def set_file_priorities(self, download: Download, priorities: dict[int, int]) -> None:
        entry = self._find_entry(download)
        if entry is None:
            return
        handle = entry["handle"]
        current = handle.file_priorities()
        for index, priority in priorities.items():
            current[index] = priority
        handle.prioritize_files(current)

    def set_sequential_download(self, download: Download, enabled: bool) -> None:
        entry = self._find_entry(download)
        if entry:
            entry["handle"].set_sequential_download(enabled)

    def is_sequential_download(self, download: Download) -> bool:
        entry = self._find_entry(download)
        return bool(entry["handle"].status().sequential_download) if entry else False

    def subscribe_progress(self, callback: Callable[[Download], None]) -> None:
        self._progress_callback = callback

    def get_stats(self) -> dict:
        if self._session is None:
            return {}
        s = self._session.status()
        # Prueba en vivo de que el cifrado MSE/PE del punto 34.3 está
        # negociándose de verdad (no solo activado en la sesión): cuenta,
        # de los peers conectados ahora mismo en las descargas activas,
        # cuántos llevan el flag de cifrado puesto tras el handshake.
        connected_peers = 0
        encrypted_peers = 0
        for entry in self._active.values():
            for peer in entry["handle"].get_peer_info():
                connected_peers += 1
                if peer.flags & (lt.peer_info.rc4_encrypted | lt.peer_info.plaintext_encrypted):
                    encrypted_peers += 1
        return {
            "listen_port": self._session.listen_port(),
            "dht_nodes": s.dht_nodes,
            # Tamaño estimado de toda la red DHT (no solo los nodos con
            # los que tenemos contacto directo, `dht_nodes`) -el
            # equivalente en BitTorrent a la "media de usuarios en la
            # red" que aMule reporta para Kad, aunque aquí lo calcula
            # la propia libtorrent a partir del espacio de IDs visto.
            "dht_global_nodes": s.dht_global_nodes,
            "active_transfers": len(self._active),
            "connected_peers": connected_peers,
            "encrypted_peers": encrypted_peers,
            "utp_connections": s.utp_stats["num_connected"],
            "total_downloaded": s.total_payload_download,
            "total_uploaded": s.total_payload_upload,
        }

    # ---- internos ----

    def _find_entry(self, download: Download) -> dict | None:
        for entry in self._active.values():
            if entry["download"] is download or entry["download"].id == download.id:
                return entry
        return None

    async def _poll_loop(self) -> None:
        """Traduce el estado interno de libtorrent a nuestro modelo Download
        cada segundo y notifica al DownloadManager."""
        LT_STATE_MAP = {
            lt.torrent_status.states.downloading: DownloadState.DOWNLOADING,
            lt.torrent_status.states.finished: DownloadState.COMPLETED,
            lt.torrent_status.states.seeding: DownloadState.COMPLETED,
            lt.torrent_status.states.checking_files: DownloadState.SEARCHING_SOURCES,
            lt.torrent_status.states.downloading_metadata: DownloadState.SEARCHING_SOURCES,
        }
        try:
            while True:
                for entry in list(self._active.values()):
                    status = entry["handle"].status()
                    d = entry["download"]
                    d.downloaded_bytes = status.total_wanted_done
                    d.size_bytes = status.total_wanted or d.size_bytes
                    d.speed_bps = status.download_rate
                    d.connected_peers = status.num_peers
                    d.state = LT_STATE_MAP.get(status.state, d.state)
                    if not status.paused and self._progress_callback:
                        self._progress_callback(d)

                # Bytes subidos totales de la sesión (punto 24 del backlog):
                # libtorrent ya lleva la cuenta él solo -incluido el tráfico
                # de "seeding" en segundo plano, que no pasa por ningún
                # bucle nuestro-, así que solo hace falta sumar el delta
                # desde la última vuelta.
                session_upload_bytes = self._session.status().total_payload_upload
                upload_delta = session_upload_bytes - self._last_session_upload_bytes
                if upload_delta > 0:
                    self._last_session_upload_bytes = session_upload_bytes
                    stats_tracker.record_uploaded(Network.TORRENT, upload_delta)

                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
