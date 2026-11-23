"""SQLite persistence for the Ghost Tracker module.

Three tables, all created idempotently in the shared ``sentinel.db``:

    * ``ghost_targets``   -- monitored users and their scan watermark.
    * ``ghost_snapshots`` -- the latest profile snapshot per user (one row each,
      upserted) used as the baseline for change detection.
    * ``ghost_history``   -- the full, append-only log of detected changes.

The snapshot is stored as a JSON document so the set of tracked fields can grow
over time without a schema migration.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import aiosqlite

from .models import GhostHistoryEntry, GhostTarget

logger = logging.getLogger("sentinel.ghost.database")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ghost_targets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL UNIQUE,
    username     TEXT,
    display_name TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    last_check   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ghost_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL UNIQUE,
    snapshot_json TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ghost_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    field_name  TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    detected_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ghost_history_user ON ghost_history (user_id);
CREATE INDEX IF NOT EXISTS idx_ghost_targets_enabled ON ghost_targets (enabled);
"""


class GhostDatabase:
    """Async persistence for Ghost Tracker targets, snapshots and history."""

    def __init__(self, path: str) -> None:
        """Initialise the database.

        Args:
            path: Path to the shared SQLite database file.
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the connection and ensure Ghost tables exist."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("Ghost database ready at %s", self._path)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("GhostDatabase is not connected")
        return self._conn

    async def healthy(self) -> bool:
        """Return ``True`` if the database responds (never raises)."""
        try:
            async with self._db.execute("SELECT 1;") as cur:
                await cur.fetchone()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ #
    # Targets
    # ------------------------------------------------------------------ #
    async def add_target(
        self, user_id: int, username: Optional[str], display_name: Optional[str]
    ) -> GhostTarget:
        """Insert (or re-enable / refresh) a monitored user."""
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO ghost_targets
                (user_id, username, display_name, enabled, created_at, updated_at, last_check)
            VALUES (?, ?, ?, 1, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                enabled = 1,
                updated_at = excluded.updated_at;
            """,
            (user_id, username, display_name, now, now),
        )
        await self._db.commit()
        target = await self.get_target(user_id)
        assert target is not None
        return target

    async def remove_target(self, user_id: int) -> bool:
        """Remove a monitored user and all of its snapshots/history.

        Returns:
            ``True`` if the target existed.
        """
        cursor = await self._db.execute(
            "DELETE FROM ghost_targets WHERE user_id = ?;", (user_id,)
        )
        await self._db.execute(
            "DELETE FROM ghost_snapshots WHERE user_id = ?;", (user_id,)
        )
        await self._db.execute(
            "DELETE FROM ghost_history WHERE user_id = ?;", (user_id,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_target(self, user_id: int) -> Optional[GhostTarget]:
        """Fetch a monitored user by id, or ``None``."""
        async with self._db.execute(
            "SELECT * FROM ghost_targets WHERE user_id = ?;", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return _target_from_row(row) if row else None

    async def list_targets(self, *, enabled_only: bool = False) -> list[GhostTarget]:
        """Return monitored users ordered by creation time."""
        query = "SELECT * FROM ghost_targets"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at;"
        async with self._db.execute(query) as cur:
            rows = await cur.fetchall()
        return [_target_from_row(r) for r in rows]

    async def set_last_check(self, user_id: int, when: float) -> None:
        """Update a target's last-scan timestamp."""
        await self._db.execute(
            "UPDATE ghost_targets SET last_check = ?, updated_at = ? WHERE user_id = ?;",
            (when, time.time(), user_id),
        )
        await self._db.commit()

    async def set_enabled(self, user_id: int, enabled: bool) -> None:
        """Enable or disable periodic scanning for a target."""
        await self._db.execute(
            "UPDATE ghost_targets SET enabled = ?, updated_at = ? WHERE user_id = ?;",
            (1 if enabled else 0, time.time(), user_id),
        )
        await self._db.commit()

    async def update_identity(
        self, user_id: int, username: Optional[str], display_name: Optional[str]
    ) -> None:
        """Refresh the cached username/display-name for nicer listings."""
        await self._db.execute(
            "UPDATE ghost_targets SET username = ?, display_name = ?, updated_at = ? "
            "WHERE user_id = ?;",
            (username, display_name, time.time(), user_id),
        )
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #
    async def get_snapshot(self, user_id: int) -> Optional[dict]:
        """Return the latest stored snapshot for a user, or ``None``."""
        async with self._db.execute(
            "SELECT snapshot_json FROM ghost_snapshots WHERE user_id = ?;", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["snapshot_json"])
        except (TypeError, ValueError):
            return None

    async def save_snapshot(self, user_id: int, snapshot: dict) -> None:
        """Upsert the latest snapshot for a user."""
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        await self._db.execute(
            """
            INSERT INTO ghost_snapshots (user_id, snapshot_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json,
                created_at = excluded.created_at;
            """,
            (user_id, payload, time.time()),
        )
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #
    async def add_history(
        self,
        user_id: int,
        field_name: str,
        old_value: Optional[str],
        new_value: Optional[str],
    ) -> None:
        """Append one detected change to the history log."""
        await self._db.execute(
            "INSERT INTO ghost_history (user_id, field_name, old_value, new_value, detected_at) "
            "VALUES (?, ?, ?, ?, ?);",
            (user_id, field_name, old_value, new_value, time.time()),
        )
        await self._db.commit()

    async def get_history(self, user_id: int) -> list[GhostHistoryEntry]:
        """Return a user's full change history, newest first."""
        async with self._db.execute(
            "SELECT * FROM ghost_history WHERE user_id = ? ORDER BY detected_at DESC, id DESC;",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            GhostHistoryEntry(
                id=r["id"],
                user_id=r["user_id"],
                field_name=r["field_name"],
                old_value=r["old_value"],
                new_value=r["new_value"],
                detected_at=r["detected_at"],
            )
            for r in rows
        ]

    async def count_history(self, user_id: Optional[int] = None) -> int:
        """Return the number of recorded changes (optionally per user)."""
        if user_id is None:
            query, params = "SELECT COUNT(*) AS n FROM ghost_history;", ()
        else:
            query = "SELECT COUNT(*) AS n FROM ghost_history WHERE user_id = ?;"
            params = (user_id,)
        async with self._db.execute(query, params) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def count_targets(self) -> int:
        """Return the number of monitored users."""
        async with self._db.execute("SELECT COUNT(*) AS n FROM ghost_targets;") as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def last_scan(self) -> float:
        """Return the most recent ``last_check`` across all targets (0 if none)."""
        async with self._db.execute(
            "SELECT MAX(last_check) AS m FROM ghost_targets;"
        ) as cur:
            row = await cur.fetchone()
        return float(row["m"]) if row and row["m"] else 0.0


def _target_from_row(row: aiosqlite.Row) -> GhostTarget:
    """Build a :class:`GhostTarget` from a database row."""
    return GhostTarget(
        id=row["id"],
        user_id=row["user_id"],
        username=row["username"],
        display_name=row["display_name"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_check=float(row["last_check"] or 0.0),
    )
