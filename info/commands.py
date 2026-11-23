"""``.info`` command handler for the Advanced Info module (owner only).

    .info       report on the current target (user / group / channel / reply)
    .info me    same report, delivered to Saved Messages instead of this chat
"""

from __future__ import annotations

import logging
import re

from telethon import TelegramClient, events

from .info_manager import InfoManager

logger = logging.getLogger("sentinel.info.commands")

#: Saved Messages pseudo-peer.
_SAVED = "me"


def help_section() -> str:
    """Return the compact Info help block for the ``.help`` menu."""
    return "\n".join(
        [
            "ℹ️ Info",
            ".info        user / group / channel (or reply)",
            ".info me     send report to Saved Messages",
        ]
    )


class InfoCommands:
    """Register and serve the ``.info`` command."""

    def __init__(
        self,
        client: TelegramClient,
        manager: InfoManager,
        *,
        prefix: str = ".",
    ) -> None:
        """Initialise the handler.

        Args:
            client: The Telethon client.
            manager: The info manager.
            prefix: Command prefix (default ``"."``).
        """
        self._client = client
        self._manager = manager
        self._prefix = prefix

    def register(self) -> None:
        """Attach the ``.info`` handler (owner only)."""
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_info,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}info(?:\s+(me))?$"),
        )
        logger.info("Info command registered (prefix=%r)", self._prefix)

    async def _on_info(self, event: events.NewMessage.Event) -> None:
        """Dispatch ``.info`` / ``.info me``, never crashing."""
        if not event.out:
            return
        try:
            save_mode = bool(event.pattern_match.group(1))
            await self._edit(event, "ℹ️ Gathering…")
            report = await self._manager.build_report(event)

            if save_mode:
                await self._client.send_message(_SAVED, report, link_preview=False)
                await self._edit(event, "✓ Info sent to Saved Messages")
            else:
                await self._edit(event, report)
        except Exception:  # noqa: BLE001 - info must not crash the bot
            logger.exception("Info command failed")
            await self._edit(event, "⚠ Info Error")

    async def _edit(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text, link_preview=False)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .info message")
