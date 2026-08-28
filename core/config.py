"""
Configuración persistente de P2P Total.

Se guarda en ~/.config/p2p-total/config.json (respeta $XDG_CONFIG_HOME
si está definida). Pensado para ir creciendo: de momento solo lleva las
credenciales de Soulseek y la carpeta de descargas por defecto, pero
aquí es donde irán el resto de ajustes del programa (límites de
velocidad, puertos, credenciales de DC++, etc.) a medida que se añadan
las demás redes.

Las contraseñas (Soulseek, hub de DC++, proxy) no se guardan en texto
plano en este json salvo que no quede más remedio: se intentan guardar
primero en el almacén de credenciales del sistema operativo (Secret
Service/KWallet en Linux, Keychain en macOS, Credential Manager en
Windows, vía la librería `keyring`) y solo se dejan en el json si eso
falla, o en modo portable (donde tiene más sentido que el propio
config.json sea autocontenido). Ver `_keyring_get`/`_keyring_set` más
abajo.
"""

import json
import logging
import os
import shutil
import stat
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import keyring
import keyring.errors

from core.models import Network
from core.proxy import ProxyConfig

logger = logging.getLogger(__name__)

# Modo portable (punto 25 del backlog): ver `is_portable_mode()` más abajo.
PORTABLE_DATA_DIRNAME = "p2p-total-data"

# Servicio bajo el que se guardan en el almacén de credenciales del sistema
# (Secret Service/KWallet en Linux, Keychain en macOS, Credential Manager en
# Windows, vía la librería `keyring`) las contraseñas de `Config` en vez de
# en texto plano en `config.json`: la de Soulseek, la del hub de DC++ y la
# del proxy. En modo portable se guardan en el propio `config.json` de la
# carpeta portable en su lugar, porque el almacén de credenciales es del
# equipo anfitrión, no del pendrive, y usarlo dejaría precisamente el rastro
# en el equipo anfitrión que el modo portable busca evitar.
_KEYRING_SERVICE = "p2p-total"


def _keyring_get(key: str) -> str | None:
    """`None` si no hay nada guardado bajo esa clave, si estamos en modo
    portable, o si el almacén de credenciales no está disponible (p.ej. un
    Linux sin sesión de escritorio, sin Secret Service en marcha)."""
    if is_portable_mode():
        return None
    try:
        return keyring.get_password(_KEYRING_SERVICE, key)
    except keyring.errors.KeyringError:
        logger.warning("No se pudo leer %r del almacén de credenciales del sistema", key, exc_info=True)
        return None


