"""Async SQLite persistence for the AI Assistant module.

Uses its own :mod:`aiosqlite` connection to the project's shared database file
(WAL mode makes concurrent connections safe) and manages three dedicated
tables, leaving the banner schema untouched:

    * ``trusted_users``  -- users allowed to use the AI module
    * ``ai_settings``    -- key/value store (persists the selected model)
    * ``ai_statistics``  -- per-user request/success/failure counters
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import aiosqlite

logger = logging.getLogger("banner.ai.database")

#: Idempotent DDL for the AI module's tables.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS trusted_users (
    user_id  INTEGER PRIMARY KEY,
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_statistics (
    user_id       INTEGER PRIMARY KEY,
    request_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_request  REAL
);
"""

#: ai_settings key under which the selected model is persisted.
_SETTING_MODEL = "model"


class AIDatabase:
    """Async wrapper around the AI module's SQLite tables."""

    def __init__(self, path: str) -> None:
        """Initialise the wrapper.

        Args:
            path: Path to the shared SQLite database file.
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """Open the connection and create AI tables if absent."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("AI database ready at %s", self._path)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        """Return the live connection or raise if not connected."""
        if self._conn is None:
            raise RuntimeError("AIDatabase is not connected; call connect() first")
        return self._conn

    async def healthy(self) -> bool:
        """Return ``True`` if a trivial query succeeds, else ``False``."""
        try:
            async with self._db.execute("SELECT 1;") as cur:
                await cur.fetchone()
            return True
        except Exception:  # noqa: BLE001 - health check must not raise
            return False

    # ------------------------------------------------------------------ #
    # Model setting
    # ------------------------------------------------------------------ #
    async def get_model(self, default: str) -> str:
        """Return the persisted model name, or ``default`` if unset."""
        async with self._db.execute(
            "SELECT value FROM ai_settings WHERE key = ?;", (_SETTING_MODEL,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_model(self, model: str) -> None:
        """Persist the selected model name (survives restart)."""
        await self._db.execute(
            "INSERT INTO ai_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (_SETTING_MODEL, model),
        )
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # Trusted users
    # ------------------------------------------------------------------ #
    async def add_trusted(self, user_id: int) -> bool:
        """Add a trusted user.

        Args:
            user_id: Telegram user ID to trust.

        Returns:
            ``True`` if newly added, ``False`` if already trusted.
        """
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO trusted_users (user_id, added_at) VALUES (?, ?);",
            (user_id, time.time()),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def remove_trusted(self, user_id: int) -> bool:
        """Remove a trusted user.

        Returns:
            ``True`` if a row was removed, ``False`` if the user was not trusted.
        """
        cursor = await self._db.execute(
            "DELETE FROM trusted_users WHERE user_id = ?;", (user_id,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def is_trusted(self, user_id: int) -> bool:
        """Return whether ``user_id`` is a trusted user (single indexed lookup)."""
        async with self._db.execute(
            "SELECT 1 FROM trusted_users WHERE user_id = ?;", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def list_trusted(self) -> list[int]:
        """Return all trusted user IDs ordered by when they were added."""
        async with self._db.execute(
            "SELECT user_id FROM trusted_users ORDER BY added_at;"
        ) as cur:
            rows = await cur.fetchall()
        return [int(row["user_id"]) for row in rows]

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #
    async def record_request(self, user_id: int, *, success: bool) -> None:
        """Increment per-user request counters in one upsert.

        Args:
            user_id: The requesting user.
            success: Whether the request ultimately succeeded.
        """
        await self._db.execute(
            """
            INSERT INTO ai_statistics (
                user_id, request_count, success_count, failure_count, last_request
            ) VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                request_count = request_count + 1,
                success_count = success_count + excluded.success_count,
                failure_count = failure_count + excluded.failure_count,
                last_request  = excluded.last_request;
            """,
            (user_id, 1 if success else 0, 0 if success else 1, time.time()),
        )
        await self._db.commit()

    async def total_requests(self) -> int:
        """Return the total number of AI requests recorded across all users."""
        async with self._db.execute(
            "SELECT COALESCE(SUM(request_count), 0) AS n FROM ai_statistics;"
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0
