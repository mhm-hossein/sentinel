"""Ghost Tracker engine: snapshotting, change detection and alerting.

Resolves targets, takes profile snapshots, diffs them against the stored
baseline and, for each real change, persists a history row and pushes an alert
to Saved Messages. A single failing target never aborts a whole scan run, and
``FloodWaitError`` is honoured (the offending target is skipped for this run and
retried on the next tick).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.errors import FloodWaitError

from core.store import SharedStore

from .database import GhostDatabase
from .models import GhostTarget, ProfileChange
from .utils import build_profile_snapshot, diff_snapshots, format_alert

logger = logging.getLogger("sentinel.ghost.tracker")

#: Destination for change alerts — the account's own Saved Messages.
_SAVED = "me"

#: Shared-counter keys owned by Ghost.
COUNTER_GHOST_SCANS = "ghost_scan_runs"


@dataclass(slots=True)
class GhostConfig:
    """Configuration for Ghost Tracker.

    Attributes:
        check_interval_minutes: Minutes between automatic profile scans.
    """

    check_interval_minutes: int = 30

    @classmethod
    def from_env(cls) -> "GhostConfig":
        """Build configuration from environment variables."""
        return cls(
            check_interval_minutes=_int_env("GHOST_CHECK_INTERVAL_MINUTES", 30),
        )

    @property
    def check_interval_seconds(self) -> int:
        """Scan interval in seconds (minimum 60s)."""
        return max(60, self.check_interval_minutes * 60)


class GhostTracker:
    """Coordinates profile snapshotting and change reporting."""

    def __init__(
        self,
        client: TelegramClient,
        database: GhostDatabase,
        store: SharedStore,
        config: GhostConfig,
    ) -> None:
        """Initialise the tracker.

        Args:
            client: The Telethon client.
            database: Ghost persistence layer.
            store: Shared store (scan-run counter).
            config: Ghost configuration.
        """
        self._client = client
        self._db = database
        self._store = store
        self._config = config

    @property
    def config(self) -> GhostConfig:
        """The active configuration."""
        return self._config

    @property
    def database(self) -> GhostDatabase:
        """The backing database (used by the command handler)."""
        return self._db

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #
    async def resolve(self, key: object) -> tuple[object, int, Optional[str], str]:
        """Resolve a key to ``(entity, user_id, username, display_name)``.

        Args:
            key: A username, numeric id, or entity.

        Returns:
            ``(entity, user_id, username, display_name)``.
        """
        entity = await self._client.get_entity(key)
        user_id = tl_utils.get_peer_id(entity)
        username = getattr(entity, "username", None)
        display_name = tl_utils.get_display_name(entity) or str(user_id)
        return entity, user_id, username, display_name

    # ------------------------------------------------------------------ #
    # Add / remove
    # ------------------------------------------------------------------ #
    async def add_target(self, key: object) -> GhostTarget:
        """Add a user and immediately capture a baseline snapshot.

        Args:
            key: A username, numeric id, or entity to monitor.

        Returns:
            The persisted :class:`GhostTarget`.
        """
        entity, user_id, username, display_name = await self.resolve(key)
        target = await self._db.add_target(user_id, username, display_name)
        # Capture the initial snapshot so the first change is detected next scan.
        try:
            snapshot = await build_profile_snapshot(self._client, entity)
            await self._db.save_snapshot(user_id, snapshot)
            await self._db.set_last_check(user_id, time.time())
        except FloodWaitError as exc:
            logger.warning("Ghost add FloodWait %ss for %s", exc.seconds, user_id)
        except Exception:  # noqa: BLE001 - baseline is best-effort
            logger.exception("Ghost baseline snapshot failed for %s", user_id)
        logger.info("Ghost tracking %s (%s)", display_name, user_id)
        return await self._db.get_target(user_id) or target

    async def remove_target(self, user_id: int) -> bool:
        """Stop monitoring a user and drop its snapshots/history."""
        removed = await self._db.remove_target(user_id)
        if removed:
            logger.info("Ghost untracked %s", user_id)
        return removed

    # ------------------------------------------------------------------ #
    # Scanning
    # ------------------------------------------------------------------ #
    async def check_target(self, target: GhostTarget) -> list[ProfileChange]:
        """Scan one target, persist changes and emit alerts.

        Args:
            target: The monitored user.

        Returns:
            The list of detected changes (empty if none / unreachable).
        """
        if not target.enabled:
            return []
        try:
            entity = await self._client.get_entity(target.user_id)
        except FloodWaitError as exc:
            logger.warning("Ghost FloodWait %ss resolving %s", exc.seconds, target.user_id)
            await asyncio.sleep(int(exc.seconds) + 1)
            return []
        except Exception:  # noqa: BLE001 - user gone / inaccessible
            logger.warning("Ghost: cannot resolve user %s", target.user_id)
            await self._db.set_last_check(target.user_id, time.time())
            return []

        try:
            new_snapshot = await build_profile_snapshot(self._client, entity)
        except FloodWaitError as exc:
            logger.warning("Ghost FloodWait %ss scanning %s", exc.seconds, target.user_id)
            await asyncio.sleep(int(exc.seconds) + 1)
            return []
        except Exception:  # noqa: BLE001 - one target must not break the run
            logger.exception("Ghost snapshot failed for %s", target.user_id)
            await self._db.set_last_check(target.user_id, time.time())
            return []

        old_snapshot = await self._db.get_snapshot(target.user_id)
        changes = diff_snapshots(old_snapshot, new_snapshot)

        # Persist the new baseline and watermark regardless of whether anything
        # changed, so the next diff is against the most recent state.
        await self._db.save_snapshot(target.user_id, new_snapshot)
        await self._db.set_last_check(target.user_id, time.time())

        if changes:
            display_name = (
                new_snapshot.get("display_name")
                or target.display_name
                or str(target.user_id)
            )
            username = new_snapshot.get("username") or target.username
            await self._db.update_identity(target.user_id, username, display_name)
            for change in changes:
                await self._db.add_history(
                    target.user_id, change.field_name, change.old_value, change.new_value
                )
                logger.info(
                    "Ghost change for %s: %s %r -> %r",
                    target.user_id, change.field_name, change.old_value, change.new_value,
                )
                await self._send_alert(str(display_name), target.user_id, change)

        return changes

    async def scan_all(self) -> int:
        """Scan every enabled target; record one scan run. Returns total changes."""
        targets = await self._db.list_targets(enabled_only=True)
        total = 0
        for target in targets:
            try:
                total += len(await self.check_target(target))
            except Exception:  # noqa: BLE001 - keep the run alive
                logger.exception("Ghost scan failed for %s", target.user_id)
        await self._store.incr(COUNTER_GHOST_SCANS)
        logger.info(
            "Ghost scan run complete: %d changes across %d targets", total, len(targets)
        )
        return total

    async def restore(self) -> int:
        """Log the number of targets resumed at startup; returns that count."""
        targets = await self._db.list_targets(enabled_only=True)
        logger.info("Ghost restored %d active target(s)", len(targets))
        return len(targets)

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #
    async def stat_provider(self) -> dict[str, int]:
        """Return Ghost figures for the global ``.stats`` command."""
        return {
            "ghost_users": await self._db.count_targets(),
            "ghost_changes": await self._db.count_history(),
        }

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _send_alert(
        self, display_name: str, user_id: int, change: ProfileChange
    ) -> None:
        """Send a single change alert to Saved Messages (best-effort)."""
        try:
            await self._client.send_message(
                _SAVED, format_alert(display_name, user_id, change), link_preview=False
            )
        except FloodWaitError as exc:
            logger.warning("Ghost alert FloodWait %ss", exc.seconds)
            await asyncio.sleep(int(exc.seconds) + 1)
        except Exception:  # noqa: BLE001 - alerting is best-effort
            logger.debug("Failed to send Ghost alert for %s", user_id)


def _int_env(name: str, default: int) -> int:
    """Read an integer environment variable, falling back on parse failure."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
