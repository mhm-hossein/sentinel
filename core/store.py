"""Shared key/value store for feature toggles and global counters.

A single :class:`SharedStore` instance (one aiosqlite connection to the
Sentinel database) is created at startup and injected into the feature modules
that need persistent settings or statistics counters. This keeps a single,
consistent home for cross-module state without coupling modules together.

Two tables are managed:

    * ``sentinel_settings`` -- string key/value pairs (feature on/off toggles,
      logger targets serialised by their owners, etc.).
    * ``sentinel_counters`` -- integer counters incremented atomically
      (saved media, captured deletions, edits tracked, …).
"""

from __future__ import annotations

import logging
from typing import Optional

import aiosqlite

from .migrations import ensure_schema_version

logger = logging.getLogger("sentinel.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sentinel_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sentinel_counters (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""

#: Canonical counter keys used across modules.
COUNTER_SAVED_MEDIA = "saved_media"
COUNTER_DELETED_CAPTURED = "deleted_captured"
COUNTER_EDITS_TRACKED = "edits_tracked"
COUNTER_MESSAGES_LOGGED = "messages_logged"
COUNTER_EXPORTS = "exports_generated"
COUNTER_BACKUPS = "backups_created"
COUNTER_MONITOR_REQUESTS = "monitor_requests"
COUNTER_ARCHIVE_SYNCS = "archive_sync_runs"
COUNTER_SEARCH_QUERIES = "search_queries"
COUNTER_SEARCH_EXPORTS = "search_exports"
COUNTER_AI_FALLBACKS = "ai_fallback_activations"
COUNTER_SAVED_MESSAGES = "saved_messages"
COUNTER_INFO_REQUESTS = "info_requests"
COUNTER_HUNTER_CHECKS = "hunter_checks"
COUNTER_HUNTER_CLAIMS = "hunter_claims"
COUNTER_HUNTER_SUCCESSES = "hunter_successes"
COUNTER_HUNTER_FAILURES = "hunter_failures"


class SharedStore:
    """Async key/value + counter store backed by the Sentinel database."""

    def __init__(self, path: str) -> None:
        """Initialise the store.

        Args:
            path: Path to the shared SQLite database file.
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the connection, create tables and record the schema version."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        await ensure_schema_version(self._conn)
        logger.info("Shared store ready at %s", self._path)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        """Return the live connection or raise if not connected."""
        if self._conn is None:
            raise RuntimeError("SharedStore is not connected; call connect() first")
        return self._conn

    async def healthy(self) -> bool:
        """Return ``True`` if the database responds, else ``False`` (no raise)."""
        try:
            async with self._db.execute("SELECT 1;") as cur:
                await cur.fetchone()
            return True
        except Exception:  # noqa: BLE001 - health check must not raise
            return False

    # ------------------------------------------------------------------ #
    # Settings (string / bool)
    # ------------------------------------------------------------------ #
    async def get_setting(self, key: str, default: str = "") -> str:
        """Return a string setting, or ``default`` if unset."""
        async with self._db.execute(
            "SELECT value FROM sentinel_settings WHERE key = ?;", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        """Persist a string setting."""
        await self._db.execute(
            "INSERT INTO sentinel_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (key, value),
        )
        await self._db.commit()

    async def get_bool(self, key: str, default: bool = False) -> bool:
        """Return a boolean setting, or ``default`` if unset."""
        raw = await self.get_setting(key, "")
        if raw == "":
            return default
        return raw == "1"

    async def set_bool(self, key: str, value: bool) -> None:
        """Persist a boolean setting as ``"1"``/``"0"``."""
        await self.set_setting(key, "1" if value else "0")

    # ------------------------------------------------------------------ #
    # Counters
    # ------------------------------------------------------------------ #
    async def incr(self, key: str, amount: int = 1) -> None:
        """Atomically increment a counter, creating it if absent."""
        await self._db.execute(
            "INSERT INTO sentinel_counters (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value;",
            (key, amount),
        )
        await self._db.commit()

    async def get_counter(self, key: str) -> int:
        """Return a counter's value (0 if absent)."""
        async with self._db.execute(
            "SELECT value FROM sentinel_counters WHERE key = ?;", (key,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["value"]) if row else 0

    async def all_counters(self) -> dict[str, int]:
        """Return every counter as a mapping in one query."""
        async with self._db.execute(
            "SELECT key, value FROM sentinel_counters;"
        ) as cur:
            rows = await cur.fetchall()
        return {row["key"]: int(row["value"]) for row in rows}
