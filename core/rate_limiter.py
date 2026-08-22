"""
Limitador de velocidad tipo "cubo de fichas" (token bucket), usado por
las cuatro redes que gestionan su propio socket a mano (Soulseek, DC++,
Gnutella2 y eD2k/eMule): cada bucle de lectura/escritura llama a
`consume(n_bytes)` tras cada trozo leído/escrito y, si se ha superado
la velocidad fijada, la corrutina se queda dormida el tiempo justo para
volver a estar dentro del límite. BitTorrent no lo necesita: libtorrent
ya trae su propio limitador nativo (ver `TorrentBackend.set_global_limits`
y `set_download_limit`).
"""

import asyncio
import time


class RateLimiter:
    """`rate_bps <= 0` significa "sin límite": `consume()` no espera
    nunca en ese caso (comprobación rápida antes de tocar el reloj)."""

    def __init__(self, rate_bps: int = 0) -> None:
        self._rate_bps = rate_bps
        self._tokens = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate_bps(self) -> int:
        return self._rate_bps

    def set_rate(self, rate_bps: int) -> None:
        self._rate_bps = max(0, rate_bps)

    async def consume(self, n_bytes: int) -> None:
        if self._rate_bps <= 0 or n_bytes <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(
                float(self._rate_bps),
                self._tokens + (now - self._last) * self._rate_bps,
            )
            self._last = now
            if self._tokens < n_bytes:
                await asyncio.sleep((n_bytes - self._tokens) / self._rate_bps)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= n_bytes


# Limitadores globales compartidos por las cuatro redes "manuales": viven
# a nivel de módulo (un único proceso, ver CLAUDE.md) para que cualquier
# bucle de transferencia en curso, de cualquier backend, recoja el nuevo
# límite en cuanto se cambie desde Preferencias sin necesidad de
# reconectar. Cada RateLimiter concreto de una descarga se encadena con
# estos: primero se consume del global y luego del propio de la
# descarga, así que la velocidad real queda acotada por el más estricto
# de los dos.
global_download_limiter = RateLimiter()
global_upload_limiter = RateLimiter()


def apply_global_limits(config) -> None:
    """Aplica a los limitadores globales compartidos los límites de
    `Config` (kB/s, 0 = ilimitado). Se llama al conectar cualquier red y
    cada vez que se guardan Preferencias, para que el cambio surta
    efecto en caliente en las descargas/subidas ya en marcha."""
    global_download_limiter.set_rate(config.global_download_limit_kbps * 1024)
    global_upload_limiter.set_rate(config.global_upload_limit_kbps * 1024)
