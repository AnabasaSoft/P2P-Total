"""
Punto de entrada de P2P Total: sin argumentos arranca la GUI PyQt6;
con un subcomando, actúa como CLI para probar cada backend real por
separado (útil para depurar un backend sin pasar por la interfaz).

Uso:
    python main.py                                             (sin argumentos: arranca la GUI, igual que "gui")
    python main.py --help | -h                                 (esta ayuda)
    python main.py config                                    (configurar credenciales)
    python main.py gui                                        (interfaz gráfica PyQt6)
    python main.py search <query> --network torrent|soulseek|dcpp|gnutella2|emule
    python main.py download <query> <carpeta-destino> --network torrent|soulseek|dcpp|gnutella2|emule

Para torrent, <query> puede ser un magnet link, un infohash de 40 hex,
la ruta a un .torrent local, o texto libre (nombre de película, serie,
etc.): en ese último caso se busca por nombre contra el indexador
público apibay.org y cada resultado trae ya su propio magnet link
(punto 29 del backlog).

Para Soulseek, <query> es texto libre (nombre de artista, canción, etc.)
y hace falta cuenta — configúrala primero con: python main.py config

Para DC++, <query> es texto libre y hace falta un nick y un hub (host:puerto)
— configúralos primero con: python main.py config. Se puede indicar un
hub distinto al de por defecto con --hub host:puerto.

Para Gnutella2 (G2), <query> es texto libre. No hace falta indicar
ningún hub: si no se pasa --hub host:puerto, se descubre uno
automáticamente vía GWebCache.

Para eMule (eD2k + Kad, las dos redes que hablan entre sí en el eMule/
aMule real), <query> es texto libre. No hace falta indicar ningún
servidor: si no se pasa --hub host:puerto, se descubre uno
automáticamente vía server.met (igual que hace eMule al arrancar de
fábrica). Kad (la DHT) se arranca en paralelo vía bootstrap con
nodes.dat, también automático.

Flags opcionales: --timeout <segundos> (por defecto 15), --debug (traza
en vivo del protocolo — aplica a torrent, dcpp, gnutella2 y emule).

Suite de tests automatizados (punto 34.2 del backlog, ver DEVLOG.md):
    pip install -r requirements-dev.txt                        (solo la primera vez)
    python -m pytest                                            (toda la suite)
    python -m pytest tests/test_dcpp_backend.py                (un solo fichero)
    python -m pytest -k nombre_del_test                        (un test suelto, por nombre)
    python -m pytest -v                                         (con el nombre de cada test)
"""

import asyncio
import getpass
import sys

from backends.dcpp_backend import DCPPBackend
from backends.emule_backend import EMuleBackend
from backends.g2_backend import G2Backend
from backends.soulseek_backend import SoulseekBackend
from backends.torrent_backend import TorrentBackend
from core.backend_base import BackendRegistry
from core.bandwidth_scheduler import effective_limits_kbps
from core.config import CONFIG_PATH, load_config, save_config
from core.download_manager import DownloadManager
from core.models import Network, SearchResult
from core.rate_limiter import apply_global_limits
from core.sharing import SharedLibrary

def _print_progress(download) -> None:
    pct = download.progress * 100
    mb_done = download.downloaded_bytes / 1_048_576
    mb_total = download.size_bytes / 1_048_576
    speed_kbps = download.speed_bps / 1024
    print(
        f"\r[{download.network.value}] {download.title[:40]:<40} "
        f"{pct:5.1f}%  {mb_done:8.1f}/{mb_total:8.1f} MB  "
        f"{speed_kbps:7.1f} KB/s  {download.state.value:<18}",
        end="", flush=True,
    )


