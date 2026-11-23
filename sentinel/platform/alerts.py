"""System alerts to the platform owner (Phase 5).

`AlertManager.notify(kind, message)` sends a rate-limited alert to the owner's
Telegram account (via an injected async sender) and records it in
``alert_state`` (rate limit survives restart). A background :meth:`run` loop
periodically checks host health (disk, RAM) and the aggregate Gemini failure
rate, emitting alerts when thresholds are crossed.

Producers call :meth:`notify` directly:
    * supervisor  → ``worker_crash`` / ``restart_loop``
    * checker     → ``subscription_expiration``
    * backup flow → ``backup_failure``
    * this loop   → ``low_disk`` / ``high_ram`` / ``gemini_failure_rate``

All sends are best-effort; a missing owner id or send failure never raises.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Awaitable, Callable, Optional

from core.central import CentralDatabase

logger = logging.getLogger("sentinel.alerts")

#: Async sender: ``await sender(chat_id, text)``.
Sender = Callable[[int, str], Awaitable[None]]

#: Default per-kind minimum seconds between alerts (rate limit).
_DEFAULT_INTERVALS: dict[str, float] = {
    "worker_crash": 300,
    "restart_loop": 900,
    "database_corruption": 600,
    "backup_failure": 600,
    "gemini_failure_rate": 3600,
    "subscription_expiration": 1800,
    "low_disk": 3600,
    "high_ram": 1800,
}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class AlertManager:
    """Rate-limited owner alerts + a periodic host-health loop."""

    def __init__(
        self,
        central: CentralDatabase,
        sender: Optional[Sender],
        owner_id: int,
        *,
        interval: Optional[int] = None,
    ) -> None:
        """Initialise the manager.

        Args:
            central: A connected central database (rate-limit state).
            sender: Async ``(chat_id, text)`` sender, or ``None`` to disable sends.
            owner_id: Owner Telegram id (alerts target). ``0`` disables alerts.
            interval: Health-loop cadence seconds (``SENTINEL_ALERT_SECONDS``).
        """
        self._central = central
        self._sender = sender
        self._owner_id = owner_id
        self._interval = interval or _int_env("SENTINEL_ALERT_SECONDS", 120)
        self._disk_min_pct = _int_env("SENTINEL_ALERT_DISK_FREE_PCT", 10)
        self._ram_max_pct = _int_env("SENTINEL_ALERT_RAM_PCT", 90)
        self._gemini_fail_threshold = _int_env("SENTINEL_ALERT_GEMINI_FAILS", 20)
        self._stopping = False

    async def notify(self, kind: str, message: str, *, min_interval: Optional[float] = None) -> bool:
        """Send an alert of ``kind`` if the rate limit allows. Returns sent?."""
        if self._owner_id <= 0 or self._sender is None:
            return False
        interval = min_interval if min_interval is not None else _DEFAULT_INTERVALS.get(kind, 600)
        try:
            if not await self._central.alert_should_send(kind, interval):
                return False
            await self._sender(self._owner_id, f"🚨 **{kind}**\n{message}")
            await self._central.record_alert_sent(kind)
            return True
        except Exception:  # noqa: BLE001 - alerting must never raise
            logger.debug("alert %s failed", kind)
            return False

    # ------------------------------------------------------------------ #
    # Health loop
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Periodically check disk/RAM/Gemini and emit alerts until stopped."""
        logger.info("Alert manager started (every %ds, owner=%s)", self._interval, self._owner_id)
        try:
            while not self._stopping:
                try:
                    await self._check_health()
                except Exception:  # noqa: BLE001 - never let the loop die
                    logger.exception("Alert health check failed")
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    def stop(self) -> None:
        """Signal the health loop to stop."""
        self._stopping = True

    async def _check_health(self) -> None:
        """One pass of disk/RAM/Gemini-failure checks."""
        # Disk free % on the data root.
        try:
            root = os.environ.get("SENTINEL_DATA_ROOT", "data")
            target = root if os.path.exists(root) else "."
            usage = shutil.disk_usage(target)
            free_pct = 100.0 * usage.free / usage.total if usage.total else 100.0
            if free_pct < self._disk_min_pct:
                await self.notify("low_disk", f"Free disk {free_pct:.1f}% (< {self._disk_min_pct}%)")
        except Exception:  # noqa: BLE001
            pass

        # RAM usage %.
        try:
            import psutil  # optional

            ram = psutil.virtual_memory().percent
            if ram > self._ram_max_pct:
                await self.notify("high_ram", f"RAM usage {ram:.0f}% (> {self._ram_max_pct}%)")
        except Exception:  # noqa: BLE001
            pass

        # Aggregate Gemini failure rate.
        try:
            fails, keys = await self._central.gemini_failure_totals()
            if keys and fails >= self._gemini_fail_threshold:
                await self.notify(
                    "gemini_failure_rate",
                    f"{fails} Gemini failures across {keys} key(s) (≥ {self._gemini_fail_threshold})",
                )
        except Exception:  # noqa: BLE001
            pass
