"""Per-user feature wiring — the multi-user replacement for ``start.setup_features``.

``setup_features(ctx)`` boots every feature module for a single tenant, sourcing
the client, database, store, prefix, paths, Gemini keys and plan from the
``UserContext`` instead of process-global state. It preserves the original
5-seam wiring per module (DB connect → commands.register → stat provider →
help section → scheduler) and the fail-soft ``try/except`` guarantee (a broken
module never blocks a user's startup).

Two behavioral additions vs. the single-user version:

    * **Plan enablement** — a module is only wired when ``ctx.plan.allows(<key>)``.
    * **Per-user isolation** — exports/backups/cache resolve under
      ``ctx.paths.*`` and the AI key comes from ``ctx.gemini_keys``.

Because each user runs in its own process, the module-global registries
(``core.stats._providers``, ``banner.commands.EXTRA_HELP_SECTIONS``) are already
per-tenant and need no locking.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Awaitable, Callable

from .context import UserContext

logger = logging.getLogger("sentinel.features")

#: An async teardown callable returned by :func:`setup_features`.
Teardown = Callable[[], Awaitable[None]]


async def setup_features(ctx: UserContext) -> Teardown:
    """Wire every plan-enabled feature module for one tenant.

    Args:
        ctx: The user's fully-built context (client connected, store connected,
            plan/paths/keys resolved).

    Returns:
        An async teardown closing every resource opened here (reverse order),
        including ``ctx.store``.
    """
    from core.commands import StatsCommand
    from core.stats import clear_providers, make_store_provider, register_stat_provider

    started_at = time.time()

    # Each user process has its own module-global registries; start clean so a
    # re-entrant setup (tests) never double-registers stat providers.
    clear_providers()

    # Closeables collected for orderly shutdown (objects exposing async close()).
    closeables: list[object] = [ctx.store]

    # Shared store counters provider (saved media, deletions, edits, …).
    register_stat_provider(make_store_provider(ctx.store))

    # Banner is both the historical entry point and a feature; wire it here so
    # the worker stays feature-agnostic.
    await _setup_banner(ctx, closeables)

    # Feature modules (order controls help-menu section order), each gated by plan.
    await _gated(ctx, "ai", _setup_ai, closeables)
    await _gated(ctx, "variables", _setup_variables, closeables)
    await _gated(ctx, "save", _setup_save, closeables)
    await _gated(ctx, "info", _setup_info, closeables)
    await _gated(ctx, "archive", _setup_archive, closeables)
    await _gated(ctx, "export", _setup_export, closeables)
    await _gated(ctx, "search", _setup_search, closeables)
    await _gated(ctx, "ghost", _setup_ghost, closeables)
    await _gated(ctx, "hunter", _setup_hunter, closeables)
    await _gated(ctx, "tracking", _setup_tracking, closeables)
    await _gated(ctx, "logger", _setup_logger, closeables)
    await _gated(ctx, "system", _setup_system, closeables)
    await _gated(ctx, "backup", _setup_backup, closeables)

    StatsCommand(
        ctx.client, ctx.store, prefix=ctx.prefix, started_at=started_at
    ).register()
    logger.info("User %s: feature modules registered", ctx.user_id)

    async def teardown() -> None:
        """Close every feature resource (reverse order)."""
        for resource in reversed(closeables):
            try:
                await resource.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - best-effort shutdown
                logger.debug("Error closing %s", type(resource).__name__)

    return teardown


async def _gated(
    ctx: UserContext,
    module: str,
    setup: Callable[[UserContext, list], Awaitable[None]],
    closeables: list,
) -> None:
    """Run ``setup`` only if the user's plan enables ``module``."""
    if not ctx.plan.allows(module):
        logger.info("User %s: %s disabled by plan %r", ctx.user_id, module, ctx.plan.name)
        return
    await setup(ctx, closeables)