async def cmd_search(query: str, timeout: float, debug: bool, network: Network,
                      hub_override: str | None = None) -> None:
    if network == Network.TORRENT:
        config = load_config()
        torrent_backend = TorrentBackend(proxy=config.proxy)
        await torrent_backend.connect()
        print(f"Buscando/resolviendo '{query}' (timeout {timeout:.0f}s)...")
        try:
            results = await torrent_backend.search(query, timeout=timeout, debug=debug)
        except TimeoutError:
            print(
                "Sin resultados: no se obtuvieron metadatos a tiempo.\n"
                "Posibles causas: (1) el magnet no tiene peers activos ahora mismo,\n"
                "(2) firewall bloqueando UDP/6881, (3) la DHT aún no ha terminado de\n"
                "arrancar en este equipo — prueba con --timeout 60 en el primer intento."
            )
            await torrent_backend.disconnect()
            return
        if not results:
            print("Sin resultados (ni en apibay.org ni resolviendo el magnet/infohash directamente).")
        for r in results:
            print(f"- {r.title}  ({r.size_bytes / 1_048_576:.1f} MB, {r.seeds_or_sources} seeds)")
            print(f"  source_id: {r.source_id}")
        await torrent_backend.disconnect()

    elif network == Network.SOULSEEK:
        backend = await _build_soulseek_backend()
        print(f"Buscando '{query}' en Soulseek (esperando {timeout:.0f}s a que lleguen resultados; "
              f"se muestran según van llegando, sin esperar a que acabe el tiempo)...\n")

        # Solo con hueco libre: si el usuario está en cola, la descarga
        # puede tardar minutos/horas en arrancar o no arrancar nunca —
        # no tiene sentido mostrarlo como opción práctica. Deduplicado por
        # (título, tamaño): el mismo archivo suele estar compartido por
        # decenas de usuarios a la vez; mostrar solo el primero de cada uno
        # evita una lista repetitiva sin aportar nada.
        seen: set[tuple[str, int]] = set()
        display_limit = 100
        shown = 0

        def _print_incremental(r: SearchResult) -> None:
            nonlocal shown
            if not r.extra.get("has_free_slots"):
                return
            key = (r.title, r.size_bytes)
            if key in seen:
                return
            seen.add(key)
            shown += 1
            if shown <= display_limit:
                print(f"- {r.title}  ({r.size_bytes / 1_048_576:.1f} MB, user: {r.extra.get('username')})")
                print(f"  source_id: {r.source_id}")

        results = await backend.search(query, timeout=timeout, on_result=_print_incremental)
        available = [r for r in results if r.extra.get("has_free_slots")]

        if not results:
            print("Sin resultados (o nadie con esa query compartiendo ahora mismo).")
            await backend.disconnect()
            return

        print(
            f"\n({len(results)} resultados totales, {len(available)} con hueco libre, "
            f"{shown} tras quitar duplicados)"
        )
        if shown > display_limit:
            print(f"  ... y {shown - display_limit} resultados únicos más (con hueco libre)")
        await backend.disconnect()

    elif network == Network.GNUTELLA2:
        backend = await _build_g2_backend(hub_override, timeout, debug)
        print(f"Buscando '{query}' en Gnutella2 (esperando {timeout:.0f}s a que lleguen resultados)...")
        results = await backend.search(query, timeout=timeout, debug=debug)
        if not results:
            print("Sin resultados (o el hub no reenvió la búsqueda a nadie que coincida).")
        for r in results:
            print(f"- {r.title}  ({r.size_bytes / 1_048_576:.1f} MB)")
            print(f"  source_id: {r.source_id}")

        hubs = backend.discovered_hubs
        if hubs:
            print(
                f"\n({len(hubs)} hub(s) más descubiertos por la propia red vía "
                f"/KHL durante esta sesión — se puede forzar cualquiera de "
                f"ellos la próxima vez con --hub host:puerto):"
            )
            for ip, port in list(hubs)[:10]:
                print(f"  {ip}:{port}")
            if len(hubs) > 10:
                print(f"  ... y {len(hubs) - 10} más")

        await backend.disconnect()

    elif network == Network.DCPP:
        backend = await _build_dcpp_backend(hub_override, debug=debug)
        print(f"Buscando '{query}' en DC++ (esperando {timeout:.0f}s a que lleguen resultados)...")
        results = await backend.search(query, timeout=timeout, debug=debug)
        if not results:
            print("Sin resultados (o nadie con esa query compartiendo ahora mismo en este hub).")
        for r in results:
            print(f"- {r.title}  ({r.size_bytes / 1_048_576:.1f} MB, {r.seeds_or_sources} huecos libres)")
            print(f"  source_id: {r.source_id}")
        await backend.disconnect()

    elif network == Network.EMULE:
        backend = await _build_emule_backend(hub_override, debug=debug)
        print(f"Buscando '{query}' en eD2k/Kad (esperando {timeout:.0f}s a que lleguen resultados)...")
        results = await backend.search(query, timeout=timeout, debug=debug)
        if not results:
            print("Sin resultados (ni por el servidor eD2k ni por Kad).")
        for r in results:
            print(f"- {r.title}  ({r.size_bytes / 1_048_576:.1f} MB, {r.seeds_or_sources} fuentes conocidas)")
            print(f"  source_id: {r.source_id}")
        await backend.disconnect()


