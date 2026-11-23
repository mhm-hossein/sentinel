"""Periodic sync scheduler for Archive Watch.

A single asyncio task wakes every ``sync_interval`` seconds and triggers a full
sync run. It never busy-loops and never raises out of its loop — a failed run
is logged and the next tick proceeds as normal.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .archive_manager import ArchiveManager

logger = logging.getLogger("sentinel.archive.scheduler")


class ArchiveScheduler:
    """Runs :meth:`ArchiveManager.sync_all` on a fixed interval."""

    def __init__(self, manager: ArchiveManager) -> None:
        """Initialise the scheduler.

        Args:
            manager: The archive manager to drive.
        """
        self._manager = manager
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        """Start the background sync loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="archive-scheduler")
        logger.info(
            "Archive scheduler started (every %d min)",
            self._manager.config.sync_interval_minutes,
        )

    async def close(self) -> None:
        """Stop the background loop and wait for it to settle."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        """Sleep-then-sync loop; resilient to per-run failures."""
        interval = self._manager.config.sync_interval_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._manager.sync_all()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - keep the loop alive
                    logger.exception("Scheduled archive sync failed")
        except asyncio.CancelledError:
            logger.debug("Archive scheduler cancelled")
            raise
