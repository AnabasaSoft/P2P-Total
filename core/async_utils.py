"""Utilidades asíncronas genéricas compartidas por los backends y por
`core/sharing.py`."""

import asyncio
import threading


async def run_in_daemon_thread(func, *args, name: str = "daemon-thread-task", **kwargs):
    """Como `await asyncio.to_thread(func, *args, **kwargs)`, pero en un
    hilo `daemon=True` propio en vez del `ThreadPoolExecutor` por defecto
    de asyncio: los hilos de ese pool NO son daemon, así que el
    intérprete de Python los espera al salir (vía `atexit`) -confirmado
    en real primero con `SharedLibrary.rescan` (cerrar la app mientras
    seguía hasheando en segundo plano dejaba el proceso colgado hasta que
    el escaneo completo acababa por su cuenta) y después con la
    verificación de hash al completar una descarga en DC++/eMule/
    Gnutella2 (mismo síntoma: "Salir" desde la bandeja quitaba el icono
    pero el proceso seguía vivo en memoria si una descarga terminaba justo
    antes de cerrar). Con un hilo daemon, el proceso puede cerrarse de
    inmediato aunque la tarea se corte a medias."""
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()

    def _runner() -> None:
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - se reenvía tal cual a quien espera
            loop.call_soon_threadsafe(future.set_exception, exc)
        else:
            loop.call_soon_threadsafe(future.set_result, result)

    threading.Thread(target=_runner, daemon=True, name=name).start()
    return await future