# --------------------------------------------------------------------------- #
# Per-module setups (each reads exclusively from ``ctx``)
# --------------------------------------------------------------------------- #
async def _setup_banner(ctx: UserContext, closeables: list) -> None:
    """Wire the banner engine (DB + manager + commands) for this user."""
    try:
        from banner.banner_manager import BannerManager
        from banner.commands import CommandHandler
        from banner.database import Database
        from core.stats import register_stat_provider

        database = Database(ctx.db_path)
        manager = BannerManager(ctx.client, database)
        CommandHandler(ctx.client, manager, prefix=ctx.prefix, plan=ctx.plan).register()
        await manager.start()  # connects the DB + restores active banners

        async def banner_provider() -> dict[str, int]:
            counts = await manager._db.status_counts()  # aggregate SQL, no scan
            return {
                "banners": int(counts.get("total", 0)),
                "banner_sends": int(counts.get("total_sent", 0)),
            }

        register_stat_provider(banner_provider)
        closeables.append(_Closer(manager.stop))
        logger.info("User %s: banner module enabled", ctx.user_id)
    except Exception:  # noqa: BLE001 - a feature must never block startup
        logger.exception("User %s: failed to initialise banner module", ctx.user_id)


async def _setup_ai(ctx: UserContext, closeables: list) -> None:
    """Initialise the Gemini AI Assistant module (per-user keys)."""
    try:
        from ai.assistant import AIAssistant
        from ai.commands import AICommandHandler
        from ai.config import AIConfig
        from ai.database import AIDatabase
        from core.stats import register_stat_provider

        config = AIConfig.from_context(ctx)
        if not config.api_key:
            logger.warning("User %s: no Gemini key — AI module disabled", ctx.user_id)
            return

        ai_db = AIDatabase(config.db_path)
        await ai_db.connect()
        closeables.append(ai_db)
        assistant = AIAssistant(ctx.client, ai_db, config, store=ctx.store)
        AICommandHandler(ctx.client, assistant, prefix=ctx.prefix).register()

        async def ai_provider() -> dict[str, int]:
            return {"ai_requests": await ai_db.total_requests()}

        register_stat_provider(ai_provider)
        logger.info("User %s: AI enabled (model %s)", ctx.user_id, config.default_model)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise AI module", ctx.user_id)


async def _setup_variables(ctx: UserContext, closeables: list) -> None:
    """Initialise the Quick Variables (aliases) module."""
    try:
        from banner.commands import register_help_section
        from core.stats import register_stat_provider
        from variables.commands import VariablesCommands, help_section
        from variables.database import VariablesDatabase
        from variables.manager import VariablesManager

        database = VariablesDatabase(ctx.db_path)
        await database.connect()
        closeables.append(database)
        VariablesCommands(
            ctx.client, VariablesManager(database), prefix=ctx.prefix
        ).register()

        async def variables_provider() -> dict[str, int]:
            return {"variables": await database.count()}

        register_stat_provider(variables_provider)
        register_help_section(help_section())
        logger.info("User %s: variables enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise variables module", ctx.user_id)


async def _setup_save(ctx: UserContext, closeables: list) -> None:
    """Initialise the Save Message module (stateless)."""
    try:
        from banner.commands import register_help_section
        from save.commands import SaveCommands, help_section
        from save.saver import MessageSaver

        SaveCommands(
            ctx.client, MessageSaver(ctx.client, ctx.store), prefix=ctx.prefix
        ).register()
        register_help_section(help_section())
        logger.info("User %s: save enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise save module", ctx.user_id)


async def _setup_info(ctx: UserContext, closeables: list) -> None:
    """Initialise the Advanced Info module (stateless)."""
    try:
        from banner.commands import register_help_section
        from info.commands import InfoCommands, help_section
        from info.info_manager import InfoManager

        InfoCommands(
            ctx.client, InfoManager(ctx.client, ctx.store), prefix=ctx.prefix
        ).register()
        register_help_section(help_section())
        logger.info("User %s: info enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise info module", ctx.user_id)


