"""SQLite persistence for the Username Hunter module.

Two tables, created idempotently in the shared ``sentinel.db``:

    * ``hunter_targets`` -- hunted usernames and their per-target check cadence.
    * ``hunter_events``  -- append-only log of checks, detections and claim
      outcomes.

The id of the reusable *reserve* entity is stored in the shared key/value store
(``SharedStore``) rather than a dedicated table, keyed by claim-target type.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import aiosqlite

from .models import HunterEvent, HunterTarget

logger = logging.getLogger("sentinel.hunter.database")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hunter_targets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE,
    enabled        INTEGER NOT NULL DEFAULT 1,
    check_interval INTEGER NOT NULL DEFAULT 60,
    last_check     REAL NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hunter_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details    TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hunter_events_username ON hunter_events (username);
CREATE INDEX IF NOT EXISTS idx_hunter_events_type ON hunter_events (event_type);
CREATE INDEX IF NOT EXISTS idx_hunter_targets_enabled ON hunter_targets (enabled);
"""


class HunterDatabase:
    """Async persistence for hunted usernames and their events."""

    def __init__(self, path: str) -> None:
        """Initialise the database.

        Args:
            path: Path to the shared SQLite database file.
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the connection and ensure Hunter tables exist."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("Hunter database ready at %s", self._path)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("HunterDatabase is not connected")
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
    async def add_target(self, username: str, check_interval: int) -> HunterTarget:
        """Insert (or re-enable) a hunted username."""
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO hunter_targets
                (username, enabled, check_interval, last_check, created_at, updated_at)
            VALUES (?, 1, ?, 0, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                enabled = 1,
                check_interval = excluded.check_interval,
                updated_at = excluded.updated_at;
            """,
            (username, check_interval, now, now),
        )
        await self._db.commit()
        target = await self.get_target(username)
        assert target is not None
        return target

    async def remove_target(self, username: str) -> bool:
        """Remove a hunted username (its events are retained for the log).

        Returns:
            ``True`` if the target existed.
        """
        cursor = await self._db.execute(
            "DELETE FROM hunter_targets WHERE username = ?;", (username,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_target(self, username: str) -> Optional[HunterTarget]:
        """Fetch a hunted username, or ``None``."""
        async with self._db.execute(
            "SELECT * FROM hunter_targets WHERE username = ?;", (username,)
        ) as cur:
            row = await cur.fetchone()
        return _target_from_row(row) if row else None

    async def list_targets(self, *, enabled_only: bool = False) -> list[HunterTarget]:
        """Return hunted usernames ordered by creation time."""
        query = "SELECT * FROM hunter_targets"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at;"
        async with self._db.execute(query) as cur:
            rows = await cur.fetchall()
        return [_target_from_row(r) for r in rows]

    async def set_last_check(self, username: str, when: float) -> None:
        """Update a target's last-check timestamp."""
        await self._db.execute(
            "UPDATE hunter_targets SET last_check = ?, updated_at = ? WHERE username = ?;",
            (when, time.time(), username),
        )
        await self._db.commit()

    async def set_enabled(self, username: str, enabled: bool) -> None:
        """Enable or disable periodic checking for a target."""
        await self._db.execute(
            "UPDATE hunter_targets SET enabled = ?, updated_at = ? WHERE username = ?;",
            (1 if enabled else 0, time.time(), username),
        )
        await self._db.commit()

    async def count_targets(self) -> int:
        """Return the number of hunted usernames."""
        async with self._db.execute("SELECT COUNT(*) AS n FROM hunter_targets;") as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def last_check(self) -> float:
        """Return the most recent ``last_check`` across targets (0 if none)."""
        async with self._db.execute(
            "SELECT MAX(last_check) AS m FROM hunter_targets;"
        ) as cur:
            row = await cur.fetchone()
        return float(row["m"]) if row and row["m"] else 0.0

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    async def add_event(
        self, username: str, event_type: str, details: Optional[str]
    ) -> None:
        """Append a Hunter event to the log."""
        await self._db.execute(
            "INSERT INTO hunter_events (username, event_type, details, created_at) "
            "VALUES (?, ?, ?, ?);",
            (username, event_type, details, time.time()),
        )
        await self._db.commit()

    async def get_events(
        self, username: Optional[str] = None
    ) -> list[HunterEvent]:
        """Return events (optionally for one username), newest first."""
        if username is None:
            query, params = (
                "SELECT * FROM hunter_events ORDER BY created_at DESC, id DESC;",
                (),
            )
        else:
            query = (
                "SELECT * FROM hunter_events WHERE username = ? "
                "ORDER BY created_at DESC, id DESC;"
            )
            params = (username,)
        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [
            HunterEvent(
                id=r["id"],
                username=r["username"],
                event_type=r["event_type"],
                details=r["details"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def count_events(self, event_type: Optional[str] = None) -> int:
        """Return the number of events (optionally of a given type)."""
        if event_type is None:
            query, params = "SELECT COUNT(*) AS n FROM hunter_events;", ()
        else:
            query = "SELECT COUNT(*) AS n FROM hunter_events WHERE event_type = ?;"
            params = (event_type,)
        async with self._db.execute(query, params) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0


def _target_from_row(row: aiosqlite.Row) -> HunterTarget:
    """Build a :class:`HunterTarget` from a database row."""
    return HunterTarget(
        id=row["id"],
        username=row["username"],
        enabled=bool(row["enabled"]),
        check_interval=int(row["check_interval"] or 60),
        last_check=float(row["last_check"] or 0.0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
