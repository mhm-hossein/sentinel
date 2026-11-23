"""Database path resolution, legacy migration and schema versioning.

Sentinel stores everything in a single SQLite file (``sentinel.db`` by
default). This module:

    * resolves the active database path from the environment, preferring the
      new ``SENTINEL_DB_PATH`` while remaining backward compatible with the old
      ``BANNER_DB_PATH``/``DB_PATH`` variables;
    * performs a one-time, non-destructive file-level migration from a legacy
      ``banner.db`` / ``banners.db`` to ``sentinel.db`` so existing users keep
      their data; and
    * maintains a ``sentinel_meta`` table recording the schema version.

The file migration must run *before* any connection opens the (possibly
not-yet-existing) target file, so it is invoked early in startup.
"""

from __future__ import annotations

import logging
import os
import shutil

import aiosqlite

logger = logging.getLogger("sentinel.migrations")

#: Default database filename for a fresh install.
DEFAULT_DB_NAME = "sentinel.db"

#: Legacy database filenames, in priority order, migrated to the new name.
LEGACY_DB_NAMES: tuple[str, ...] = ("banners.db", "banner.db")

#: SQLite sidecar suffixes copied alongside the main file (WAL mode).
_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm")

#: Current schema version recorded in ``sentinel_meta``.
#:
#: History (all upgrades are additive — no existing data is ever destroyed):
#:   * v1 — baseline (banner/AI/tracking/logger/archive tables).
#:   * v2 — adds the ``variables`` table for the Quick Variables module.
#:   * v3 — adds Ghost Tracker (``ghost_targets``/``ghost_snapshots``/
#:          ``ghost_history``) and Username Hunter (``hunter_targets``/
#:          ``hunter_events``) tables. All created idempotently by their modules.
SCHEMA_VERSION = 3


def resolve_db_path() -> str:
    """Resolve the active database path from the environment.

    Resolution order: ``SENTINEL_DB_PATH`` -> ``BANNER_DB_PATH`` -> ``DB_PATH``
    -> :data:`DEFAULT_DB_NAME`. The first non-empty value wins, preserving
    backward compatibility with pre-Sentinel configurations.

    Returns:
        The database file path.
    """
    for var in ("SENTINEL_DB_PATH", "BANNER_DB_PATH", "DB_PATH"):
        value = os.environ.get(var)
        if value:
            return value
    return DEFAULT_DB_NAME


def prepare_database(target_path: str) -> None:
    """Migrate a legacy database file to ``target_path`` if needed.

    No-op when the target already exists. Otherwise the first existing legacy
    database (and its WAL/SHM sidecars) is *copied* — not moved — to the target,
    leaving the original intact as a safety net.

    Args:
        target_path: The desired (new) database path, e.g. ``sentinel.db``.
    """
    if os.path.exists(target_path):
        return

    for legacy in LEGACY_DB_NAMES:
        if legacy != target_path and os.path.exists(legacy):
            try:
                shutil.copy2(legacy, target_path)
                for suffix in _SIDECAR_SUFFIXES:
                    sidecar = legacy + suffix
                    if os.path.exists(sidecar):
                        shutil.copy2(sidecar, target_path + suffix)
                logger.info(
                    "Migrated legacy database %s -> %s", legacy, target_path
                )
            except OSError as exc:
                logger.error("Database migration failed (%s): %s", legacy, exc)
            return

    logger.info("No legacy database found; starting fresh at %s", target_path)


async def ensure_schema_version(conn: aiosqlite.Connection) -> int:
    """Ensure the ``sentinel_meta`` table exists and record the schema version.

    Acts as the anchor for versioned migrations. Module tables themselves are
    created idempotently with ``IF NOT EXISTS`` by their respective modules, so
    this records the baseline version and provides a hook for future upgrades.

    Args:
        conn: An open aiosqlite connection.

    Returns:
        The schema version now stored.
    """
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS sentinel_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    async with conn.execute(
        "SELECT value FROM sentinel_meta WHERE key = 'schema_version';"
    ) as cur:
        row = await cur.fetchone()

    current = int(row[0]) if row else 0
    if current < SCHEMA_VERSION:
        await conn.execute(
            "INSERT INTO sentinel_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (str(SCHEMA_VERSION),),
        )
        await conn.commit()
        logger.info("Schema version set to %d (was %d)", SCHEMA_VERSION, current)
    return SCHEMA_VERSION
