"""``.vps`` command handler for the system monitor.

Owner-only; edits the command message with a compact metrics report. Each
invocation increments the global ``monitor_requests`` counter.
"""

from __future__ import annotations

import logging
import re

from telethon import TelegramClient, events

from core.store import COUNTER_MONITOR_REQUESTS, SharedStore

from .monitor import PingResult, SystemMonitor

logger = logging.getLogger("system.commands")


def help_section() -> str:
    """Return the compact System help block for the ``.help`` menu."""
    return "\n".join(
        [
            "🖥 System",
            ".vps  (overview)",
            ".vps cpu|ram|disk|ping|services",
        ]
    )


class SystemCommands:
    """Register and serve the ``.vps`` command."""

    def __init__(
        self,
        client: TelegramClient,
        monitor: SystemMonitor,
        store: SharedStore,
        *,
        prefix: str = ".",
    ) -> None:
        """Initialise the command handler.

        Args:
            client: The Telethon client.
            monitor: The system metrics collector.
            store: Shared store (monitor-request counter).
            prefix: Command prefix.
        """
        self._client = client
        self._monitor = monitor
        self._store = store
        self._prefix = prefix

    def register(self) -> None:
        """Attach the ``.vps`` handler (owner only)."""
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_vps,
            events.NewMessage(
                outgoing=True,
                pattern=rf"(?i)^{p}vps(?:\s+(cpu|ram|disk|ping|services|net|network))?$",
            ),
        )
        logger.info("System .vps command registered")

    async def _on_vps(self, event: events.NewMessage.Event) -> None:
        """Render the requested metric(s), never crashing."""
        try:
            sub = (event.pattern_match.group(1) or "").lower()
            text = await self._render(sub)
            await self._store.incr(COUNTER_MONITOR_REQUESTS)
        except Exception:  # noqa: BLE001 - monitor must not crash the bot
            logger.exception("VPS command failed")
            text = "⚠ Monitor Unavailable"
        await self._edit(event, text)

    async def _render(self, sub: str) -> str:
        """Build the output text for a ``.vps`` subcommand."""
        if sub == "cpu":
            return await self._monitor.cpu()
        if sub == "ram":
            return await self._monitor.ram()
        if sub == "disk":
            return await self._monitor.disk()
        if sub in ("net", "network"):
            return await self._monitor.network()
        if sub == "ping":
            return self._format_ping(await self._monitor.ping())
        if sub == "services":
            return await self._format_services()

        # Full overview.
        cpu, ram, disk, net, uptime, ping = (
            await self._monitor.cpu(),
            await self._monitor.ram(),
            await self._monitor.disk(),
            await self._monitor.network(),
            await self._monitor.uptime(),
            await self._monitor.ping(),
        )
        lines = ["🖥 VPS Status", "", cpu, ram, disk, net, uptime, self._format_ping(ping)]
        services = await self._format_services()
        if services:
            lines += ["", services]
        return "\n".join(lines)

    @staticmethod
    def _format_ping(result: PingResult) -> str:
        """Format a :class:`PingResult` compactly."""
        if result.average_ms < 0:
            return f"PING: unreachable ({result.host})"
        return f"PING: {result.average_ms:.0f}ms avg, {result.min_ms:.0f}ms min ({result.host})"

    async def _format_services(self) -> str:
        """Format detected services, or an empty string if none exist."""
        services = await self._monitor.services()
        if not services:
            return ""
        lines = ["Services:"]
        for name, running in services:
            lines.append(f"• {name}: {'active' if running else 'inactive'}")
        return "\n".join(lines)

    @staticmethod
    async def _edit(event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .vps message")
