"""
Planificador de ancho de banda por franja horaria (punto 34.5 del
backlog): sobre los límites globales de velocidad ya existentes (punto
2, `Config.global_download_limit_kbps`/`global_upload_limit_kbps`),
permite fijar unos límites "alternativos" que sustituyen a los
normales solo durante una franja horaria configurable del día (p.ej.
limitar más de noche, o durante horas de trabajo), volviendo a los
límites normales fuera de esa franja -- mismo concepto que los
"límites alternativos programados" de qBittorrent/aMule. La franja se
define con hora de inicio y fin en formato "HH:MM" y puede cruzar la
medianoche (inicio > fin, p.ej. "23:00" a "07:00").

`BandwidthScheduler` reevalúa periódicamente si toca aplicar los
límites alternativos o los normales y, si el estado cambia, reaplica
los límites globales -- mismo patrón de bucle en segundo plano que ya
usan `WatchFolderManager`/`SavedSearchManager`.
"""

import asyncio
from datetime import datetime, time as dt_time
from typing import Callable

from core.config import load_config

_POLL_SECONDS = 30


def _parse_hhmm(value: str) -> dt_time:
    hour_str, _, minute_str = value.partition(":")
    return dt_time(int(hour_str), int(minute_str))


def is_within_schedule(start: str, end: str, now: dt_time | None = None) -> bool:
    """`start`/`end` en formato "HH:MM". Si son iguales se interpreta
    como "todo el día" (franja de 24h, siempre activa). Si `start` es
    posterior a `end`, la franja cruza la medianoche (p.ej. "23:00" a
    "07:00" cubre desde las 23:00 hasta las 07:00 del día siguiente)."""
    now = now if now is not None else datetime.now().time()
    start_t, end_t = _parse_hhmm(start), _parse_hhmm(end)
    if start_t == end_t:
        return True
    if start_t < end_t:
        return start_t <= now < end_t
    return now >= start_t or now < end_t


def effective_limits_kbps(config, now: dt_time | None = None) -> tuple[int, int]:
    """Devuelve (bajada, subida) en kB/s a aplicar ahora mismo: los
    límites alternativos del planificador si está activado y la hora
    actual (o `now`, para poder probarlo sin depender del reloj real)
    cae dentro de su franja horaria, o los límites globales normales
    en caso contrario."""
    schedule = config.schedule
    if schedule.enabled and is_within_schedule(schedule.start, schedule.end, now):
        return schedule.download_limit_kbps, schedule.upload_limit_kbps
    return config.global_download_limit_kbps, config.global_upload_limit_kbps


class BandwidthScheduler:
    """La GUI solo habla con esta clase (mismo patrón que
    `WatchFolderManager`): arranca un bucle en segundo plano que
    reaplica los límites globales de velocidad (vía la función
    `apply_limits` que le pasa `MainWindow`, normalmente
    `ConnectionManager.apply_global_speed_limits`) cada vez que toca
    entrar o salir de la franja horaria configurada, sin que el
    usuario tenga que reconectar ni tocar nada manualmente. Un cambio
    de los límites o de la franja hecho a mano desde Preferencias ya
    se aplica al momento por su cuenta (ver `SettingsDialog`/
    `MainWindow._on_settings`); este bucle solo cubre la transición
    automática al llegar la hora, sin que el usuario haga nada."""

    def __init__(self, apply_limits: Callable[[], None]) -> None:
        self._apply_limits = apply_limits
        self._task: asyncio.Task | None = None
        self._active: bool | None = None  # None = todavía no evaluado

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(_POLL_SECONDS)

    def _check_once(self) -> None:
        config = load_config()
        schedule = config.schedule
        active_now = schedule.enabled and is_within_schedule(schedule.start, schedule.end)
        if active_now != self._active:
            self._active = active_now
            self._apply_limits()