def _keyring_set(key: str, value: str) -> bool:
    """Intenta guardar `value` en el almacén de credenciales del sistema.
    Devuelve si lo consiguió: si no (modo portable, o almacén no
    disponible), la contraseña se queda en texto plano en `config.json`
    como hasta ahora, en vez de perderse."""
    if is_portable_mode():
        return False
    try:
        if value:
            keyring.set_password(_KEYRING_SERVICE, key, value)
        else:
            try:
                keyring.delete_password(_KEYRING_SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass  # no había nada guardado bajo esa clave: nada que borrar
        return True
    except keyring.errors.KeyringError:
        logger.warning("No se pudo guardar %r en el almacén de credenciales del sistema", key, exc_info=True)
        return False


def _keyring_password_or(from_keyring: str | None, from_json: str) -> str:
    """`from_keyring` si había algo guardado bajo esa clave, si no lo que
    hubiera en `config.json` (portable, almacén no disponible, o un
    `config.json` de antes de este cambio que aún no se ha vuelto a
    guardar)."""
    return from_keyring if from_keyring is not None else from_json


def _executable_dir() -> Path:
    if getattr(sys, "frozen", False):  # empaquetado (PyInstaller o similar)
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent


def _portable_marker_path() -> Path:
    return _executable_dir() / "portable.marker"


def is_portable_mode() -> bool:
    """Si existe un fichero `portable.marker` junto a `main.py` (o el
    ejecutable empaquetado), toda la configuración y los datos
    (config.json, downloads.db, cachés de identidad/servidores/contactos
    conocidos de eD2k/Kad/G2...) se guardan en una carpeta
    `p2p-total-data` junto a él en vez de en `~/.config/p2p-total` y
    `~/.local/share/p2p-manager` — pensado para poder llevar el programa
    entero (código + datos) en un pendrive sin dejar rastro en el
    usuario del equipo donde se ejecute. Se activa/desactiva desde la
    propia GUI (Preferencias → General, ver `enable_portable_mode`/
    `disable_portable_mode`); el cambio no tiene efecto hasta reiniciar
    la aplicación, porque estas rutas se calculan una sola vez al
    arrancar (igual que `CONFIG_PATH`, más abajo)."""
    return _portable_marker_path().exists()


def _config_dir() -> Path:
    if is_portable_mode():
        return _executable_dir() / PORTABLE_DATA_DIRNAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "p2p-total"


CONFIG_PATH = _config_dir() / "config.json"

# Nombres de las cachés que también viven en `_config_dir()` (ver
# backends/emule_backend.py y backends/g2_backend.py) y la ruta clásica
# de la base de datos (ver core/database.py): se usan solo para copiar
# los datos ya existentes al activar el modo portable, sin borrar los
# originales por si se desactiva más tarde.
_PORTABLE_CACHE_FILENAMES = (
    "g2_hub_cache.json",
    "ed2k_server_cache.json",
    "kad_contacts_cache.json",
    "ed2k_identity.json",
    "ed2k_credits.json",
    "ed2k_friends.json",
)
_LEGACY_DB_PATH = Path.home() / ".local" / "share" / "p2p-manager" / "downloads.db"


@dataclass
class TorrentConfig:
    # A diferencia del resto de campos de búsqueda de esta misma clase,
    # aquí `search_timeout` cubre tanto la espera de metadatos por DHT
    # (magnet/infohash/.torrent ya identificado) como el propio GET a
    # apibay.org (punto 29 del backlog: búsqueda de torrents por
    # nombre); `max_results` solo aplica a esta segunda, ya que la
    # resolución DHT siempre devuelve como mucho un resultado.
    max_results: int = 0      # 0 = ilimitado
    search_timeout: float = 15.0
    auto_connect: bool = False  # conectar esta red sola al arrancar la GUI (ver ConnectionManager.autoconnect_configured_networks)
    # Punto 38 del backlog: al alcanzar cualquiera de los dos límites (el
    # que no sea 0) durante la siembra, TorrentBackend pausa el torrent
    # solo -no cancela ni borra nada, igual que un pause manual.
    seed_ratio_limit: float = 0.0        # 0 = sin límite (subido / tamaño total)
    seed_time_limit_minutes: int = 0     # 0 = sin límite, minutos sembrando tras completarse


@dataclass
class SoulseekConfig:
    username: str = ""
    password: str = ""
    listen_port: int = 2234
    max_results: int = 0      # 0 = ilimitado (solo lo limita search_timeout)
    search_timeout: float = 20.0
    auto_connect: bool = False


@dataclass
class DCPPConfig:
    nickname: str = ""
    default_hub_host: str = ""
    default_hub_port: int = 411
    default_hub_password: str = ""   # vacío = sin contraseña (hub abierto)
    listen_port: int = 41290  # >1024: los puertos reservados (<1024) necesitan root en Linux
    max_results: int = 0      # 0 = ilimitado (solo lo limita search_timeout)
    search_timeout: float = 20.0
    auto_connect: bool = False

    def is_configured(self) -> bool:
        return bool(self.nickname)


@dataclass
class Gnutella2Config:
    # Gnutella2 usa hubs, no nodos sueltos — mismo concepto que el
    # --hub de DC++. Tampoco necesita cuenta.
    default_hub_host: str = ""
    default_hub_port: int = 6346
    listen_port: int = 6346   # puerto fijo para recibir conexiones /PUSH entrantes (reenviable en el router)
    max_results: int = 0      # 0 = ilimitado (solo lo limita search_timeout)
    search_timeout: float = 20.0
    auto_connect: bool = False


@dataclass
class EMuleConfig:
    # eD2k/Kad tampoco necesitan cuenta: el nick es solo el nombre que
    # ven los demás peers al conectar. El servidor por defecto es
    # opcional — si no se fija ninguno, se descubre uno automáticamente
    # vía server.met (igual que hace el eMule real de fábrica).
    nickname: str = "P2PTotalUser"
    default_server_host: str = ""
    default_server_port: int = 4661
    listen_port: int = 4662       # puerto TCP cliente-a-cliente (el "de libro" de eMule)
    kad_udp_port: int = 4672      # puerto UDP Kad (el "de libro" de eMule)
    max_results: int = 0          # 0 = ilimitado (solo lo limita search_timeout)
    search_timeout: float = 20.0
    # Ofuscación de protocolo (punto 20 del backlog, ver backends/emule_backend.py):
    # "disabled" = nunca la ofrecemos ni la aceptamos; "enabled" = la
    # soportamos y preferimos pero seguimos aceptando conexiones sin
    # ofuscar (por defecto, igual que el eMule real de fábrica);
    # "required" = rechazamos cualquier conexión entrante que no venga
    # ofuscada (puede romper la ruta de "callback" para fuentes LowID
    # si el otro lado no tiene ya en caché nuestro userhash).
    obfuscation: str = "enabled"
    auto_connect: bool = False


@dataclass
class UIConfig:
    # Preferencias de la GUI. "dark" es el valor por defecto porque el
    # estilo buscado (aMule/Shareaza) es tradicionalmente oscuro.
    theme: str = "dark"           # "dark" | "light"
    language: str = "es"          # "es" | "en" | "eu"
    window_width: int = 1100
    window_height: int = 680
    window_x: int | None = None   # None = sin guardar todavía, deja que el
    window_y: int | None = None   # gestor de ventanas decida la posición inicial
    minimize_to_tray: bool = False  # al cerrar la ventana, minimizar al icono de
                                     # la bandeja del sistema en vez de salir del todo
    minimize_to_tray_on_minimize: bool = False  # también al pulsar el botón de
                                                 # minimizar de la ventana (no solo al cerrarla)
    notify_on_download_finish: bool = True  # aviso nativo del sistema al completar
                                             # o fallar una descarga (vía el icono
                                             # de la bandeja, si hay uno disponible)
    notify_on_chat_message: bool = True  # aviso nativo al recibir un mensaje privado
                                          # de chat con la ventana minimizada/oculta


@dataclass
class Category:
    """Categoría de descarga al estilo aMule/qBittorrent: un nombre
    ("Música", "Vídeos"...) con su propia carpeta de destino asociada,
    que se usa en vez de `default_download_dir` cuando la descarga se
    arranca eligiendo esa categoría."""
    name: str
    dest_dir: str


@dataclass
class ScheduleConfig:
    """Planificador de ancho de banda por franja horaria (punto 34.5
    del backlog, ver `core/bandwidth_scheduler.py`): mientras
    `enabled` está activo y la hora actual cae entre `start` y `end`
    (formato "HH:MM", `start` > `end` cruza la medianoche), se aplican
    `download_limit_kbps`/`upload_limit_kbps` en vez de los límites
    globales normales de `Config`."""
    enabled: bool = False
    start: str = "22:00"
    end: str = "07:00"
    download_limit_kbps: int = 0  # 0 = ilimitado durante la franja
    upload_limit_kbps: int = 0    # 0 = ilimitado durante la franja


@dataclass
class RemoteControlConfig:
    """Control remoto / API web (punto 34.6 del backlog, ver
    `core/remote_control.py`): permite gestionar las descargas desde un
    navegador sin abrir la ventana de escritorio. Desactivado por
    defecto; hace falta fijar un `token` propio (Preferencias ofrece un
    botón para generar uno aleatorio) antes de poder activarlo, porque
    de lo contrario cualquiera con acceso al puerto podría gestionar
    las descargas -- hay que mandarlo en cada petición a la API
    (cabecera "Authorization: Bearer <token>" o "?token="). `host` por
    defecto es solo localhost ("127.0.0.1"); ponerlo a "0.0.0.0" expone
    la API/página a toda la red local, una decisión explícita del
    usuario."""
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""


@dataclass
class Config:
    torrent: TorrentConfig = field(default_factory=TorrentConfig)
    soulseek: SoulseekConfig = field(default_factory=SoulseekConfig)
    dcpp: DCPPConfig = field(default_factory=DCPPConfig)
    gnutella2: Gnutella2Config = field(default_factory=Gnutella2Config)
    emule: EMuleConfig = field(default_factory=EMuleConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    remote_control: RemoteControlConfig = field(default_factory=RemoteControlConfig)
    default_download_dir: str = str(Path.home() / "Descargas" / "P2P-Total")
    shared_folders: list[str] = field(default_factory=list)   # vacía = no se comparte nada (ver core/sharing.py)
    global_download_limit_kbps: int = 0   # 0 = ilimitado; aplica a la suma de todas las redes/descargas
    global_upload_limit_kbps: int = 0     # 0 = ilimitado; aplica a la suma de todas las subidas servidas
    categories: list[Category] = field(default_factory=list)  # vacía = sin categorías configuradas
    auto_retry_max_attempts: int = 3      # 0 = desactivado; reintentos automáticos cuando una descarga cae en error
    auto_retry_delay_seconds: float = 30.0  # espera entre el error y cada reintento automático
    watched_torrent_dir: str = ""         # vacío = desactivado (punto 26 del backlog, ver core/watch_folder.py)
    auto_verify_on_complete: bool = False  # verificar automáticamente el contenido al completar una descarga (punto 27), solo redes que lo soporten
    # Punto 39 del backlog: filtro de IPs estilo aMule/eMule real
    # (ipfilter.dat, formato Bluetack). No es propio de ninguna red -se
    # aplica a las cinco por igual (ver core/ip_filter.py)-, así que vive
    # aquí y no en ninguna *Config por red.
    ip_filter_enabled: bool = False
    ip_filter_path: str = ""              # vacío = sin fichero configurado
    ip_filter_level: int = 127            # umbral de nivel de acceso (0-255, igual convención y valor por defecto que aMule)

    def is_soulseek_configured(self) -> bool:
        return bool(self.soulseek.username and self.soulseek.password)

    def auto_connect_networks(self) -> list[Network]:
        """Redes marcadas en Preferencias para conectar solas al arrancar
        la GUI (una por red, ver `auto_connect` en cada *Config), en el
        mismo orden que recorre `core.models.Network`."""
        per_network = {
            Network.TORRENT: self.torrent,
            Network.SOULSEEK: self.soulseek,
            Network.DCPP: self.dcpp,
            Network.GNUTELLA2: self.gnutella2,
            Network.EMULE: self.emule,
        }
        return [network for network, cfg in per_network.items() if cfg.auto_connect]


def load_config(path: Path | None = None) -> Config:
    """`path`, si se indica, permite leer un `config.json` de cualquier
    otra ubicación en vez del real (usado por la importación de
    configuración desde la GUI, punto 25 del backlog). En ese caso el
    fichero importado se trata como autocontenido y no se toca el
    almacén de credenciales del sistema: las contraseñas se leen tal
    cual vengan en su propio json, igual que en modo portable."""
    use_keyring = path is None  # explícita: no tocar el keyring en importación/exportación ni en la transición a portable (`_keyring_get`/`_keyring_set` ya evitan el keyring por su cuenta en modo portable)
    path = path or CONFIG_PATH
    if not path.exists():
        return Config()

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    torrent_data = data.get("torrent", {})     # {} si es de antes de añadir la búsqueda por nombre (punto 29)
    soulseek_data = data.get("soulseek", {})
    dcpp_data = data.get("dcpp", {})           # {} si el config.json es de antes de añadir DC++
    gnutella2_data = data.get("gnutella2", {}) # {} si es de antes de añadir Gnutella2
    emule_data = data.get("emule", {})         # {} si es de antes de añadir eMule
    ui_data = data.get("ui", {})                 # {} si es de antes de añadir la GUI
    proxy_data = data.get("proxy", {})           # {} si es de antes de añadir el soporte de proxy
    schedule_data = data.get("schedule", {})     # {} si es de antes de añadir el planificador (punto 34.5)
    remote_control_data = data.get("remote_control", {})  # {} si es de antes de añadir el control remoto (punto 34.6)
    return Config(
        torrent=TorrentConfig(
            max_results=torrent_data.get("max_results", 0),
            search_timeout=torrent_data.get("search_timeout", 15.0),
            auto_connect=torrent_data.get("auto_connect", False),
            seed_ratio_limit=torrent_data.get("seed_ratio_limit", 0.0),
            seed_time_limit_minutes=torrent_data.get("seed_time_limit_minutes", 0),
        ),
        soulseek=SoulseekConfig(
            username=soulseek_data.get("username", ""),
            password=_keyring_password_or(_keyring_get("soulseek") if use_keyring else None, soulseek_data.get("password", "")),
            listen_port=soulseek_data.get("listen_port", 2234),
            max_results=soulseek_data.get("max_results", 0),
            search_timeout=soulseek_data.get("search_timeout", 20.0),
            auto_connect=soulseek_data.get("auto_connect", False),
        ),
        dcpp=DCPPConfig(
            nickname=dcpp_data.get("nickname", ""),
            default_hub_host=dcpp_data.get("default_hub_host", ""),
            default_hub_port=dcpp_data.get("default_hub_port", 411),
            default_hub_password=_keyring_password_or(_keyring_get("dcpp_hub") if use_keyring else None, dcpp_data.get("default_hub_password", "")),
            listen_port=dcpp_data.get("listen_port", 41290),
            max_results=dcpp_data.get("max_results", 0),
            search_timeout=dcpp_data.get("search_timeout", 20.0),
            auto_connect=dcpp_data.get("auto_connect", False),
        ),
        gnutella2=Gnutella2Config(
            default_hub_host=gnutella2_data.get("default_hub_host", ""),
            default_hub_port=gnutella2_data.get("default_hub_port", 6346),
            listen_port=gnutella2_data.get("listen_port", 6346),
            max_results=gnutella2_data.get("max_results", 0),
            search_timeout=gnutella2_data.get("search_timeout", 20.0),
            auto_connect=gnutella2_data.get("auto_connect", False),
        ),
        emule=EMuleConfig(
            nickname=emule_data.get("nickname", "P2PTotalUser"),
            default_server_host=emule_data.get("default_server_host", ""),
            default_server_port=emule_data.get("default_server_port", 4661),
            listen_port=emule_data.get("listen_port", 4662),
            kad_udp_port=emule_data.get("kad_udp_port", 4672),
            max_results=emule_data.get("max_results", 0),
            search_timeout=emule_data.get("search_timeout", 20.0),
            obfuscation=emule_data.get("obfuscation", "enabled"),
            auto_connect=emule_data.get("auto_connect", False),
        ),
        ui=UIConfig(
            theme=ui_data.get("theme", "dark"),
            language=ui_data.get("language", "es"),
            window_width=ui_data.get("window_width", 1100),
            window_height=ui_data.get("window_height", 680),
            window_x=ui_data.get("window_x"),
            window_y=ui_data.get("window_y"),
            minimize_to_tray=ui_data.get("minimize_to_tray", False),
            minimize_to_tray_on_minimize=ui_data.get("minimize_to_tray_on_minimize", False),
            notify_on_download_finish=ui_data.get("notify_on_download_finish", True),
            notify_on_chat_message=ui_data.get("notify_on_chat_message", True),
        ),
        proxy=ProxyConfig(
            enabled=proxy_data.get("enabled", False),
            kind=proxy_data.get("kind", "socks5"),
            host=proxy_data.get("host", ""),
            port=proxy_data.get("port", 1080),
            username=proxy_data.get("username", ""),
            password=_keyring_password_or(_keyring_get("proxy") if use_keyring else None, proxy_data.get("password", "")),
        ),
        schedule=ScheduleConfig(
            enabled=schedule_data.get("enabled", False),
            start=schedule_data.get("start", "22:00"),
            end=schedule_data.get("end", "07:00"),
            download_limit_kbps=schedule_data.get("download_limit_kbps", 0),
            upload_limit_kbps=schedule_data.get("upload_limit_kbps", 0),
        ),
        remote_control=RemoteControlConfig(
            enabled=remote_control_data.get("enabled", False),
            host=remote_control_data.get("host", "127.0.0.1"),
            port=remote_control_data.get("port", 8765),
            token=_keyring_password_or(
                _keyring_get("remote_control_token") if use_keyring else None, remote_control_data.get("token", "")
            ),
        ),
        default_download_dir=data.get("default_download_dir", Config().default_download_dir),
        # "shared_folder" (str) es el nombre del campo antes de admitir
        # varias carpetas a la vez; se migra a la lista nueva si es lo
        # único que hay en un config.json guardado por una versión previa.
        shared_folders=data.get("shared_folders") or ([data["shared_folder"]] if data.get("shared_folder") else []),
        global_download_limit_kbps=data.get("global_download_limit_kbps", 0),
        global_upload_limit_kbps=data.get("global_upload_limit_kbps", 0),
        categories=[Category(name=c["name"], dest_dir=c["dest_dir"]) for c in data.get("categories", [])],
        auto_retry_max_attempts=data.get("auto_retry_max_attempts", 3),
        auto_retry_delay_seconds=data.get("auto_retry_delay_seconds", 30.0),
        watched_torrent_dir=data.get("watched_torrent_dir", ""),
        auto_verify_on_complete=data.get("auto_verify_on_complete", False),
        ip_filter_enabled=data.get("ip_filter_enabled", False),
        ip_filter_path=data.get("ip_filter_path", ""),
        ip_filter_level=data.get("ip_filter_level", 127),
    )


def save_config(config: Config, path: Path | None = None) -> None:
    """`path`, si se indica, exporta a cualquier otra ubicación en vez de
    la real (usado por la exportación de configuración desde la GUI,
    punto 25 del backlog, y por la transición a modo portable, ver
    `enable_portable_mode`). En ese caso el fichero exportado se trata
    como autocontenido y no se toca el almacén de credenciales del
    sistema: las contraseñas se guardan tal cual, en su propio json."""
    use_keyring = path is None  # ver el mismo razonamiento en `load_config`
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    soulseek_dict = asdict(config.soulseek)
    dcpp_dict = asdict(config.dcpp)
    proxy_dict = asdict(config.proxy)
    remote_control_dict = asdict(config.remote_control)
    # Si se consigue guardar cada contraseña en el almacén de credenciales
    # del sistema, no se duplica en texto plano en config.json (en modo
    # portable, o si el almacén no está disponible, se queda en el json
    # como hasta ahora, ver `_keyring_set`).
    if use_keyring and _keyring_set("soulseek", config.soulseek.password):
        soulseek_dict["password"] = ""
    if use_keyring and _keyring_set("dcpp_hub", config.dcpp.default_hub_password):
        dcpp_dict["default_hub_password"] = ""
    if use_keyring and _keyring_set("proxy", config.proxy.password):
        proxy_dict["password"] = ""
    if use_keyring and _keyring_set("remote_control_token", config.remote_control.token):
        remote_control_dict["token"] = ""

    data = {
        "torrent": asdict(config.torrent),
        "soulseek": soulseek_dict,
        "dcpp": dcpp_dict,
        "gnutella2": asdict(config.gnutella2),
        "emule": asdict(config.emule),
        "ui": asdict(config.ui),
        "proxy": proxy_dict,
        "schedule": asdict(config.schedule),
        "remote_control": remote_control_dict,
        "default_download_dir": config.default_download_dir,
        "shared_folders": config.shared_folders,
        "global_download_limit_kbps": config.global_download_limit_kbps,
        "global_upload_limit_kbps": config.global_upload_limit_kbps,
        "categories": [asdict(c) for c in config.categories],
        "auto_retry_max_attempts": config.auto_retry_max_attempts,
        "auto_retry_delay_seconds": config.auto_retry_delay_seconds,
        "watched_torrent_dir": config.watched_torrent_dir,
        "auto_verify_on_complete": config.auto_verify_on_complete,
        "ip_filter_enabled": config.ip_filter_enabled,
        "ip_filter_path": config.ip_filter_path,
        "ip_filter_level": config.ip_filter_level,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Por si alguna contraseña no se pudo mover al almacén de credenciales
    # del sistema y se ha quedado en texto plano: que solo lo pueda leer
    # el propio usuario (equivalente a chmod 600).
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def enable_portable_mode(config: Config) -> None:
    """Activa el modo portable (punto 25 del backlog, ver
    `is_portable_mode` más arriba): guarda `config` en la carpeta
    portable, copia ahí (sin borrar los originales) la base de datos de
    descargas y las cachés de identidad/servidores/contactos conocidos
    si ya existían, y por último deja el fichero marcador que hace que,
    a partir del próximo arranque, tanto `_config_dir()` como
    `core.database.DB_PATH` apunten a esa carpeta."""
    old_dir = _config_dir()  # todavía sin marcador: resuelve a la ruta clásica
    portable_dir = _executable_dir() / PORTABLE_DATA_DIRNAME
    portable_dir.mkdir(parents=True, exist_ok=True)

    save_config(config, portable_dir / "config.json")
    for filename in _PORTABLE_CACHE_FILENAMES:
        src = old_dir / filename
        if src.exists():
            shutil.copy2(src, portable_dir / filename)
    if _LEGACY_DB_PATH.exists():
        shutil.copy2(_LEGACY_DB_PATH, portable_dir / "downloads.db")

    _portable_marker_path().write_text(
        "Este fichero activa el modo portable de P2P Total (punto 25 del "
        "backlog): borralo, o desmarca la opción correspondiente en "
        "Preferencias -> General, para volver a guardar los datos en "
        "~/.config/p2p-total y ~/.local/share/p2p-manager.\n",
        encoding="utf-8",
    )


def disable_portable_mode() -> None:
    """Desactiva el modo portable borrando el fichero marcador. No borra
    la carpeta `p2p-total-data`: si se reactiva más tarde, los datos
    siguen ahí tal cual se dejaron."""
    _portable_marker_path().unlink(missing_ok=True)
