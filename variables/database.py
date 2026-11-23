"""SQLite persistence for the Quick Variables module.

A single table, ``variables``, stores keyword → value aliases. The keyword is
unique; values are kept verbatim (multiline supported). The table is created
idempotently with ``CREATE TABLE IF NOT EXISTS`` so existing databases upgrade
without any data loss (additive schema v2).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import aiosqlite

from .models import Variable

logger = logging.getLogger("sentinel.variables.database")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS variables (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword    TEXT NOT NULL UNIQUE,
    value      TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_variables_keyword ON variables (keyword);
"""


class VariablesDatabase:
    """Async persistence for text aliases."""

    def __init__(self, path: str) -> None:
        """Initialise the database.

        Args:
            path: Path to the shared SQLite database file.
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the connection and ensure the ``variables`` table exists."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("Variables database ready at %s", self._path)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("VariablesDatabase is not connected")
        return self._conn

    async def healthy(self) -> bool:
        """Return ``True`` if the database responds (never raises)."""
        try:
            async with self._db.execute("SELECT 1;") as cur:
                await cur.fetchone()
            return True
        except Exception:  # noqa: BLE001 - health check must not raise
            return False

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def get(self, keyword: str) -> Optional[Variable]:
        """Fetch a variable by keyword, or ``None``."""
        async with self._db.execute(
            "SELECT * FROM variables WHERE keyword = ?;", (keyword,)
        ) as cur:
            row = await cur.fetchone()
        return _from_row(row) if row else None

    async def list_keywords(self) -> list[str]:
        """Return every keyword, alphabetically (values not loaded)."""
        async with self._db.execute(
            "SELECT keyword FROM variables ORDER BY keyword;"
        ) as cur:
            rows = await cur.fetchall()
        return [row["keyword"] for row in rows]

    async def count(self) -> int:
        """Return the number of stored variables."""
        async with self._db.execute("SELECT COUNT(*) AS n FROM variables;") as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def add(self, keyword: str, value: str) -> bool:
        """Insert a new variable.

        Args:
            keyword: Unique, normalised keyword.
            value: The text value (stored verbatim).

        Returns:
            ``True`` if inserted, ``False`` if the keyword already exists.
        """
        now = time.time()
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO variables (keyword, value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?);",
            (keyword, value, now, now),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def edit(self, keyword: str, value: str) -> bool:
        """Update an existing variable's value.

        Returns:
            ``True`` if a row was updated, ``False`` if the keyword is unknown.
        """
        cursor = await self._db.execute(
            "UPDATE variables SET value = ?, updated_at = ? WHERE keyword = ?;",
            (value, time.time(), keyword),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def remove(self, keyword: str) -> bool:
        """Delete a variable.

        Returns:
            ``True`` if a row was deleted.
        """
        cursor = await self._db.execute(
            "DELETE FROM variables WHERE keyword = ?;", (keyword,)
        )
        await self._db.commit()
        return cursor.rowcount > 0


def _from_row(row: aiosqlite.Row) -> Variable:
    """Build a :class:`Variable` from a database row."""
    return Variable(
        id=row["id"],
        keyword=row["keyword"],
        value=row["value"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