async def cmd_download(query: str, dest: str, timeout: float, network: Network,
                        hub_override: str | None = None) -> None:
    config = load_config()
    apply_global_limits(config)  # límites globales (punto 2 del backlog, con planificador por franja horaria del 34.5); las cuatro redes "manuales" comparten el limitador de core.rate_limiter
    download_kbps, upload_kbps = effective_limits_kbps(config)

    if network == Network.TORRENT:
        torrent_backend = TorrentBackend(proxy=config.proxy)
        await torrent_backend.connect()
        torrent_backend.set_global_limits(download_kbps * 1024, upload_kbps * 1024)
        BackendRegistry.register(torrent_backend)

        manager = DownloadManager()
        manager.on_progress(_print_progress)

        print(f"Resolviendo '{query}' en {network.value}...")
        results = await manager.search_all(query, networks=[network])
        if not results:
            print("No se encontraron resultados.")
            return
        result = results[0]

    elif network == Network.SOULSEEK:
        # Defensa contra un problema real que nos pasó: al copiar el
        # source_id desde una terminal que envuelve líneas largas visualmente
        # (con su propio prompt decorativo), es fácil arrastrar sin querer
        # un salto de línea y caracteres de decoración pegados al final del
        # texto copiado. Eso corrompe silenciosamente el remote_path (el
        # archivo real no tiene esos caracteres) y el peer responde
        # "File not shared" de forma confusa, sin pista de la causa real.
        raw_query = query
        query = query.strip()
        if raw_query != query:
            print(
                "⚠ Se han quitado espacios/saltos de línea sobrantes al "
                "principio o final del source_id (probable artefacto de "
                "copiar y pegar desde la terminal). Si la descarga falla "
                "igualmente, prueba a redirigir 'search' a un archivo de "
                "texto y copiar el source_id desde ahí en vez de la "
                "terminal interactiva, para evitar arrastrar el wrap visual."
            )

        # A propósito NO reutilizamos search_all() aquí. Si a 'download'
        # le pasas texto libre en vez de un source_id real, el fallo
        # anterior era exactamente este: se lanzaba una búsqueda nueva
        # y se descargaba a ciegas el primer resultado que devolviera la
        # red — casi nunca lo que el usuario pretendía. Para Soulseek,
        # 'download' exige el source_id exacto (formato "usuario:::ruta")
        # que te dio una búsqueda previa con 'search'.
        if ":::" not in query:
            print(
                "Para Soulseek, 'download' necesita el source_id exacto de un\n"
                "resultado de 'search' (formato usuario:::ruta), no texto libre.\n"
                "Ejemplo: python main.py search \"algo\" --network soulseek\n"
                "         (copia el source_id de un resultado real y pégalo aquí,\n"
                "          siempre entre comillas simples por si tiene espacios)"
            )
            return

        soulseek_backend = await _build_soulseek_backend()
        BackendRegistry.register(soulseek_backend)
        manager = DownloadManager()
        manager.on_progress(_print_progress)

        username, remote_path = query.split(":::", 1)
        title = remote_path.rsplit("\\", 1)[-1]
        # El tamaño real se desconoce hasta que el peer confirma la
        # transferencia (TransferRequest); el progreso lo actualizará en
        # cuanto lo sepa.
        result = SearchResult(
            network=Network.SOULSEEK,
            title=title,
            size_bytes=0,
            source_id=query,
        )

    elif network == Network.GNUTELLA2:
        raw_query = query
        query = query.strip()
        if raw_query != query:
            print("⚠ Se han quitado espacios/saltos de línea sobrantes al principio o final del source_id.")

        # Formato nuevo (con GUID del origen, hace falta para el
        # fallback /PUSH): host:puerto:::hash_base32:::guid_hex:::nombre
        # (3 separadores). Se acepta también el formato viejo sin GUID
        # (2 separadores) por si el source_id viene de una búsqueda
        # anterior a que se añadiera ese campo.
        if query.count(":::") not in (2, 3):
            print(
                "Para Gnutella2, 'download' necesita el source_id exacto de un\n"
                "resultado de 'search' (formato host:puerto:::hash_base32:::"
                "guid_hex:::nombre),\n"
                "no texto libre.\n"
                "Ejemplo: python main.py search \"algo\" --network gnutella2 --hub host:puerto\n"
                "         (copia el source_id de un resultado real y pégalo aquí,\n"
                "          siempre entre comillas simples por si tiene espacios)"
            )
            return

        g2_backend = await _build_g2_backend(hub_override, timeout, debug=False)
        BackendRegistry.register(g2_backend)
        manager = DownloadManager()
        manager.on_progress(_print_progress)

        filename = query.split(":::")[-1]
        result = SearchResult(
            network=Network.GNUTELLA2,
            title=filename,
            size_bytes=0,
            source_id=query,
        )

    elif network == Network.DCPP:
        raw_query = query
        query = query.strip()
        if raw_query != query:
            print("⚠ Se han quitado espacios/saltos de línea sobrantes al principio o final del source_id.")

        if ":::" not in query:
            print(
                "Para DC++, 'download' necesita el source_id exacto de un\n"
                "resultado de 'search' (formato nick:::ruta), no texto libre.\n"
                "Ejemplo: python main.py search \"algo\" --network dcpp\n"
                "         (copia el source_id de un resultado real y pégalo aquí,\n"
                "          siempre entre comillas simples por si tiene espacios)"
            )
            return

        dcpp_backend = await _build_dcpp_backend(hub_override)
        BackendRegistry.register(dcpp_backend)
        manager = DownloadManager()
        manager.on_progress(_print_progress)

        nick, remote_path = query.split(":::", 1)
        title = remote_path.rsplit("\\", 1)[-1]
        result = SearchResult(
            network=Network.DCPP,
            title=title,
            size_bytes=0,
            source_id=query,
        )

    elif network == Network.EMULE:
        raw_query = query
        query = query.strip()
        if raw_query != query:
            print("⚠ Se han quitado espacios/saltos de línea sobrantes al principio o final del source_id.")

        if query.count(":::") != 2:
            print(
                "Para eMule, 'download' necesita el source_id exacto de un\n"
                "resultado de 'search' (formato hash_hex:::tamaño:::nombre),\n"
                "no texto libre.\n"
                "Ejemplo: python main.py search \"algo\" --network emule\n"
                "         (copia el source_id de un resultado real y pégalo aquí,\n"
                "          siempre entre comillas simples por si tiene espacios)"
            )
            return

        emule_backend = await _build_emule_backend(hub_override, debug=False)
        BackendRegistry.register(emule_backend)
        manager = DownloadManager()
        manager.on_progress(_print_progress)

        file_hash_hex, size_str, title = query.split(":::", 2)
        result = SearchResult(
            network=Network.EMULE,
            title=title,
            size_bytes=int(size_str),
            source_id=query,
        )

    download = await manager.download(result, dest)
    print(f"Descarga iniciada: {download.title} -> {dest}")
    print("Ctrl+C para parar (esto CORTA la descarga: es un solo proceso, no hay demonio en background todavía).\n")

    try:
        while download.state.value not in ("completed", "error", "cancelled"):
            await asyncio.sleep(1)
        if download.state.value == "error":
            print(f"\nLa descarga falló: {download.error_message or 'motivo desconocido'}")
        elif download.state.value == "completed":
            print(f"\nDescarga completada: {dest}/{download.title}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nMonitor detenido por el usuario.")


