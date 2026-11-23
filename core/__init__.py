"""Shared infrastructure for the Sentinel automation platform.

This package holds cross-cutting concerns used by every feature module
(banner, ai, tracking, logger, system, backup) without those modules depending
on one another:

    * :mod:`core.migrations` -- database path resolution, legacy-database
      migration (``banner.db`` -> ``sentinel.db``) and schema versioning.
    * :mod:`core.store`      -- a shared key/value store for feature toggles and
      global counters.
    * :mod:`core.stats`      -- a decoupled statistics-provider registry feeding
      the global ``.stats`` command.
    * :mod:`core.commands`   -- the global ``.stats`` command handler.

Feature modules depend on ``core`` (a star topology); ``core`` depends on no
feature module.
"""

from __future__ import annotations

from .migrations import prepare_database, resolve_db_path
from .store import SharedStore
from .stats import register_stat_provider, collect_stats

__all__ = [
    "prepare_database",
    "resolve_db_path",
    "SharedStore",
    "register_stat_provider",
    "collect_stats",
]

#: Human-facing project identity.
APP_NAME = "Sentinel"
__version__ = "2.0.0"
