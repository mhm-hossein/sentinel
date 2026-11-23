"""Periodic subscription-expiration checker (control-plane background task).

Mirrors the supervisor's resilient loop: wake every ``interval`` seconds, mark
past-due subscriptions expired, and enforce the consequences:

    * mark expired (always) — `FeatureGate` then blocks premium features and the
      control bot blocks new-instance creation for those users;
    * optionally stop the user's workers (`SENTINEL_STOP_ON_EXPIRE=1`) by
      disabling their instances so the supervisor reconciles them down.

Coordination is entirely through the central DB (no direct IPC), exactly like
the supervisor.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from core.central import CentralDatabase

logger = logging.getLogger("sentinel.subscription_checker")


def _interval_seconds() -> int:
    """Check cadence (``SENTINEL_SUBCHECK_SECONDS``, default 300s)."""
    try:
        return max(30, int(os.environ.get("SENTINEL_SUBCHECK_SECONDS", "300")))
    except (TypeError, ValueError):
        return 300


def _stop_on_expire() -> bool:
    """Whether to stop a user's workers when their subscription expires."""
    return os.environ.get("SENTINEL_STOP_ON_EXPIRE", "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


class SubscriptionChecker:
    """Expires due subscriptions on a fixed interval and enforces the effects."""

    def __init__(
        self, central: CentralDatabase, *, interval: Optional[int] = None, alerts: object = None
    ) -> None:
        """Initialise the checker.

        Args:
            central: A connected central database.
            interval: Seconds between checks (defaults from env).
            alerts: Optional ``AlertManager`` (owner alert on expiries).
        """
        self._central = central
        self._interval = interval or _interval_seconds()
        self._stop_on_expire = _stop_on_expire()
        self._alerts = alerts
        self._stopping = False

    async def run(self) -> None:
        """Run the expiration loop until :meth:`stop` is called."""
        logger.info(
            "Subscription checker started (every %ds, stop_on_expire=%s)",
            self._interval, self._stop_on_expire,
        )
        # An immediate first pass catches anything already overdue at boot.
        try:
            while not self._stopping:
                try:
                    await self._tick()
                except Exception:  # noqa: BLE001 - never let the loop die
                    logger.exception("Subscription check tick failed")
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    def stop(self) -> None:
        """Signal the loop to stop."""
        self._stopping = True

    async def _tick(self) -> None:
        """Expire due subscriptions and enforce consequences once."""
        expired_user_pks = await self._central.expire_due_subscriptions()
        if not expired_user_pks:
            return
        logger.info("Expired %d subscription(s)", len(expired_user_pks))
        if self._alerts is not None:
            try:
                await self._alerts.notify(  # type: ignore[attr-defined]
                    "subscription_expiration",
                    f"{len(expired_user_pks)} subscription(s) expired.",
                )
            except Exception:  # noqa: BLE001
                pass
        if not self._stop_on_expire:
            return
        for user_pk in expired_user_pks:
            for inst in await self._central.list_instances(user_pk):
                if inst.enabled:
                    await self._central.set_instance_enabled(inst.id, False)
                    logger.info("Disabled instance %s (subscription expired)", inst.id)


async def run_subscription_checker() -> None:
    """Standalone runner (connects its own central DB)."""
    from sentinel.paths import central_db_path

    central = CentralDatabase(central_db_path())
    await central.connect()
    checker = SubscriptionChecker(central)
    try:
        await checker.run()
    finally:
        await central.close()
