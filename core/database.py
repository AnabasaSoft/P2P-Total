"""
Persistencia ligera en SQLite. Guarda el estado de las descargas
para poder reanudar la app sin perder la cola ni el historial.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import _config_dir, is_portable_mode
from core.models import Download, DownloadState, Network, SavedSearch, SearchHistoryEntry, SearchResult

# Modo portable (punto 25 del backlog, ver core/config.py): la base de
# datos se guarda junto al ejecutable en vez de en ~/.local/share cuando
# está activado. Se calcula una sola vez al importar el módulo, igual
# que `CONFIG_PATH` en core/config.py: cambiar el modo portable necesita
# reiniciar la aplicación para que surta efecto.
DB_PATH = (_config_dir() / "downloads.db") if is_portable_mode() else (
    Path.home() / ".local" / "share" / "p2p-manager" / "downloads.db"
)

# Máximo de búsquedas que se guardan en el historial (punto 7 del backlog):
# pasado este número se borran las más antiguas al insertar una nueva, para
# que la tabla no crezca sin límite con el uso normal de la GUI.
SEARCH_HISTORY_LIMIT = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network TEXT NOT NULL,
    title TEXT NOT NULL,
    source_id TEXT NOT NULL,
    dest_path TEXT NOT NULL,
    size_bytes INTEGER DEFAULT 0,
    downloaded_bytes INTEGER DEFAULT 0,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    category TEXT,
    added_at TEXT NOT NULL,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS search_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    networks TEXT NOT NULL,
    file_type TEXT NOT NULL,
    searched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    networks TEXT NOT NULL,
    file_type TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 30,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    seen_keys TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS saved_search_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_search_id INTEGER NOT NULL,
    network TEXT NOT NULL,
    title TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    found_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS network_stats (
    network TEXT PRIMARY KEY,
    total_downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    total_uploaded_bytes INTEGER NOT NULL DEFAULT 0,
    total_connected_seconds INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS network_stats_daily (
    date TEXT NOT NULL,
    network TEXT NOT NULL,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    uploaded_bytes INTEGER NOT NULL DEFAULT 0,
    connected_seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, network)
);
"""


@contextmanager
def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        # Migración para bases de datos ya existentes de antes del punto 4
        # del backlog (columna 'priority' nueva): se rellena con el propio
        # id, así las descargas más antiguas quedan arriba de la cola,
        # igual que aparecían antes con el orden por 'added_at'.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(downloads)")}
        if "priority" not in columns:
            conn.execute("ALTER TABLE downloads ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE downloads SET priority = id")
        # Migración para el punto 5 del backlog (categorías): columna
        # nueva 'category', NULL para todo lo descargado antes de existir
        # el concepto (equivale a "sin categoría").
        if "category" not in columns:
            conn.execute("ALTER TABLE downloads ADD COLUMN category TEXT")


