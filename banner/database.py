"""Async SQLite persistence layer for the Banner system.

Wraps :mod:`aiosqlite` to provide fully non-blocking access to banner records
and global statistics. Tables are created automatically on
:meth:`Database.connect`. All timestamps are POSIX seconds.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

import aiosqlite

from .models import Banner, BannerStatus

logger = logging.getLogger("banner.database")

#: DDL executed on startup. Idempotent (``IF NOT EXISTS``).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS banners (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    interval          INTEGER NOT NULL,
    mode              TEXT    NOT NULL DEFAULT 'forward',
    status            TEXT    NOT NULL DEFAULT 'active',
    created_at        REAL    NOT NULL,
    last_run          REAL,
    next_run          REAL,
    sent_count        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_banners_status ON banners (status);

CREATE TABLE IF NOT EXISTS stats (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""

#: Global statistics counter keys.
STAT_TOTAL_SENT = "total_sent"
STAT_TOTAL_CREATED = "total_created"


class Database:
    """Async wrapper around an SQLite database file.

    Example:
        >>> db = Database("banners.db")
        >>> await db.connect()
        >>> banner = await db.create_banner(banner)
        >>> await db.close()
    """

    def __init__(self, path: str) -> None:
        """Initialise the database wrapper.

        Args:
            path: Filesystem path to the SQLite database file.
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """Open the connection and ensure the schema and stat rows exist."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(_SCHEMA)
        # Seed counters so UPDATE statements always have a row to touch.
        for key in (STAT_TOTAL_SENT, STAT_TOTAL_CREATED):
            await self._conn.execute(
                "INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0);", (key,)
            )
        await self._conn.commit()
        logger.info("Database connected at %s", self._path)

    async def close(self) -> None:
        """Close the underlying connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed")

    @property
    def _db(self) -> aiosqlite.Connection:
        """Return the live connection or raise if not connected."""
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first")
        return self._conn

    async def healthy(self) -> bool:
        """Return ``True`` if a trivial query succeeds, else ``False``.

        Never raises; used by the status command to report DB health.
        """
        try:
            async with self._db.execute("SELECT 1;") as cur:
                await cur.fetchone()
            return True
        except Exception:  # noqa: BLE001 - health check must not propagate
            logger.exception("Database health check failed")
            return False

    # ------------------------------------------------------------------ #
    # Banner CRUD
    # ------------------------------------------------------------------ #
    async def create_banner(self, banner: Banner) -> Banner:
        """Insert a new banner and return it with its assigned ``id``.

        Also increments the global ``total_created`` counter.

        Args:
            banner: The banner to persist (its ``id`` is ignored).

        Returns:
            The same banner instance with ``id`` populated.
        """
        cursor = await self._db.execute(
            """
            INSERT INTO banners (
                chat_id, source_message_id, interval, mode, status,
                created_at, last_run, next_run, sent_count
            ) VALUES (
                :chat_id, :source_message_id, :interval, :mode, :status,
                :created_at, :last_run, :next_run, :sent_count
            );
            """,
            banner.to_row(),
        )
        await self._db.execute(
            "UPDATE stats SET value = value + 1 WHERE key = ?;",
            (STAT_TOTAL_CREATED,),
        )
        await self._db.commit()
        banner.id = cursor.lastrowid
        return banner

    async def get_banner(self, banner_id: int) -> Optional[Banner]:
        """Fetch a single banner by ID, or ``None`` if it does not exist."""
        async with self._db.execute(
            "SELECT * FROM banners WHERE id = ?;", (banner_id,)
        ) as cur:
            row = await cur.fetchone()
        return Banner.from_row(row) if row else None

    async def list_banners(
        self, *, statuses: Optional[Iterable[BannerStatus]] = None
    ) -> list[Banner]:
        """List banners, optionally filtered by status.

        Args:
            statuses: If given, only banners whose status is in this iterable
                are returned. Otherwise all banners are returned.

        Returns:
            Banners ordered by ascending ID.
        """
        if statuses is None:
            query = "SELECT * FROM banners ORDER BY id;"
            params: tuple[object, ...] = ()
        else:
            values = [s.value for s in statuses]
            placeholders = ",".join("?" for _ in values)
            query = (
                f"SELECT * FROM banners WHERE status IN ({placeholders}) "
                "ORDER BY id;"
            )
            params = tuple(values)

        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [Banner.from_row(row) for row in rows]

    async def update_banner(self, banner: Banner) -> None:
        """Persist all mutable fields of an existing banner.

        Args:
            banner: A banner with a valid ``id``.

        Raises:
            ValueError: If the banner has no ``id``.
        """
        if banner.id is None:
            raise ValueError("Cannot update a banner without an id")
        await self._db.execute(
            """
            UPDATE banners SET
                chat_id           = :chat_id,
                source_message_id = :source_message_id,
                interval          = :interval,
                mode              = :mode,
                status            = :status,
                created_at        = :created_at,
                last_run          = :last_run,
                next_run          = :next_run,
                sent_count        = :sent_count
            WHERE id = :id;
            """,
            banner.to_row(),
        )
        await self._db.commit()

    async def delete_banner(self, banner_id: int) -> bool:
        """Permanently (hard) delete a banner row by ID.

        Global statistics in the ``stats`` table are intentionally left
        untouched so historical send counts survive deletion.

        Args:
            banner_id: The banner to delete.

        Returns:
            ``True`` if a row was removed, ``False`` if no such banner existed.
        """
        cursor = await self._db.execute(
            "DELETE FROM banners WHERE id = ?;", (banner_id,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete_all_banners(self) -> int:
        """Hard-delete every banner row in a single transaction.

        Global statistics (``total_sent``, ``total_created``) are preserved.

        Returns:
            The number of banner rows deleted.
        """
        cursor = await self._db.execute("DELETE FROM banners;")
        await self._db.commit()
        return cursor.rowcount if cursor.rowcount is not None else 0

    async def record_send(self, banner: Banner) -> None:
        """Atomically persist a successful send for ``banner``.

        Updates the banner's ``last_run``, ``next_run`` and ``sent_count`` and
        increments the global ``total_sent`` counter in a single transaction.

        Args:
            banner: The banner whose in-memory counters have already been
                advanced by the scheduler.
        """
        await self._db.execute(
            """
            UPDATE banners SET
                last_run   = :last_run,
                next_run   = :next_run,
                sent_count = :sent_count
            WHERE id = :id;
            """,
            {
                "id": banner.id,
                "last_run": banner.last_run,
                "next_run": banner.next_run,
                "sent_count": banner.sent_count,
            },
        )
        await self._db.execute(
            "UPDATE stats SET value = value + 1 WHERE key = ?;",
            (STAT_TOTAL_SENT,),
        )
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # Statistics / aggregate queries
    # ------------------------------------------------------------------ #
    async def get_stat(self, key: str) -> int:
        """Return the value of a global statistic counter (0 if absent)."""
        async with self._db.execute(
            "SELECT value FROM stats WHERE key = ?;", (key,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["value"]) if row else 0

    async def status_counts(self) -> dict[str, int]:
        """Return aggregate counts for the status command in one query set.

        Returns:
            A mapping with keys ``active``, ``paused``, ``stopped``,
            ``total`` and ``total_sent``. Computed with grouped/aggregate
            SQL rather than by iterating banners in Python.
        """
        counts = {
            BannerStatus.ACTIVE.value: 0,
            BannerStatus.PAUSED.value: 0,
            BannerStatus.STOPPED.value: 0,
        }
        async with self._db.execute(
            "SELECT status, COUNT(*) AS n FROM banners GROUP BY status;"
        ) as cur:
            async for row in cur:
                counts[row["status"]] = int(row["n"])

        total = sum(counts.values())
        total_sent = await self.get_stat(STAT_TOTAL_SENT)
        return {
            "active": counts[BannerStatus.ACTIVE.value],
            "paused": counts[BannerStatus.PAUSED.value],
            "stopped": counts[BannerStatus.STOPPED.value],
            "total": total,
            "total_sent": total_sent,
        }
