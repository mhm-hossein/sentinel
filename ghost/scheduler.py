"""Periodic scan scheduler for Ghost Tracker.

A single asyncio task wakes every ``check_interval`` seconds and scans every
enabled target. State lives entirely in SQLite (``ghost_targets`` /
``ghost_snapshots``), so a restart that reloads enabled targets from the
database resumes monitoring automatically — the scheduler itself is stateless.
It never busy-loops and never raises out of its loop: a failed run is logged and
the next tick proceeds normally.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .tracker import GhostTracker

logger = logging.getLogger("sentinel.ghost.scheduler")


class GhostScheduler:
    """Runs :meth:`GhostTracker.scan_all` on a fixed interval."""

    def __init__(self, tracker: GhostTracker) -> None:
        """Initialise the scheduler.

        Args:
            tracker: The Ghost tracker to drive.
        """
        self._tracker = tracker
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        """Start the background scan loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="ghost-scheduler")
        logger.info(
            "Ghost scheduler started (every %d min)",
            self._tracker.config.check_interval_minutes,
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
        """Sleep-then-scan loop; resilient to per-run failures."""
        interval = self._tracker.config.check_interval_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._tracker.scan_all()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - keep the loop alive
                    logger.exception("Scheduled Ghost scan failed")
        except asyncio.CancelledError:
            logger.debug("Ghost scheduler cancelled")
            raise
