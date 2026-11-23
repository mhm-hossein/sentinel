"""Global command handler for Sentinel-wide commands (``.stats``).

Provides the reworked, project-wide statistics command that aggregates every
module's figures via the :mod:`core.stats` registry. Owner-only; edits the
command message in place.
"""

from __future__ import annotations

import logging
import re
import time

from telethon import TelegramClient, events

from .stats import STAT_LAYOUT, collect_stats
from .store import SharedStore

logger = logging.getLogger("sentinel.commands")


def format_uptime(seconds: float) -> str:
    """Format an uptime duration compactly, e.g. ``"14d 3h"`` or ``"42m"``.

    Args:
        seconds: Elapsed seconds.

    Returns:
        A short uptime string (at most two units).
    """
    seconds = int(seconds)
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


class StatsCommand:
    """Register and serve the global ``.stats`` command."""

    def __init__(
        self,
        client: TelegramClient,
        store: SharedStore,
        *,
        prefix: str = ".",
        started_at: float | None = None,
    ) -> None:
        """Initialise the command.

        Args:
            client: The Telethon client.
            store: Shared store (used for the DB health check).
            prefix: Command prefix.
            started_at: Process start timestamp for uptime (defaults to now).
        """
        self._client = client
        self._store = store
        self._prefix = prefix
        self._started_at = started_at if started_at is not None else time.time()

    def register(self) -> None:
        """Attach the ``.stats`` handler (owner only)."""
        pattern = rf"(?i)^{re.escape(self._prefix)}stats$"
        self._client.add_event_handler(
            self._on_stats, events.NewMessage(outgoing=True, pattern=pattern)
        )
        logger.info("Global .stats command registered")

    async def _on_stats(self, event: events.NewMessage.Event) -> None:
        """Render the aggregated Sentinel statistics, never crashing."""
        try:
            data = await collect_stats()
            db_ok = await self._store.healthy()
        except Exception:  # noqa: BLE001 - stats must never crash the bot
            logger.exception("Failed to build .stats output")
            await self._safe_edit(event, "⚠ Statistics Unavailable")
            return

        lines = ["📊 Sentinel Statistics", ""]
        for key, label in STAT_LAYOUT:
            lines.append(f"{label}: {data.get(key, 0)}")
        lines += [
            "",
            f"Uptime: {format_uptime(time.time() - self._started_at)}",
            f"DB: {'OK' if db_ok else 'ERROR'}",
        ]
        await self._safe_edit(event, "\n".join(lines))

    @staticmethod
    async def _safe_edit(event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text, link_preview=False)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .stats message")
