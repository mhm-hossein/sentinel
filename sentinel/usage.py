"""Read-only usage readers over a per-instance ``sentinel.db``.

The control bot and the worker heartbeat need cheap, read-only snapshots of an
instance's activity without opening the worker's live aiosqlite connection.
These helpers use a short-lived, **read-only** ``sqlite3`` connection (SQLite WAL
permits concurrent readers) and never write. They degrade to zeros/empty when
the database or a table does not exist yet.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger("sentinel.usage")


def _connect_ro(db_path: str) -> Optional[sqlite3.Connection]:
    """Open a read-only connection to ``db_path``, or ``None`` if unavailable."""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:  # noqa: BLE001 - reader must never raise
        return None


def _scalar(conn: sqlite3.Connection, query: str, default: int = 0) -> int:
    """Run a scalar COUNT/SUM query, returning ``default`` on any failure."""
    try:
        cur = conn.execute(query)
        row = cur.fetchone()
        if not row:
            return default
        value = row[0]
        return int(value) if value is not None else default
    except Exception:  # noqa: BLE001 - missing table etc.
        return default


def count_active_banners(db_path: str) -> int:
    """Return the number of active banners in an instance database (0 on error)."""
    conn = _connect_ro(db_path)
    if conn is None:
        return 0
    try:
        # Banner status is stored as text/int depending on version; match both.
        return _scalar(
            conn,
            "SELECT COUNT(*) FROM banners WHERE lower(CAST(status AS TEXT)) "
            "IN ('active','1','running');",
        )
    finally:
        conn.close()


def read_usage(db_path: str) -> dict[str, int]:
    """Return a labeled usage snapshot for an instance database.

    Merges the ``sentinel_counters`` key/value rows with a couple of derived
    totals (banner count). Keys mirror ``core.stats`` counter names; the control
    bot maps them to human labels for the Usage panel.
    """
    usage: dict[str, int] = {}
    conn = _connect_ro(db_path)
    if conn is None:
        return usage
    try:
        try:
            for row in conn.execute("SELECT key, value FROM sentinel_counters;"):
                usage[str(row["key"])] = int(row["value"] or 0)
        except Exception:  # noqa: BLE001 - table may not exist yet
            pass
        usage["banners_total"] = _scalar(conn, "SELECT COUNT(*) FROM banners;")
        usage["banners_active"] = count_active_banners_from(conn)
    finally:
        conn.close()
    return usage


def count_active_banners_from(conn: sqlite3.Connection) -> int:
    """Active-banner count using an already-open connection."""
    return _scalar(
        conn,
        "SELECT COUNT(*) FROM banners WHERE lower(CAST(status AS TEXT)) "
        "IN ('active','1','running');",
    )


def read_targets(db_path: str) -> dict[str, int]:
    """Return per-module target counts for an instance (archive/ghost/hunter/banners).

    Missing tables degrade to 0 — safe on a freshly-created instance database.
    """
    out = {"banners_active": 0, "archive_targets": 0, "ghost_targets": 0, "hunter_targets": 0}
    conn = _connect_ro(db_path)
    if conn is None:
        return out
    try:
        out["banners_active"] = count_active_banners_from(conn)
        out["archive_targets"] = _scalar(conn, "SELECT COUNT(*) FROM archive_chats;")
        out["ghost_targets"] = _scalar(conn, "SELECT COUNT(*) FROM ghost_targets;")
        out["hunter_targets"] = _scalar(conn, "SELECT COUNT(*) FROM hunter_targets;")
    finally:
        conn.close()
    return out


def file_size(path: str) -> int:
    """Return a file's size in bytes (0 if missing/unreadable)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def dir_size(path: str) -> int:
    """Return the total size in bytes of a directory tree (0 on error)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        return total
    return total


def fmt_bytes(n: int) -> str:
    """Format a byte count compactly (B/KB/MB/GB)."""
    size = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