def insert_download(d: Download) -> int:
    with get_connection() as conn:
        # Las descargas nuevas se añaden al final de la cola (prioridad
        # más alta que cualquier otra ya existente).
        next_priority = conn.execute("SELECT COALESCE(MAX(priority), -1) + 1 FROM downloads").fetchone()[0]
        d.priority = next_priority
        cur = conn.execute(
            """INSERT INTO downloads
               (network, title, source_id, dest_path, size_bytes,
                downloaded_bytes, state, priority, category, added_at, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d.network.value, d.title, d.source_id, d.dest_path, d.size_bytes,
             d.downloaded_bytes, d.state.value, d.priority, d.category, d.added_at.isoformat(), d.error_message),
        )
        return cur.lastrowid


def reorder_downloads(pairs: list[tuple[int, int]]) -> None:
    """`pairs` es una lista de (download_id, priority) ya calculada por
    `DownloadManager.reorder()` a partir del nuevo orden de filas de la
    pestaña Transferencias (arrastrar filas o subir/bajar en el menú
    contextual)."""
    with get_connection() as conn:
        conn.executemany(
            "UPDATE downloads SET priority = ? WHERE id = ?",
            [(priority, download_id) for download_id, priority in pairs],
        )


def update_download_progress(download_id: int, downloaded_bytes: int, state: DownloadState) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE downloads SET downloaded_bytes = ?, state = ? WHERE id = ?",
            (downloaded_bytes, state.value, download_id),
        )


def delete_download(download_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))


def delete_downloads_by_state(state: DownloadState) -> list[int]:
    """Borra del historial todas las descargas en ese estado y devuelve
    los IDs borrados (la GUI los usa para quitarlos también de la tabla)."""
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM downloads WHERE state = ?", (state.value,)).fetchall()
        ids = [row["id"] for row in rows]
        conn.execute("DELETE FROM downloads WHERE state = ?", (state.value,))
    return ids


def load_all_downloads() -> list[Download]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM downloads ORDER BY priority ASC, added_at DESC").fetchall()
    result = []
    for row in rows:
        result.append(Download(
            id=row["id"],
            network=Network(row["network"]),
            title=row["title"],
            source_id=row["source_id"],
            dest_path=row["dest_path"],
            size_bytes=row["size_bytes"],
            downloaded_bytes=row["downloaded_bytes"],
            state=DownloadState(row["state"]),
            priority=row["priority"],
            category=row["category"],
            error_message=row["error_message"],
        ))
    return result


def insert_search_history(query: str, networks: list[Network], file_type: str) -> int:
    """Guarda una búsqueda lanzada desde la GUI y recorta el historial a
    `SEARCH_HISTORY_LIMIT` entradas (se borran las más antiguas)."""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO search_history (query, networks, file_type, searched_at) VALUES (?, ?, ?, ?)",
            (query, ",".join(n.value for n in networks), file_type, datetime.utcnow().isoformat()),
        )
        conn.execute(
            """DELETE FROM search_history WHERE id NOT IN (
                   SELECT id FROM search_history ORDER BY id DESC LIMIT ?
               )""",
            (SEARCH_HISTORY_LIMIT,),
        )
        return cur.lastrowid


def load_search_history() -> list[SearchHistoryEntry]:
    """Del más reciente al más antiguo."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM search_history ORDER BY id DESC").fetchall()
    return [
        SearchHistoryEntry(
            id=row["id"],
            query=row["query"],
            networks=[Network(n) for n in row["networks"].split(",") if n],
            file_type=row["file_type"],
            searched_at=datetime.fromisoformat(row["searched_at"]),
        )
        for row in rows
    ]


def clear_search_history() -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM search_history")


# ---- Búsquedas guardadas / alertas (punto 8 del backlog) ----

def _row_to_saved_search(row) -> SavedSearch:
    return SavedSearch(
        id=row["id"],
        query=row["query"],
        networks=[Network(n) for n in row["networks"].split(",") if n],
        file_type=row["file_type"],
        interval_minutes=row["interval_minutes"],
        enabled=bool(row["enabled"]),
        last_checked_at=datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else None,
        seen_keys=set(json.loads(row["seen_keys"])),
    )


def insert_saved_search(query: str, networks: list[Network], file_type: str, interval_minutes: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO saved_searches (query, networks, file_type, interval_minutes, enabled, seen_keys)
               VALUES (?, ?, ?, ?, 1, '[]')""",
            (query, ",".join(n.value for n in networks), file_type, interval_minutes),
        )
        return cur.lastrowid


def load_saved_searches() -> list[SavedSearch]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM saved_searches ORDER BY id ASC").fetchall()
    return [_row_to_saved_search(row) for row in rows]


def load_saved_search(saved_search_id: int) -> SavedSearch | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM saved_searches WHERE id = ?", (saved_search_id,)).fetchone()
    return _row_to_saved_search(row) if row else None


def delete_saved_search(saved_search_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM saved_searches WHERE id = ?", (saved_search_id,))
        conn.execute("DELETE FROM saved_search_alerts WHERE saved_search_id = ?", (saved_search_id,))


def set_saved_search_enabled(saved_search_id: int, enabled: bool) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE saved_searches SET enabled = ? WHERE id = ?", (1 if enabled else 0, saved_search_id))


def update_saved_search_run(saved_search_id: int, seen_keys: set, checked_at: datetime) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE saved_searches SET seen_keys = ?, last_checked_at = ? WHERE id = ?",
            (json.dumps(sorted(seen_keys)), checked_at.isoformat(), saved_search_id),
        )


def insert_saved_search_alerts(saved_search_id: int, results: list[SearchResult]) -> None:
    with get_connection() as conn:
        conn.executemany(
            """INSERT INTO saved_search_alerts (saved_search_id, network, title, size_bytes, source_id, found_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (saved_search_id, r.network.value, r.title, r.size_bytes, r.source_id, datetime.utcnow().isoformat())
                for r in results
            ],
        )


