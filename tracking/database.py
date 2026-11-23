"""SQLite cache for the tracking module.

A single ``tracked_messages`` table holds the most recently seen text/media of
observed messages so that anti-delete and edit-tracking can reconstruct content
after the original is gone. Rows are pruned by age to stay memory/disk safe.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite

logger = logging.getLogger("tracking.database")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_messages (
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    sender_id    INTEGER,
    username     TEXT,
    chat_title   TEXT,
    text         TEXT,
    message_type TEXT,
    media_path   TEXT,
    date         REAL NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_tracked_date ON tracked_messages (date);
"""


@dataclass(slots=True)
class CachedMessage:
    """A cached snapshot of an observed message."""

    chat_id: int
    message_id: int
    sender_id: Optional[int]
    username: Optional[str]
    chat_title: Optional[str]
    text: Optional[str]
    message_type: Optional[str]
    media_path: Optional[str]
    date: float


class TrackingDatabase:
    """Async persistence for the tracking message cache."""

    def __init__(self, path: str) -> None:
        """Initialise the cache database.

        Args:
            path: Path to the shared SQLite database file.
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the connection and ensure the cache table exists."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("Tracking cache ready at %s", self._path)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("TrackingDatabase is not connected")
        return self._conn

    async def upsert(self, message: CachedMessage) -> None:
        """Insert or update a cached message snapshot."""
        await self._db.execute(
            """
            INSERT INTO tracked_messages (
                chat_id, message_id, sender_id, username, chat_title,
                text, message_type, media_path, date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                sender_id    = excluded.sender_id,
                username     = excluded.username,
                chat_title   = excluded.chat_title,
                text         = excluded.text,
                message_type = excluded.message_type,
                media_path   = COALESCE(excluded.media_path, tracked_messages.media_path),
                date         = excluded.date;
            """,
            (
                message.chat_id,
                message.message_id,
                message.sender_id,
                message.username,
                message.chat_title,
                message.text,
                message.message_type,
                message.media_path,
                message.date,
            ),
        )
        await self._db.commit()

    async def get(self, chat_id: int, message_id: int) -> Optional[CachedMessage]:
        """Fetch a cached message, or ``None`` if absent."""
        async with self._db.execute(
            "SELECT * FROM tracked_messages WHERE chat_id = ? AND message_id = ?;",
            (chat_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        return _from_row(row) if row else None

    async def delete(self, chat_id: int, message_id: int) -> None:
        """Remove a cached message snapshot."""
        await self._db.execute(
            "DELETE FROM tracked_messages WHERE chat_id = ? AND message_id = ?;",
            (chat_id, message_id),
        )
        await self._db.commit()

    async def prune_older_than(self, max_age_seconds: float) -> list[str]:
        """Delete cache rows older than ``max_age_seconds``.

        Args:
            max_age_seconds: Maximum row age to keep.

        Returns:
            The media paths of pruned rows (so callers can delete files).
        """
        cutoff = time.time() - max_age_seconds
        async with self._db.execute(
            "SELECT media_path FROM tracked_messages "
            "WHERE date < ? AND media_path IS NOT NULL;",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        await self._db.execute(
            "DELETE FROM tracked_messages WHERE date < ?;", (cutoff,)
        )
        await self._db.commit()
        return [row["media_path"] for row in rows]


def _from_row(row: aiosqlite.Row) -> CachedMessage:
    """Build a :class:`CachedMessage` from a database row."""
    return CachedMessage(
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        sender_id=row["sender_id"],
        username=row["username"],
        chat_title=row["chat_title"],
        text=row["text"],
        message_type=row["message_type"],
        media_path=row["media_path"],
        date=row["date"],
    )
