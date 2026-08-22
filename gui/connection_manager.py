"""
Puente entre la GUI y los backends de red.

Reutiliza exactamente la misma lógica de conexión que ya validó la CLI
(`main.py`, funciones `_build_*_backend`), pero sin `print`/`sys.exit`:
en su lugar expone el estado de cada red vía una señal Qt para que el
panel de redes de la GUI pueda pintar el "piloto" de color
correspondiente (desconectado / conectando / conectado / error).
"""

from PyQt6.QtCore import QObject, pyqtSignal

from backends.dcpp_backend import DCPPBackend
from backends.emule_backend import EMuleBackend
from backends.g2_backend import G2Backend
from backends.soulseek_backend import SoulseekBackend
from backends.torrent_backend import TorrentBackend
from core.backend_base import NetworkBackend
from core.config import load_config
from core.download_manager import DownloadManager
from core.models import Network
from core.rate_limiter import apply_global_limits
from core.sharing import SharedLibrary
from core.stats_tracker import stats_tracker

STATUS_DISCONNECTED = "disconnected"
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_ERROR = "error"


class ConnectionManager(QObject):
    # network.value, status, mensaje (solo relevante en status == error)
    status_changed = pyqtSignal(str, str, str)

    def __init__(self, download_manager: DownloadManager) -> None:
        super().__init__()
        self._download_manager = download_manager
        self._backends: dict[Network, NetworkBackend] = {}
        self._statuses: dict[Network, str] = {n: STATUS_DISCONNECTED for n in Network}

    def status(self, network: Network) -> str:
        return self._statuses[network]

    def is_connected(self, network: Network) -> bool:
        return self._statuses[network] == STATUS_CONNECTED

    def connected_networks(self) -> list[Network]:
        return [n for n, s in self._statuses.items() if s == STATUS_CONNECTED]

    def get_backend(self, network: Network) -> NetworkBackend | None:
        return self._backends.get(network)

    def _set_status(self, network: Network, status: str, message: str = "") -> None:
        self._statuses[network] = status
        self.status_changed.emit(network.value, status, message)

    async def connect_network(self, network: Network, hub_override: tuple[str, int] | None = None) -> None:
        if self._statuses[network] in (STATUS_CONNECTING, STATUS_CONNECTED):
            return
        self._set_status(network, STATUS_CONNECTING)
        try:
            backend = await self._build_backend(network, hub_override)
        except Exception as e:
            self._set_status(network, STATUS_ERROR, str(e))
            return
        self._download_manager.register_backend(backend)
        self._backends[network] = backend
        self._apply_speed_limits(backend, load_config())
        stats_tracker.connection_opened(network)
        self._set_status(network, STATUS_CONNECTED)

    def apply_global_speed_limits(self) -> None:
        """Se llama al guardar Preferencias: aplica en caliente los
        límites globales nuevos a todas las redes ya conectadas, sin
        necesidad de reconectar (ver `core.rate_limiter.apply_global_limits`
        para las cuatro redes "manuales", que comparten un limitador a
        nivel de módulo; BitTorrent recibe el suyo aquí explícitamente
        porque usa el limitador propio de su sesión de libtorrent)."""
        config = load_config()
        for backend in self._backends.values():
            self._apply_speed_limits(backend, config)

    @staticmethod
    def _apply_speed_limits(backend: NetworkBackend, config) -> None:
        apply_global_limits(config)
        backend.set_global_limits(
            config.global_download_limit_kbps * 1024,
            config.global_upload_limit_kbps * 1024,
        )

    async def disconnect_network(self, network: Network) -> None:
        backend = self._backends.pop(network, None)
        if backend is not None:
            try:
                await backend.disconnect()
            except Exception:
                pass  # una desconexión ruidosa no debe impedir marcarla como desconectada
        stats_tracker.connection_closed(network)
        self._set_status(network, STATUS_DISCONNECTED)

    async def _build_backend(self, network: Network, hub_override: tuple[str, int] | None = None) -> NetworkBackend:
        config = load_config()

        if network == Network.TORRENT:
            backend = TorrentBackend(proxy=config.proxy)
            await backend.connect()
            return backend

        if network == Network.SOULSEEK:
            if not config.is_soulseek_configured():
                raise RuntimeError("Soulseek no está configurado (Preferencias > Soulseek).")
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

        if network == Network.DCPP:
            if not config.dcpp.is_configured():
                raise RuntimeError("DC++ no está configurado (Preferencias > DC++).")
            if hub_override is not None:
                host, port = hub_override
                password = None  # enlace dchub:// pegado: no lleva contraseña
            else:
                host, port = config.dcpp.default_hub_host, config.dcpp.default_hub_port
                password = config.dcpp.default_hub_password or None
            if not host:
                raise RuntimeError("Falta un hub por defecto (Preferencias > DC++).")
            backend = DCPPBackend(
                config.dcpp.nickname,
                listen_port=config.dcpp.listen_port,
                shared_library=SharedLibrary(config.shared_folders),
                proxy=config.proxy,
            )
            await backend.connect()
            await backend.connect_to_hub(host, port, password=password, timeout=15.0)
            return backend

        if network == Network.GNUTELLA2:
            backend = G2Backend(
                listen_port=config.gnutella2.listen_port,
                shared_library=SharedLibrary(config.shared_folders),
                proxy=config.proxy,
            )
            await backend.connect()
            if config.gnutella2.default_hub_host:
                try:
                    await backend.connect_to_hub_with_fallback(
                        config.gnutella2.default_hub_host, config.gnutella2.default_hub_port, timeout=15.0
                    )
                except (OSError, ConnectionError, EOFError):
                    await backend.connect_auto()
            else:
                await backend.connect_auto()
            return backend

        if network == Network.EMULE:
            backend = EMuleBackend(
                config.emule.nickname,
                listen_port=config.emule.listen_port,
                kad_udp_port=config.emule.kad_udp_port,
                shared_library=SharedLibrary(config.shared_folders),
                proxy=config.proxy,
                obfuscation=config.emule.obfuscation,
            )
            await backend.connect()
            if config.emule.default_server_host:
                await backend.connect_to_server(
                    config.emule.default_server_host, config.emule.default_server_port, timeout=15.0
                )
            else:
                await backend.connect_auto(timeout_per_server=8.0)
            await backend.connect_kad(timeout=8.0)
            return backend

        raise RuntimeError(f"Red desconocida: {network}")
