"""Comprobación de nueva versión disponible, consultando el último
release publicado en GitHub (`AnabasaSoft/P2P-Total`). Reutiliza
`core.http_client.http_get`, sin depender de ninguna librería HTTP de
terceros, siguiendo la misma restricción de diseño que el resto del
proyecto."""

import json

from core.http_client import http_get
from core.proxy import ProxyConfig
from core.version import VERSION

_RELEASES_API_URL = "https://api.github.com/repos/AnabasaSoft/P2P-Total/releases/latest"
_RELEASES_PAGE_URL = "https://github.com/AnabasaSoft/P2P-Total/releases/latest"


def _parse_version(tag: str) -> tuple[int, ...]:
    """Convierte una etiqueta tipo "v1.2.3" o "1.2" en una tupla de
    enteros comparable, ignorando cualquier prefijo no numérico."""
    parts = []
    for chunk in tag.strip().lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


async def check_for_update(proxy: ProxyConfig | None = None, timeout: float = 8.0) -> tuple[str, str] | None:
    """Devuelve (versión_nueva, url_de_la_release) si GitHub tiene
    publicado un release más reciente que `VERSION`, o `None` si no lo
    hay o si la comprobación falla (sin conexión, GitHub caído,
    límite de peticiones anónimas agotado...) — nunca debe impedir que
    la app arranque."""
    try:
        body = await http_get(
            _RELEASES_API_URL, timeout=timeout, proxy=proxy,
            headers={"Accept": "application/vnd.github+json"},
        )
        data = json.loads(body)
        tag = data.get("tag_name")
        if not tag:
            return None
        if _parse_version(tag) > _parse_version(VERSION):
            return tag.lstrip("vV"), data.get("html_url") or _RELEASES_PAGE_URL
    except Exception:
        pass
    return None
