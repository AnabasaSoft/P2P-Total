"""
Filtro de IPs estilo aMule/eMule real (punto 39 del backlog): carga un
`ipfilter.dat` en el formato clásico de Bluetack/PeerGuardian —

    001.002.003.004 - 001.002.005.006 , 023 , Alguna descripción

una línea por rango (IP inicial, IP final, "nivel de acceso" 0-255, y una
descripción opcional que se ignora) — y bloquea conexiones hacia/desde IPs
que caigan en un rango cuyo nivel esté por debajo o igual al umbral
configurado, exactamente el mismo criterio que usa aMule (nivel más bajo =
rango más peligroso; el umbral por defecto, 127, es también el que trae
aMule de fábrica).

No pertenece a ninguna red en concreto: se consulta desde
`core.proxy.open_connection` (conexiones salientes de las cuatro redes
"manuales" - Soulseek, DC++, Gnutella2 y eMule) y desde el punto de
aceptación de conexión entrante de cada una de ellas. BitTorrent no lo usa
directamente porque `libtorrent` trae su propio `lt.ip_filter` nativo, al
que se le cargan los mismos rangos por separado en
`TorrentBackend.connect()`.

Asume, como el propio aMule, que los rangos del fichero no se solapan
entre sí -es como se distribuyen en la práctica las listas Bluetack- para
poder usar búsqueda binaria en vez de un recorrido lineal por cada IP
consultada.
"""

import ipaddress
import re
from bisect import bisect_right

_LINE_RE = re.compile(r"^\s*([\d.]+)\s*-\s*([\d.]+)\s*,\s*(\d+)\s*(?:,.*)?$")

DEFAULT_LEVEL_THRESHOLD = 127


class IPFilter:
    def __init__(self) -> None:
        self._enabled = False
        self._level_threshold = DEFAULT_LEVEL_THRESHOLD
        self._starts: list[int] = []
        self._ranges: list[tuple[int, int, int]] = []  # (start, end, level), ordenado por start

    def load(self, path: str) -> int:
        """Carga (sustituyendo lo que hubiera antes) los rangos de `path`.
        Devuelve cuántos rangos válidos se cargaron; si el fichero no
        existe, está vacío o no tiene líneas reconocibles, deja el filtro
        sin rangos (equivale a no bloquear nada, aunque `enabled` siga a
        `True`) en vez de lanzar una excepción -un ipfilter.dat mal escrito
        no debería impedir conectar a las redes."""
        ranges = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    match = _LINE_RE.match(line)
                    if not match:
                        continue
                    try:
                        start = int(ipaddress.IPv4Address(match.group(1)))
                        end = int(ipaddress.IPv4Address(match.group(2)))
                    except ValueError:
                        continue
                    level = int(match.group(3))
                    if start <= end:
                        ranges.append((start, end, level))
        except OSError:
            ranges = []

        ranges.sort()
        self._ranges = ranges
        self._starts = [r[0] for r in ranges]
        return len(ranges)

    def configure(self, enabled: bool, level_threshold: int = DEFAULT_LEVEL_THRESHOLD) -> None:
        self._enabled = enabled
        self._level_threshold = level_threshold

    def is_blocked(self, ip: str) -> bool:
        if not self._enabled or not self._ranges:
            return False
        try:
            value = int(ipaddress.IPv4Address(ip))
        except ValueError:
            return False  # host no-IPv4 (dominio, IPv6...): fuera del alcance de ipfilter.dat
        idx = bisect_right(self._starts, value) - 1
        if idx < 0:
            return False
        start, end, level = self._ranges[idx]
        return start <= value <= end and level <= self._level_threshold

    def rule_count(self) -> int:
        return len(self._ranges)

    def blocked_ranges(self) -> list[tuple[str, str]]:
        """Rangos (IP inicial, IP final) en notación de puntos que superan
        el umbral configurado -listos para volcar en un filtro nativo como
        `lt.ip_filter` de libtorrent (ver `TorrentBackend.reload_ip_filter`).
        Vacío si el filtro está desactivado, igual que `is_blocked`."""
        if not self._enabled:
            return []
        return [
            (str(ipaddress.IPv4Address(start)), str(ipaddress.IPv4Address(end)))
            for start, end, level in self._ranges
            if level <= self._level_threshold
        ]


# Instancia única a nivel de módulo, igual que `core.stats_tracker`: todos
# los backends consultan el mismo filtro sin tener que pasárselo unos a
# otros explícitamente.
ip_filter = IPFilter()


def apply_config(config) -> None:
    """Vuelca lo configurado en Preferencias (`config.ip_filter_enabled`/
    `ip_filter_path`/`ip_filter_level`, ver `core.config.Config`) sobre el
    filtro global: recarga el fichero si hay uno configurado y ajusta el
    interruptor general y el umbral de nivel. Se llama tanto al conectar
    cada red como al guardar Preferencias -mismo patrón que
    `core.rate_limiter.apply_global_limits`."""
    if config.ip_filter_path:
        ip_filter.load(config.ip_filter_path)
    ip_filter.configure(config.ip_filter_enabled, config.ip_filter_level)


def peer_ip_from_writer(writer) -> str | None:
    """IP remota de un `asyncio.StreamWriter` ya aceptado, o `None` si no
    está disponible (p.ej. transporte ya cerrado). Pequeño helper
    compartido por el manejador de conexión entrante de las cuatro redes
    "manuales", para no repetir el acceso a `get_extra_info` cuatro veces."""
    peername = writer.get_extra_info("peername")
    return peername[0] if peername else None