async def _setup_archive(ctx: UserContext, closeables: list) -> None:
    """Initialise Archive Watch (history mirror + scheduler)."""
    try:
        from archive.archive_manager import ArchiveConfig, ArchiveManager
        from archive.commands import ArchiveCommands, help_section
        from archive.database import ArchiveDatabase
        from archive.scheduler import ArchiveScheduler
        from banner.commands import register_help_section
        from core.stats import register_stat_provider

        database = ArchiveDatabase(ctx.db_path)
        await database.connect()
        closeables.append(database)
        manager = ArchiveManager(ctx.client, database, ctx.store, ArchiveConfig.from_env())
        ArchiveCommands(
            ctx.client, manager, database, db_path=ctx.db_path, store=ctx.store,
            exports_dir=ctx.paths.exports, prefix=ctx.prefix, plan=ctx.plan,
        ).register()

        scheduler = ArchiveScheduler(manager)
        scheduler.start()
        closeables.append(scheduler)

        register_stat_provider(manager.stat_provider)
        register_help_section(help_section())
        logger.info("User %s: archive enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise archive module", ctx.user_id)


async def _setup_export(ctx: UserContext, closeables: list) -> None:
    """Initialise the live Chat Export module."""
    try:
        from banner.commands import register_help_section
        from export.commands import ExportCommands, help_section
        from export.exporter import ChatExporter, ExportConfig

        config = ExportConfig.from_env()
        _set_if_present(config, "exports_dir", ctx.paths.exports)
        _set_if_present(config, "output_dir", ctx.paths.exports)
        exporter = ChatExporter(ctx.client, config)
        ExportCommands(ctx.client, exporter, ctx.store, prefix=ctx.prefix).register()
        register_help_section(help_section())
        logger.info("User %s: export enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise export module", ctx.user_id)


async def _setup_search(ctx: UserContext, closeables: list) -> None:
    """Initialise the live Global Search module."""
    try:
        from banner.commands import register_help_section
        from search.commands import SearchCommands, help_section
        from search.service import SearchService

        SearchCommands(
            ctx.client, SearchService(ctx.client), ctx.store,
            prefix=ctx.prefix, exports_dir=ctx.paths.exports,
        ).register()
        register_help_section(help_section())
        logger.info("User %s: search enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise search module", ctx.user_id)


async def _setup_ghost(ctx: UserContext, closeables: list) -> None:
    """Initialise Ghost Tracker (profile-change monitor + scheduler)."""
    try:
        from banner.commands import register_help_section
        from core.stats import register_stat_provider
        from ghost.commands import GhostCommands, help_section
        from ghost.database import GhostDatabase
        from ghost.scheduler import GhostScheduler
        from ghost.tracker import GhostConfig, GhostTracker

        database = GhostDatabase(ctx.db_path)
        await database.connect()
        closeables.append(database)
        tracker = GhostTracker(ctx.client, database, ctx.store, GhostConfig.from_env())
        GhostCommands(
            ctx.client, tracker, database,
            db_path=ctx.db_path, exports_dir=ctx.paths.exports, prefix=ctx.prefix,
            plan=ctx.plan,
        ).register()

        await tracker.restore()
        scheduler = GhostScheduler(tracker)
        scheduler.start()
        closeables.append(scheduler)

        register_stat_provider(tracker.stat_provider)
        register_help_section(help_section())
        logger.info("User %s: ghost enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise ghost module", ctx.user_id)


async def _setup_hunter(ctx: UserContext, closeables: list) -> None:
    """Initialise Username Hunter (availability monitor + claim scheduler)."""
    try:
        from banner.commands import register_help_section
        from core.stats import register_stat_provider
        from hunter.commands import HunterCommands, help_section
        from hunter.database import HunterDatabase
        from hunter.hunter import HunterConfig, UsernameHunter
        from hunter.scheduler import HunterScheduler

        database = HunterDatabase(ctx.db_path)
        await database.connect()
        closeables.append(database)
        hunter = UsernameHunter(ctx.client, database, ctx.store, HunterConfig.from_env())
        HunterCommands(
            ctx.client, hunter, database, ctx.store,
            db_path=ctx.db_path, exports_dir=ctx.paths.exports, prefix=ctx.prefix,
            plan=ctx.plan,
        ).register()

        await hunter.restore()
        scheduler = HunterScheduler(hunter)
        scheduler.start()
        closeables.append(scheduler)

        register_stat_provider(hunter.stat_provider)
        register_help_section(help_section())
        logger.info("User %s: hunter enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise hunter module", ctx.user_id)


