"""SQLite persistence for the message logger.

Three tables:

    * ``logger_targets``  -- chats currently being logged.
    * ``logged_messages`` -- the captured messages.
    * ``logger_exports``  -- export history.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite

logger = logging.getLogger("sentinel.logger.database")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logger_targets (
    chat_id  INTEGER PRIMARY KEY,
    title    TEXT,
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS logged_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sender_id  INTEGER,
    username   TEXT,
    date       REAL NOT NULL,
    text       TEXT,
    media_type TEXT
);

CREATE INDEX IF NOT EXISTS idx_logged_chat ON logged_messages (chat_id);

CREATE TABLE IF NOT EXISTS logger_exports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER,
    format     TEXT NOT NULL,
    created_at REAL NOT NULL,
    count      INTEGER NOT NULL
);
"""


@dataclass(slots=True)
class LoggedMessage:
    """A single logged message row."""

    chat_id: int
    message_id: int
    sender_id: Optional[int]
    username: Optional[str]
    date: float
    text: Optional[str]
    media_type: Optional[str]


class LoggerDatabase:
    """Async persistence for the message logger."""

    def __init__(self, path: str) -> None:
        """Initialise the database.

        Args:
            path: Path to the shared SQLite database file.
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the connection and ensure logger tables exist."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("Logger database ready at %s", self._path)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("LoggerDatabase is not connected")
        return self._conn

    # ------------------------------------------------------------------ #
    # Targets
    # ------------------------------------------------------------------ #
    async def add_target(self, chat_id: int, title: str) -> None:
        """Add (or update) a logged chat target."""
        await self._db.execute(
            "INSERT INTO logger_targets (chat_id, title, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title;",
            (chat_id, title, time.time()),
        )
        await self._db.commit()

    async def remove_target(self, chat_id: int) -> bool:
        """Remove a logged target. Returns ``True`` if one existed."""
        cursor = await self._db.execute(
            "DELETE FROM logger_targets WHERE chat_id = ?;", (chat_id,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def list_targets(self) -> list[tuple[int, str]]:
        """Return ``(chat_id, title)`` for all targets."""
        async with self._db.execute(
            "SELECT chat_id, title FROM logger_targets ORDER BY added_at;"
        ) as cur:
            rows = await cur.fetchall()
        return [(int(r["chat_id"]), r["title"] or str(r["chat_id"])) for r in rows]

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #
    async def log_message(self, message: LoggedMessage) -> None:
        """Persist a logged message."""
        await self._db.execute(
            """
            INSERT INTO logged_messages (
                chat_id, message_id, sender_id, username, date, text, media_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                message.chat_id,
                message.message_id,
                message.sender_id,
                message.username,
                message.date,
                message.text,
                message.media_type,
            ),
        )
        await self._db.commit()

    async def fetch_messages(self, chat_id: int) -> list[LoggedMessage]:
        """Return all logged messages for a chat, oldest first."""
        async with self._db.execute(
            "SELECT * FROM logged_messages WHERE chat_id = ? ORDER BY date, id;",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            LoggedMessage(
                chat_id=r["chat_id"],
                message_id=r["message_id"],
                sender_id=r["sender_id"],
                username=r["username"],
                date=r["date"],
                text=r["text"],
                media_type=r["media_type"],
            )
            for r in rows
        ]

    async def count_messages(self, chat_id: Optional[int] = None) -> int:
        """Return the number of logged messages (optionally per chat)."""
        if chat_id is None:
            query, params = "SELECT COUNT(*) AS n FROM logged_messages;", ()
        else:
            query = "SELECT COUNT(*) AS n FROM logged_messages WHERE chat_id = ?;"
            params = (chat_id,)
        async with self._db.execute(query, params) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ #
    # Exports
    # ------------------------------------------------------------------ #
    async def record_export(self, chat_id: Optional[int], fmt: str, count: int) -> None:
        """Record an export in the history table."""
        await self._db.execute(
            "INSERT INTO logger_exports (chat_id, format, created_at, count) "
            "VALUES (?, ?, ?, ?);",
            (chat_id, fmt, time.time(), count),
        )
        await self._db.commit()
