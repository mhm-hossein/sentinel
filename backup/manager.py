"""Backup creation, listing and restore.

Archives are built off the event loop (``asyncio.to_thread``) to avoid blocking
the scheduler. History is persisted; the global ``backups_created`` counter is
incremented on each successful backup.
"""

from __future__ import annotations

import logging
import os
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from core.store import COUNTER_BACKUPS, SharedStore

logger = logging.getLogger("backup.manager")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at REAL NOT NULL,
    size       INTEGER NOT NULL
);
"""

#: Directory names excluded from backups.
_EXCLUDE_DIRS = {"venv", ".venv", "__pycache__", ".git", "backups", "node_modules"}


@dataclass(slots=True)
class BackupConfig:
    """Configuration for the backup module.

    Attributes:
        backup_path: Directory where archives are written.
        db_path: Path to the Sentinel database (included in backups).
        session_name: Telethon session name (``<name>.session`` is included).
        extra_paths: Additional files/directories to include if present.
    """

    backup_path: str = "backups"
    db_path: str = "sentinel.db"
    session_name: str = "banner"
    extra_paths: list[str] = field(
        default_factory=lambda: [".env", "banner/config.py", os.path.join("data", "exports")]
    )

    @classmethod
    def from_env(cls, db_path: str) -> "BackupConfig":
        """Build configuration from environment variables.

        Args:
            db_path: The resolved Sentinel database path.
        """
        return cls(
            backup_path=os.environ.get("BACKUP_PATH", "backups"),
            db_path=db_path,
            session_name=os.environ.get("BANNER_SESSION", "banner"),
        )


@dataclass(slots=True)
class BackupRecord:
    """A backup history entry."""

    id: int
    filename: str
    path: str
    created_at: float
    size: int


class BackupManager:
    """Creates, lists and restores Sentinel backups."""

    def __init__(self, store: SharedStore, config: BackupConfig) -> None:
        """Initialise the manager.

        Args:
            store: Shared store (statistics counter + history connection).
            config: Backup configuration.
        """
        self._store = store
        self._config = config
        self._conn: Optional[aiosqlite.Connection] = None

    async def start(self) -> None:
        """Open the history connection and ensure the backups directory exists."""
        os.makedirs(self._config.backup_path, exist_ok=True)
        self._conn = await aiosqlite.connect(self._config.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("Backup manager ready (path=%s)", self._config.backup_path)

    async def close(self) -> None:
        """Close the history connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("BackupManager is not started")
        return self._conn

    # ------------------------------------------------------------------ #
    # Create
    # ------------------------------------------------------------------ #
    async def create(self) -> BackupRecord:
        """Create a new backup archive and record it.

        Returns:
            The created :class:`BackupRecord`.

        Raises:
            RuntimeError: If the archive could not be created.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"sentinel_backup_{stamp}.zip"
        path = os.path.join(self._config.backup_path, filename)

        members = self._collect_members()
        import asyncio

        size = await asyncio.to_thread(self._build_zip, path, members)
        if size <= 0:
            raise RuntimeError("Backup archive is empty")

        cursor = await self._db.execute(
            "INSERT INTO backups (filename, path, created_at, size) VALUES (?, ?, ?, ?);",
            (filename, path, time.time(), size),
        )
        await self._db.commit()
        await self._store.incr(COUNTER_BACKUPS)
        logger.info("Backup created: %s (%d bytes)", filename, size)
        return BackupRecord(
            id=int(cursor.lastrowid),
            filename=filename,
            path=path,
            created_at=time.time(),
            size=size,
        )

    def _collect_members(self) -> list[tuple[str, str]]:
        """Collect ``(absolute_path, archive_name)`` pairs to include.

        Includes the database (and WAL/SHM), session files, and configured
        extra paths (configs, exports, logs). Excludes virtualenvs, caches and
        prior backups.

        Returns:
            A list of file path / archive-name pairs.
        """
        members: list[tuple[str, str]] = []

        def add_file(file_path: str) -> None:
            if os.path.isfile(file_path):
                members.append((file_path, os.path.relpath(file_path)))

        def add_dir(dir_path: str) -> None:
            for root, dirs, files in os.walk(dir_path):
                dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
                for name in files:
                    add_file(os.path.join(root, name))

        # Database + sidecars.
        for suffix in ("", "-wal", "-shm"):
            add_file(self._config.db_path + suffix)

        # Session files in the current directory.
        for entry in os.listdir("."):
            if entry.endswith(".session"):
                add_file(entry)

        # Configured extras (files or directories).
        for extra in self._config.extra_paths:
            if os.path.isdir(extra):
                add_dir(extra)
            else:
                add_file(extra)

        # De-duplicate while preserving order.
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for abs_path, arc in members:
            if abs_path not in seen:
                seen.add(abs_path)
                unique.append((abs_path, arc))
        return unique

    @staticmethod
    def _build_zip(path: str, members: list[tuple[str, str]]) -> int:
        """Build the ZIP archive and return its size in bytes."""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for abs_path, arc in members:
                try:
                    zf.write(abs_path, arc)
                except OSError:
                    logger.debug("Skipping unreadable file %s", abs_path)
        return os.path.getsize(path) if os.path.exists(path) else 0

    # ------------------------------------------------------------------ #
    # List / restore
    # ------------------------------------------------------------------ #
    async def list_backups(self) -> list[BackupRecord]:
        """Return the backup history, newest first."""
        async with self._db.execute(
            "SELECT * FROM backups ORDER BY created_at DESC;"
        ) as cur:
            rows = await cur.fetchall()
        return [
            BackupRecord(
                id=int(r["id"]),
                filename=r["filename"],
                path=r["path"],
                created_at=r["created_at"],
                size=int(r["size"]),
            )
            for r in rows
        ]

    async def restore(self, identifier: str) -> str:
        """Extract a backup to a fresh, timestamped directory (non-destructive).

        Args:
            identifier: A backup id (number) or filename.

        Returns:
            The directory the archive was extracted to.

        Raises:
            FileNotFoundError: If the backup cannot be located.
        """
        record = await self._find(identifier)
        if record is None or not os.path.exists(record.path):
            raise FileNotFoundError("Backup not found")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        target = os.path.join(self._config.backup_path, f"restored_{stamp}")
        import asyncio

        await asyncio.to_thread(self._extract_zip, record.path, target)
        logger.info("Backup %s restored to %s", record.filename, target)
        return target

    async def _find(self, identifier: str) -> Optional[BackupRecord]:
        """Find a backup by numeric id or filename."""
        records = await self.list_backups()
        if identifier.isdigit():
            wanted = int(identifier)
            return next((r for r in records if r.id == wanted), None)
        return next((r for r in records if r.filename == identifier), None)

    @staticmethod
    def _extract_zip(path: str, target: str) -> None:
        """Extract ``path`` into ``target`` (created if missing).

        Every member is sanitized first (ZipSlip guard): absolute paths and
        ``..`` escapes are refused, so a tampered archive can never write
        outside ``target``.
        """
        os.makedirs(target, exist_ok=True)
        safe_root = os.path.realpath(target)
        with zipfile.ZipFile(path, "r") as zf:
            for member in zf.infolist():
                name = member.filename
                if not name or os.path.isabs(name) or ".." in name.split("/"):
                    logger.warning("Refusing unsafe zip member: %r", name)
                    continue
                dest = os.path.realpath(os.path.join(safe_root, name))
                if dest != safe_root and not dest.startswith(safe_root + os.sep):
                    logger.warning("Refusing escaping zip member: %r", name)
                    continue
                zf.extract(member, safe_root)