async def _build_soulseek_backend() -> SoulseekBackend:
    config = load_config()
    if not config.is_soulseek_configured():
        print(
            "Soulseek no está configurado todavía. Ejecuta primero:\n"
            "  python main.py config"
        )
        sys.exit(1)
    backend = SoulseekBackend(
        config.soulseek.username,
        config.soulseek.password,
        download_dir=config.default_download_dir,
        listen_port=config.soulseek.listen_port,
        shared_library=SharedLibrary(config.shared_folders),
        proxy=config.proxy,
    )
    await backend.connect()
    return backend


def _parse_hub_address(hub_str: str) -> tuple[str, int]:
    """
    Acepta varios formatos habituales de dirección de hub DC++:
      - dchub://host:puerto
      - dchub://host          (sin puerto -> 411 por defecto)
      - host:puerto
      - host                  (sin puerto -> 411 por defecto)
    """
    hub_str = hub_str.strip()
    if "://" in hub_str:
        hub_str = hub_str.split("://", 1)[1]
    hub_str = hub_str.rstrip("/")  # por si viene con barra final

    if ":" in hub_str:
        host, port_str = hub_str.rsplit(":", 1)
        return host, int(port_str)
    return hub_str, 411


async def _build_g2_backend(hub_override: str | None, timeout: float, debug: bool) -> G2Backend:
    config = load_config()
    backend = G2Backend(
        listen_port=config.gnutella2.listen_port,
        shared_library=SharedLibrary(config.shared_folders),
        proxy=config.proxy,
    )
    await backend.connect()

    if hub_override:
        host, port = _parse_hub_address(hub_override)
    elif config.gnutella2.default_hub_host:
        host, port = config.gnutella2.default_hub_host, config.gnutella2.default_hub_port
    else:
        # Sin hub indicado: descubrimiento automático vía GWebCache
        # (G2 no tiene UHC por UDP como G1 — es el único mecanismo real
        # de bootstrap para esta red).
        print("Sin hub indicado — buscando uno automáticamente vía GWebCache...")
        try:
            connected_host, connected_port = await backend.connect_auto(debug=debug)
            print(f"Conectado automáticamente a {connected_host}:{connected_port} (handshake 0.6 aceptado).")
        except ConnectionError as e:
            print(f"No se pudo conectar automáticamente: {e}")
            sys.exit(1)
        return backend

    print(f"Conectando al hub G2 {host}:{port}...")
    try:
        connected_host, connected_port = await backend.connect_to_hub_with_fallback(
            host, port, timeout=timeout, debug=debug
        )
    except (OSError, asyncio.TimeoutError, ConnectionError, EOFError) as e:
        # Un hub concreto (el indicado a mano, o el de por defecto
        # guardado en la config) puede llevar caído o ya no ser hub
        # desde hace rato — la red G2 tiene bastante rotación de hubs.
        # En vez de morir aquí, caemos al descubrimiento automático en
        # paralelo (mismo camino que si no se hubiera indicado ningún
        # hub) en lugar de dejar al usuario colgado con ese único host.
        print(f"{host}:{port} no aceptó la conexión ({e}); "
              f"buscando un hub automáticamente vía GWebCache...")
        try:
            connected_host, connected_port = await backend.connect_auto(debug=debug)
            print(f"Conectado automáticamente a {connected_host}:{connected_port} (handshake 0.6 aceptado).")
        except ConnectionError as e2:
            print(f"No se pudo conectar automáticamente: {e2}")
            sys.exit(1)
        return backend
    if (connected_host, connected_port) != (host, port):
        print(
            f"{host}:{port} estaba lleno (503); conectado en su lugar a un "
            f"hub alternativo recibido vía X-Try-Hubs: {connected_host}:{connected_port}"
        )
    print("Handshake 0.6 (G2) aceptado.")
    return backend


