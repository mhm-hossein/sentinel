"""Periodic check scheduler for Username Hunter.

A single asyncio task wakes every ``check_interval`` seconds and scans every
enabled target whose per-target interval has elapsed. All state lives in SQLite
(``hunter_targets``), so reloading enabled targets after a restart resumes
hunting automatically — the scheduler itself holds no state. The loop never
busy-loops and never raises out of itself: a failed run is logged and the next
tick proceeds normally.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .hunter import UsernameHunter

logger = logging.getLogger("sentinel.hunter.scheduler")


class HunterScheduler:
    """Runs :meth:`UsernameHunter.scan_all` on a fixed interval."""

    def __init__(self, hunter: UsernameHunter) -> None:
        """Initialise the scheduler.

        Args:
            hunter: The username hunter to drive.
        """
        self._hunter = hunter
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        """Start the background check loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="hunter-scheduler")
        logger.info(
            "Hunter scheduler started (every %ds)", self._hunter.config.interval
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
        """Sleep-then-check loop; resilient to per-run failures."""
        interval = self._hunter.config.interval
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._hunter.scan_all()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - keep the loop alive
                    logger.exception("Scheduled Hunter scan failed")
        except asyncio.CancelledError:
            logger.debug("Hunter scheduler cancelled")
            raise
