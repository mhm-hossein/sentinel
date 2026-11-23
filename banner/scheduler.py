"""Async scheduler for banner tasks.

Each active banner is driven by its own :class:`asyncio.Task` that sleeps
until the banner's ``next_run`` timestamp, performs a send via an injected
sender coroutine, persists the result, and repeats. The design is intentionally
event-loop friendly: there are no busy-loops, so dozens of banners cost almost
nothing while idle.

Responsibilities handled here:
    * timing (sleep until ``next_run``)
    * ``FloodWaitError`` handling (sleep the required duration, then resume)
    * advancing counters and persisting after each successful send
    * reacting to fatal errors by pausing the banner instead of crashing
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from telethon.errors import FloodWaitError

from .database import Database
from .models import Banner, BannerStatus

logger = logging.getLogger("banner.scheduler")

#: Type of the injected coroutine that performs one send for a banner.
SenderFunc = Callable[[Banner], Awaitable[None]]


class BannerFatalError(Exception):
    """Raised by a sender when a banner cannot continue.

    Examples: the source message was deleted, the chat is gone, or the account
    lacks permission to post. The scheduler reacts by pausing the banner rather
    than crashing the loop.
    """


class BannerScheduler:
    """Manage one independent :class:`asyncio.Task` per active banner.

    The scheduler is deliberately decoupled from Telethon: it receives a
    ``sender`` coroutine that knows how to deliver a banner's message. This
    keeps timing/retry logic isolated and unit-testable.
    """

    def __init__(self, database: Database, sender: SenderFunc) -> None:
        """Initialise the scheduler.

        Args:
            database: Persistence layer used to record sends and status
                changes.
            sender: Coroutine invoked once per scheduled tick to perform the
                actual Telegram send. It must raise :class:`FloodWaitError` on
                rate limits and :class:`BannerFatalError` on unrecoverable
                conditions.
        """
        self._db = database
        self._sender = sender
        self._tasks: dict[int, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ #
    # Public scheduling API
    # ------------------------------------------------------------------ #
    def is_scheduled(self, banner_id: int) -> bool:
        """Return whether an active task exists for ``banner_id``."""
        task = self._tasks.get(banner_id)
        return task is not None and not task.done()

    def schedule(self, banner: Banner) -> None:
        """Start (or restart) the task loop for an active banner.

        If ``next_run`` is unset it is initialised to ``now + interval`` so a
        freshly created banner does not fire instantly. Any existing task for
        the same ID is cancelled first.

        Args:
            banner: The banner to schedule. Must have a valid ``id``.
        """
        if banner.id is None:
            raise ValueError("Cannot schedule a banner without an id")
        if banner.status is not BannerStatus.ACTIVE:
            return

        self.unschedule(banner.id)
        if banner.next_run is None:
            banner.next_run = time.time() + banner.interval

        task = asyncio.create_task(
            self._run(banner), name=f"banner-{banner.id}"
        )
        self._tasks[banner.id] = task
        logger.debug("Scheduled banner #%s", banner.id)

    def unschedule(self, banner_id: int) -> None:
        """Cancel and forget the task for ``banner_id`` if present."""
        task = self._tasks.pop(banner_id, None)
        if task is not None and not task.done():
            task.cancel()
            logger.debug("Unscheduled banner #%s", banner_id)

    def reschedule(self, banner: Banner) -> None:
        """Apply changed timing/state by restarting the banner's task."""
        if banner.id is not None:
            self.unschedule(banner.id)
        self.schedule(banner)

    def unschedule_all(self) -> int:
        """Cancel and forget every scheduled task.

        Each task is cancelled and removed from the in-memory registry so no
        zombie tasks or dangling references remain. Unlike :meth:`shutdown`
        this does not await the tasks, keeping the call non-blocking; the
        cancelled tasks settle on the event loop shortly after.

        Returns:
            The number of tasks that were cancelled.
        """
        banner_ids = list(self._tasks.keys())
        for banner_id in banner_ids:
            self.unschedule(banner_id)
        return len(banner_ids)

    async def shutdown(self) -> None:
        """Cancel every running task and wait for them to settle."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Scheduler shut down (%d task(s) cancelled)", len(tasks))

    @property
    def active_count(self) -> int:
        """Number of currently running banner tasks."""
        return sum(1 for t in self._tasks.values() if not t.done())

    # ------------------------------------------------------------------ #
    # Internal task loop
    # ------------------------------------------------------------------ #
    async def _run(self, banner: Banner) -> None:
        """Drive a single banner until cancelled or paused on fatal error.

        The loop sleeps until ``next_run``, sends, persists, then advances
        ``next_run`` by the interval. ``FloodWaitError`` simply delays the
        next attempt; any other transient error is logged and the banner is
        rescheduled for a later attempt. A :class:`BannerFatalError` pauses
        the banner permanently.

        Args:
            banner: The banner instance owned by this task.
        """
        try:
            while True:
                delay = self._seconds_until(banner.next_run)
                if delay > 0:
                    await asyncio.sleep(delay)

                try:
                    await self._sender(banner)
                except FloodWaitError as exc:
                    wait = int(getattr(exc, "seconds", 0)) + 1
                    logger.warning(
                        "Banner #%s hit FloodWait; sleeping %ss",
                        banner.id,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue  # retry the same tick without advancing schedule
                except BannerFatalError as exc:
                    logger.error("Banner #%s fatal: %s -- pausing", banner.id, exc)
                    await self._pause_on_failure(banner)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - never let the loop die
                    logger.exception(
                        "Banner #%s transient send failure; will retry next tick",
                        banner.id,
                    )
                    # Advance schedule so a persistent error doesn't hot-loop.
                    banner.next_run = time.time() + banner.interval
                    continue

                # Success: advance counters and persist.
                now = time.time()
                banner.sent_count += 1
                banner.last_run = now
                banner.next_run = now + banner.interval
                await self._db.record_send(banner)
                logger.info(
                    "Banner #%s sent (count=%d, next in %ds)",
                    banner.id,
                    banner.sent_count,
                    banner.interval,
                )
        except asyncio.CancelledError:
            logger.debug("Banner #%s task cancelled", banner.id)
            raise

    async def _pause_on_failure(self, banner: Banner) -> None:
        """Mark ``banner`` paused and persist, swallowing DB errors."""
        banner.status = BannerStatus.PAUSED
        banner.next_run = None
        try:
            await self._db.update_banner(banner)
        except Exception:  # noqa: BLE001 - status persistence is best-effort
            logger.exception("Failed to persist pause for banner #%s", banner.id)
        finally:
            if banner.id is not None:
                self._tasks.pop(banner.id, None)

    @staticmethod
    def _seconds_until(target: Optional[float]) -> float:
        """Return non-negative seconds from now until ``target`` (0 if past)."""
        if target is None:
            return 0.0
        return max(0.0, target - time.time())