async def _build_dcpp_backend(hub_override: str | None = None, debug: bool = False) -> DCPPBackend:
    config = load_config()
    if not config.dcpp.is_configured():
        print(
            "DC++ no está configurado todavía. Ejecuta primero:\n"
            "  python main.py config"
        )
        sys.exit(1)

    if hub_override:
        host, port = _parse_hub_address(hub_override)
        password = None
    else:
        if not config.dcpp.default_hub_host:
            print(
                "No hay hub por defecto configurado. Indica uno con --hub host:puerto\n"
                "o configura uno por defecto con: python main.py config"
            )
            sys.exit(1)
        host = config.dcpp.default_hub_host
        port = config.dcpp.default_hub_port
        password = config.dcpp.default_hub_password or None

    backend = DCPPBackend(
        config.dcpp.nickname,
        listen_port=config.dcpp.listen_port,
        shared_library=SharedLibrary(config.shared_folders),
        proxy=config.proxy,
    )
    await backend.connect()  # arranca nuestro listener (modo activo)
    print(f"Conectando al hub {host}:{port}...")
    await backend.connect_to_hub(host, port, password=password, timeout=15.0, debug=debug)
    print(f"Conectado y logueado como '{config.dcpp.nickname}'.")
    return backend


async def _build_emule_backend(server_override: str | None = None, debug: bool = False) -> EMuleBackend:
    config = load_config()
    backend = EMuleBackend(
        config.emule.nickname,
        listen_port=config.emule.listen_port,
        kad_udp_port=config.emule.kad_udp_port,
        shared_library=SharedLibrary(config.shared_folders),
        proxy=config.proxy,
        obfuscation=config.emule.obfuscation,
    )
    await backend.connect()  # arranca nuestro listener (callbacks) y el socket UDP de Kad

    if server_override:
        host, port = _parse_hub_address(server_override)
        if port == 411:  # _parse_hub_address usa 411 (DC++) como puerto por defecto si no se indica
            port = 4661
        print(f"Conectando al servidor eD2k {host}:{port}...")
        await backend.connect_to_server(host, port, timeout=15.0, debug=debug)
    elif config.emule.default_server_host:
        host, port = config.emule.default_server_host, config.emule.default_server_port
        print(f"Conectando al servidor eD2k por defecto {host}:{port}...")
        await backend.connect_to_server(host, port, timeout=15.0, debug=debug)
    else:
        print("Sin servidor indicado — descubriendo uno automáticamente vía server.met...")
        try:
            host, port = await backend.connect_auto(timeout_per_server=8.0, debug=debug)
            print(f"Conectado automáticamente al servidor eD2k {host}:{port}.")
        except ConnectionError as e:
            print(f"No se pudo conectar automáticamente: {e}")
            sys.exit(1)

    print("Iniciando bootstrap de Kad (best-effort, vía nodes.dat)...")
    kad_contacts = await backend.connect_kad(timeout=8.0, debug=debug)
    print(f"Kad: {kad_contacts} contacto(s) conocidos tras el bootstrap.")

    return backend