async def _setup_tracking(ctx: UserContext, closeables: list) -> None:
    """Initialise the tracking module (autosave / anti-delete / edit tracker)."""
    try:
        from banner.commands import register_help_section
        from tracking.database import TrackingDatabase
        from tracking.service import TrackingConfig, TrackingService, help_section

        config = TrackingConfig.from_env()
        config.cache_dir = ctx.paths.cache
        config.autosave_dir = os.path.join(ctx.paths.cache, "autosave")

        database = TrackingDatabase(ctx.db_path)
        service = TrackingService(ctx.client, database, ctx.store, config, prefix=ctx.prefix)
        await service.start()
        service.register()
        closeables.append(service)  # stop() unregisters handlers first
        closeables.append(database)
        register_help_section(help_section())
        logger.info("User %s: tracking enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise tracking module", ctx.user_id)


async def _setup_logger(ctx: UserContext, closeables: list) -> None:
    """Initialise the message logger module."""
    try:
        from banner.commands import register_help_section
        from logger.database import LoggerDatabase
        from logger.service import LoggerConfig, LoggerService, help_section

        database = LoggerDatabase(ctx.db_path)
        service = LoggerService(
            ctx.client, database, ctx.store, LoggerConfig.from_env(), prefix=ctx.prefix
        )
        await service.start()
        service.register()
        closeables.append(service)  # stop() unregisters handlers first
        closeables.append(database)
        register_help_section(help_section())
        logger.info("User %s: logger enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise logger module", ctx.user_id)


async def _setup_system(ctx: UserContext, closeables: list) -> None:
    """Initialise the VPS monitor module (stateless)."""
    try:
        from banner.commands import register_help_section
        from system.commands import SystemCommands, help_section
        from system.monitor import SystemMonitor

        SystemCommands(
            ctx.client, SystemMonitor.from_env(), ctx.store, prefix=ctx.prefix
        ).register()
        register_help_section(help_section())
        logger.info("User %s: system enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise system module", ctx.user_id)


async def _setup_backup(ctx: UserContext, closeables: list) -> None:
    """Initialise the backup manager module (per-user output dir + db)."""
    try:
        from backup.commands import BackupCommands, help_section
        from backup.manager import BackupConfig, BackupManager
        from banner.commands import register_help_section

        config = BackupConfig.from_env(ctx.db_path)
        config.backup_path = ctx.paths.backups
        config.session_name = ctx.paths.session_name
        config.extra_paths = [ctx.paths.exports]
        manager = BackupManager(ctx.store, config)
        await manager.start()
        closeables.append(manager)
        BackupCommands(ctx.client, manager, prefix=ctx.prefix).register()
        register_help_section(help_section())
        logger.info("User %s: backup enabled", ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("User %s: failed to initialise backup module", ctx.user_id)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _Closer:
    """Adapt an arbitrary async zero-arg callable to the ``close()`` protocol."""

    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[[], Awaitable[None]]) -> None:
        self._fn = fn

    async def close(self) -> None:
        await self._fn()


def _set_if_present(obj: object, attr: str, value: object) -> None:
    """Set ``obj.attr = value`` only when the attribute already exists.

    Lets us route optional per-config path fields (whose names vary across
    modules) to the per-user directory without assuming a specific schema.
    """
    if hasattr(obj, attr):
        try:
            setattr(obj, attr, value)
        except Exception:  # noqa: BLE001 - slots/frozen configs are fine to skip
            pass