def load_saved_search_alerts(saved_search_id: int) -> list[SearchResult]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM saved_search_alerts WHERE saved_search_id = ? ORDER BY id ASC", (saved_search_id,)
        ).fetchall()
    return [
        SearchResult(
            network=Network(row["network"]),
            title=row["title"],
            size_bytes=row["size_bytes"],
            source_id=row["source_id"],
        )
        for row in rows
    ]


def clear_saved_search_alerts(saved_search_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM saved_search_alerts WHERE saved_search_id = ?", (saved_search_id,))


def count_alerts_by_saved_search() -> dict[int, int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT saved_search_id, COUNT(*) AS n FROM saved_search_alerts GROUP BY saved_search_id"
        ).fetchall()
    return {row["saved_search_id"]: row["n"] for row in rows}


# ---- Estadísticas globales acumuladas por red (punto 24 del backlog) ----

def add_network_stats(network: Network, downloaded_delta: int = 0, uploaded_delta: int = 0,
                       connected_seconds_delta: float = 0.0) -> None:
    """Suma estos deltas tanto al acumulado total de la red como a la
    fila de hoy del histórico diario (creando ambas filas la primera
    vez que se registra algo de esa red). Los tres deltas son
    opcionales porque cada llamada normalmente solo aporta uno (bytes
    bajados, bytes subidos o tiempo conectado)."""
    today = datetime.utcnow().date().isoformat()
    connected_seconds_delta = int(connected_seconds_delta)
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO network_stats (network, total_downloaded_bytes, total_uploaded_bytes, total_connected_seconds)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(network) DO UPDATE SET
                   total_downloaded_bytes = total_downloaded_bytes + excluded.total_downloaded_bytes,
                   total_uploaded_bytes = total_uploaded_bytes + excluded.total_uploaded_bytes,
                   total_connected_seconds = total_connected_seconds + excluded.total_connected_seconds""",
            (network.value, downloaded_delta, uploaded_delta, connected_seconds_delta),
        )
        conn.execute(
            """INSERT INTO network_stats_daily (date, network, downloaded_bytes, uploaded_bytes, connected_seconds)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(date, network) DO UPDATE SET
                   downloaded_bytes = downloaded_bytes + excluded.downloaded_bytes,
                   uploaded_bytes = uploaded_bytes + excluded.uploaded_bytes,
                   connected_seconds = connected_seconds + excluded.connected_seconds""",
            (today, network.value, downloaded_delta, uploaded_delta, connected_seconds_delta),
        )


def get_all_network_stats() -> dict[str, dict]:
    """Clave = `Network.value`. Solo trae las redes que ya tengan algo
    registrado; las que faltan equivalen a todo a cero."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM network_stats").fetchall()
    return {
        row["network"]: {
            "total_downloaded_bytes": row["total_downloaded_bytes"],
            "total_uploaded_bytes": row["total_uploaded_bytes"],
            "total_connected_seconds": row["total_connected_seconds"],
        }
        for row in rows
    }


def get_network_stats_daily(days: int = 30) -> list[dict]:
    """Histórico diario, del día más reciente al más antiguo, hasta
    `days` días atrás (solo días con algo de actividad registrada)."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM network_stats_daily
               WHERE date >= date('now', ?)
               ORDER BY date DESC, network ASC""",
            (f"-{days} days",),
        ).fetchall()
    return [
        {
            "date": row["date"],
            "network": row["network"],
            "downloaded_bytes": row["downloaded_bytes"],
            "uploaded_bytes": row["uploaded_bytes"],
            "connected_seconds": row["connected_seconds"],
        }
        for row in rows
    ]