def cmd_config() -> None:
    """Configuración interactiva, guardada en ~/.config/p2p-total/config.json."""
    config = load_config()

    print(f"Configuración de P2P Total (se guarda en {CONFIG_PATH})\n")

    print("--- Soulseek ---")
    if config.is_soulseek_configured():
        print(f"Usuario actual: {config.soulseek.username}")
        if input("¿Cambiar credenciales? [s/N]: ").strip().lower() != "s":
            print("Sin cambios en Soulseek.")
        else:
            config.soulseek.username = input("Usuario: ").strip()
            config.soulseek.password = getpass.getpass("Contraseña: ")
    else:
        print("Sin configurar. Si el usuario no existe todavía en la red,")
        print("se registra solo con esta contraseña en el primer login.")
        config.soulseek.username = input("Usuario: ").strip()
        config.soulseek.password = getpass.getpass("Contraseña: ")

    print()
    print("--- DC++ ---")
    if config.dcpp.is_configured():
        print(f"Nick actual: {config.dcpp.nickname}")
        print(f"Hub por defecto: {config.dcpp.default_hub_host}:{config.dcpp.default_hub_port}")
        print(f"Puerto de escucha (modo activo): {config.dcpp.listen_port}")
        if input("¿Cambiar configuración de DC++? [s/N]: ").strip().lower() != "s":
            print("Sin cambios en DC++.")
        else:
            config.dcpp.nickname = input("Nick: ").strip()
            hub_input = input("Hub por defecto (host:puerto, Enter para dejar sin uno por defecto): ").strip()
            if hub_input:
                host, _, port_str = hub_input.partition(":")
                config.dcpp.default_hub_host = host
                config.dcpp.default_hub_port = int(port_str) if port_str else 411
            hub_pass = getpass.getpass("Contraseña del hub (Enter si no requiere): ")
            config.dcpp.default_hub_password = hub_pass
            listen_input = input(
                f"Puerto de escucha [{config.dcpp.listen_port}] (Enter para dejar el actual, "
                "debe ser >1024 salvo que corras con sudo): "
            ).strip()
            if listen_input:
                config.dcpp.listen_port = int(listen_input)
    else:
        print("Sin configurar. DC++ es una red de hubs: necesitas un nick")
        print("y, si quieres, un hub por defecto (host:puerto). Puedes indicar")
        print("un hub distinto en cada búsqueda con --hub host:puerto.")
        config.dcpp.nickname = input("Nick: ").strip()
        hub_input = input("Hub por defecto (host:puerto, Enter para omitir): ").strip()
        if hub_input:
            host, _, port_str = hub_input.partition(":")
            config.dcpp.default_hub_host = host
            config.dcpp.default_hub_port = int(port_str) if port_str else 411
            hub_pass = getpass.getpass("Contraseña del hub (Enter si no requiere): ")
            config.dcpp.default_hub_password = hub_pass
        listen_input = input(
            f"Puerto de escucha [{config.dcpp.listen_port}] (Enter para dejar el por defecto, "
            "debe ser >1024 salvo que corras con sudo): "
        ).strip()
        if listen_input:
            config.dcpp.listen_port = int(listen_input)

    print()
    print("--- Gnutella2 (G2) ---")
    print("No necesita cuenta. Usa hubs (como DC++), no nodos sueltos, y se")
    print("descubre uno automáticamente vía GWebCache si no fijas ninguno")
    print("(puede fallar si los dos GWebCache de G2 conocidos están caídos).")
    print("Solo hace falta esto si quieres saltarte el descubrimiento y fijar")
    print("siempre el mismo hub.")
    hub_input = input(
        f"Hub por defecto [{config.gnutella2.default_hub_host or 'ninguno, descubrimiento automático'}] "
        "(host:puerto, Enter para dejar el actual): "
    ).strip()
    if hub_input:
        host, _, port_str = hub_input.partition(":")
        config.gnutella2.default_hub_host = host
        config.gnutella2.default_hub_port = int(port_str) if port_str else 6346

    print()
    print("--- eMule (eD2k + Kad) ---")
    print("Tampoco necesita cuenta. El nick es solo el nombre que ven los")
    print("demás al conectar. El servidor eD2k se descubre automáticamente")
    print("vía server.met si no fijas uno por defecto (Kad se bootstrapea")
    print("aparte, siempre automático, vía nodes.dat).")
    nick_input = input(f"Nick [{config.emule.nickname}] (Enter para dejar el actual): ").strip()
    if nick_input:
        config.emule.nickname = nick_input
    server_input = input(
        f"Servidor eD2k por defecto [{config.emule.default_server_host or 'ninguno, descubrimiento automático'}] "
        "(host:puerto, Enter para dejar el actual): "
    ).strip()
    if server_input:
        host, _, port_str = server_input.partition(":")
        config.emule.default_server_host = host
        config.emule.default_server_port = int(port_str) if port_str else 4661
    print(
        "Ofuscación de protocolo [disabled/enabled/required] "
        f"[{config.emule.obfuscation}] (esquiva el throttling de tráfico P2P de algunos ISPs; "
        "'enabled' la soporta y prefiere pero sigue aceptando conexiones sin ofuscar, "
        "'required' rechaza las que no vengan ofuscadas): "
    )
    obf_input = input("Enter para dejar el actual: ").strip().lower()
    if obf_input in ("disabled", "enabled", "required"):
        config.emule.obfuscation = obf_input

    print()
    print(f"Carpeta de descargas actual: {config.default_download_dir}")
    nueva = input("Nueva carpeta (Enter para dejar la actual): ").strip()
    if nueva:
        config.default_download_dir = nueva

    print()
    actual_share = ", ".join(config.shared_folders) if config.shared_folders else "(nada compartido)"
    print(f"Carpetas compartidas actuales: {actual_share}")
    print("Lo que haya dentro se sirve a otros peers en Soulseek, DC++,")
    print("Gnutella2 y eMule/eD2k.")
    nueva_share = input(
        "Nuevas carpetas compartidas, separadas por coma (Enter para dejar "
        "las actuales, '-' para no compartir nada): "
    ).strip()
    if nueva_share == "-":
        config.shared_folders = []
    elif nueva_share:
        config.shared_folders = [p.strip() for p in nueva_share.split(",") if p.strip()]

    print()
    print("--- Proxy saliente ---")
    print("Opcional: para pasar las conexiones TCP salientes de las cinco")
    print("redes (Soulseek, DC++, Gnutella2, eMule/eD2k y BitTorrent) por un")
    print("proxy SOCKS5 o HTTP CONNECT. El tráfico UDP (Kad, hub de")
    print("Gnutella2) sigue yendo directo, sin proxear.")
    estado_actual = f"activado ({config.proxy.kind}, {config.proxy.host}:{config.proxy.port})" if config.proxy.enabled else "desactivado"
    print(f"Estado actual: {estado_actual}")
    if input("¿Cambiar configuración de proxy? [s/N]: ").strip().lower() == "s":
        usar = input("¿Usar proxy? [s/N]: ").strip().lower() == "s"
        config.proxy.enabled = usar
        if usar:
            tipo_input = input(f"Tipo [{config.proxy.kind}] (socks5/http, Enter para dejar el actual): ").strip().lower()
            if tipo_input in ("socks5", "http"):
                config.proxy.kind = tipo_input
            host_input = input(f"Servidor [{config.proxy.host}] (Enter para dejar el actual): ").strip()
            if host_input:
                config.proxy.host = host_input
            port_input = input(f"Puerto [{config.proxy.port}] (Enter para dejar el actual): ").strip()
            if port_input:
                config.proxy.port = int(port_input)
            user_input = input(f"Usuario [{config.proxy.username or '(ninguno)'}] (Enter para dejar el actual): ").strip()
            if user_input:
                config.proxy.username = user_input
            if config.proxy.username:
                config.proxy.password = getpass.getpass("Contraseña (Enter para dejar la actual): ") or config.proxy.password

    save_config(config)
    print(f"\nGuardado en {CONFIG_PATH} (permisos 600, solo tu usuario puede leerlo).")


