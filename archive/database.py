"""SQLite persistence for the Archive Watch module.

Two tables:

    * ``archive_chats``    -- tracked chats and their sync watermark.
    * ``archive_messages`` -- archived messages (deduplicated per chat).

Messages are deduplicated on ``(chat_id, message_id)`` so re-syncing never
stores a message twice.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

import aiosqlite

from .models import ArchiveChat, ArchiveMessage

logger = logging.getLogger("sentinel.archive.database")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_chats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL UNIQUE,
    chat_name       TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    sender_id   INTEGER,
    sender_name TEXT,
    date        REAL,
    text        TEXT,
    media_type  TEXT,
    reply_to    INTEGER,
    created_at  REAL NOT NULL,
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_archive_msg_chat ON archive_messages (chat_id);
"""


class ArchiveDatabase:
    """Async persistence for archived chats and messages."""

    def __init__(self, path: str) -> None:
        """Initialise the database.

        Args:
            path: Path to the shared SQLite database file.
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the connection and ensure archive tables exist."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("Archive database ready at %s", self._path)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("ArchiveDatabase is not connected")
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
    # Chats
    # ------------------------------------------------------------------ #
    async def add_chat(self, chat_id: int, chat_name: str) -> ArchiveChat:
        """Insert (or update the name of) a tracked chat."""
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO archive_chats (chat_id, chat_name, enabled, last_message_id, created_at, updated_at)
            VALUES (?, ?, 1, 0, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET chat_name = excluded.chat_name, enabled = 1;
            """,
            (chat_id, chat_name, now, now),
        )
        await self._db.commit()
        chat = await self.get_chat(chat_id)
        assert chat is not None
        return chat

    async def remove_chat(self, chat_id: int) -> bool:
        """Remove a tracked chat and all of its archived messages.

        Returns:
            ``True`` if the chat existed.
        """
        cursor = await self._db.execute(
            "DELETE FROM archive_chats WHERE chat_id = ?;", (chat_id,)
        )
        await self._db.execute(
            "DELETE FROM archive_messages WHERE chat_id = ?;", (chat_id,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_chat(self, chat_id: int) -> Optional[ArchiveChat]:
        """Fetch a tracked chat by id, or ``None``."""
        async with self._db.execute(
            "SELECT * FROM archive_chats WHERE chat_id = ?;", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        return _chat_from_row(row) if row else None

    async def list_chats(self) -> list[ArchiveChat]:
        """Return all tracked chats ordered by creation time."""
        async with self._db.execute(
            "SELECT * FROM archive_chats ORDER BY created_at;"
        ) as cur:
            rows = await cur.fetchall()
        return [_chat_from_row(r) for r in rows]

    async def set_watermark(self, chat_id: int, last_message_id: int) -> None:
        """Update a chat's last-seen message id and ``updated_at``."""
        await self._db.execute(
            "UPDATE archive_chats SET last_message_id = ?, updated_at = ? "
            "WHERE chat_id = ?;",
            (last_message_id, time.time(), chat_id),
        )
        await self._db.commit()

    async def set_enabled(self, chat_id: int, enabled: bool) -> None:
        """Enable or disable periodic sync for a chat."""
        await self._db.execute(
            "UPDATE archive_chats SET enabled = ? WHERE chat_id = ?;",
            (1 if enabled else 0, chat_id),
        )
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #
    async def store_messages(self, messages: Iterable[ArchiveMessage]) -> int:
        """Insert messages, ignoring duplicates; return the number stored.

        Args:
            messages: Messages to persist.

        Returns:
            The count of newly inserted rows.
        """
        now = time.time()
        rows = [
            (
                m.chat_id, m.message_id, m.sender_id, m.sender_name,
                m.date, m.text, m.media_type, m.reply_to, now,
            )
            for m in messages
        ]
        if not rows:
            return 0
        cursor = await self._db.executemany(
            """
            INSERT OR IGNORE INTO archive_messages (
                chat_id, message_id, sender_id, sender_name,
                date, text, media_type, reply_to, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            rows,
        )
        await self._db.commit()
        return cursor.rowcount if cursor.rowcount is not None else 0

    async def fetch_messages(self, chat_id: int) -> list[ArchiveMessage]:
        """Return all archived messages for a chat, oldest first."""
        async with self._db.execute(
            "SELECT * FROM archive_messages WHERE chat_id = ? ORDER BY message_id;",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            ArchiveMessage(
                chat_id=r["chat_id"],
                message_id=r["message_id"],
                sender_id=r["sender_id"],
                sender_name=r["sender_name"],
                date=r["date"],
                text=r["text"],
                media_type=r["media_type"],
                reply_to=r["reply_to"],
            )
            for r in rows
        ]

    async def count_messages(self, chat_id: Optional[int] = None) -> int:
        """Return the number of archived messages (optionally per chat)."""
        if chat_id is None:
            query, params = "SELECT COUNT(*) AS n FROM archive_messages;", ()
        else:
            query = "SELECT COUNT(*) AS n FROM archive_messages WHERE chat_id = ?;"
            params = (chat_id,)
        async with self._db.execute(query, params) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def count_chats(self) -> int:
        """Return the number of tracked chats."""
        async with self._db.execute("SELECT COUNT(*) AS n FROM archive_chats;") as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0


def _chat_from_row(row: aiosqlite.Row) -> ArchiveChat:
    """Build an :class:`ArchiveChat` from a database row."""
    return ArchiveChat(
        id=row["id"],
        chat_id=row["chat_id"],
        chat_name=row["chat_name"] or str(row["chat_id"]),
        enabled=bool(row["enabled"]),
        last_message_id=int(row["last_message_id"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