def main() -> None:
    if len(sys.argv) < 2:
        from gui.app import run_gui
        run_gui()
        return

    command = sys.argv[1]

    if command in ("--help", "-h"):
        print(__doc__)
        return

    if command == "config":
        cmd_config()
        return

    if command == "gui":
        from gui.app import run_gui
        run_gui()
        return

    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    query = sys.argv[2]

    timeout = 15.0
    if "--timeout" in sys.argv:
        idx = sys.argv.index("--timeout")
        timeout = float(sys.argv[idx + 1])

    debug = "--debug" in sys.argv

    network = Network.TORRENT
    if "--network" in sys.argv:
        idx = sys.argv.index("--network")
        network = Network(sys.argv[idx + 1])

    hub_override = None
    if "--hub" in sys.argv:
        idx = sys.argv.index("--hub")
        hub_override = sys.argv[idx + 1]

    if command == "search":
        asyncio.run(cmd_search(query, timeout, debug, network, hub_override))
    elif command == "download":
        if len(sys.argv) < 4 or sys.argv[3].startswith("-"):
            print("Falta la carpeta destino: python main.py download <source_id> <carpeta> [--network ...]")
            sys.exit(1)
        asyncio.run(cmd_download(query, sys.argv[3], timeout, network, hub_override))
    else:
        print(f"Comando desconocido: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
