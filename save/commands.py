"""``.save`` command handler for the Save Message module (owner only).

    .save        reply to a message → save it (and its album) to Saved Messages
    .save <n>    save the last <n> messages of the current chat

Replies edit the command message in place to report progress/outcome.
"""

from __future__ import annotations

import logging
import re

from telethon import TelegramClient, events

from .saver import MessageSaver, SaveResult

logger = logging.getLogger("sentinel.save.commands")

#: Upper bound for ``.save <n>`` to avoid runaway downloads.
_MAX_RECENT = 100


def help_section() -> str:
    """Return the compact Save help block for the ``.help`` menu."""
    return "\n".join(
        [
            "💾 Save",
            ".save           (reply) → Saved Messages",
            ".save <n>       last n messages of this chat",
        ]
    )


class SaveCommands:
    """Register and serve the ``.save`` command."""

    def __init__(
        self,
        client: TelegramClient,
        saver: MessageSaver,
        *,
        prefix: str = ".",
    ) -> None:
        """Initialise the handler.

        Args:
            client: The Telethon client.
            saver: The message saver.
            prefix: Command prefix (default ``"."``).
        """
        self._client = client
        self._saver = saver
        self._prefix = prefix

    def register(self) -> None:
        """Attach the ``.save`` handler (owner only)."""
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_save,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}save(?:\s+(\d+))?$"),
        )
        logger.info("Save command registered (prefix=%r)", self._prefix)

    async def _on_save(self, event: events.NewMessage.Event) -> None:
        """Dispatch ``.save`` / ``.save <n>``, never crashing."""
        if not event.out:
            return
        try:
            count_arg = event.pattern_match.group(1)
            if count_arg:
                await self._cmd_recent(event, int(count_arg))
            else:
                await self._cmd_single(event)
        except Exception:  # noqa: BLE001 - save must not crash the bot
            logger.exception("Save command failed")
            await self._edit(event, "⚠ Save Error")

    async def _cmd_single(self, event: events.NewMessage.Event) -> None:
        """Save the replied-to message (or its album)."""
        if not event.is_reply:
            await self._edit(event, "⚠ Reply to a message to save it")
            return
        replied = await event.get_reply_message()
        if replied is None:
            await self._edit(event, "⚠ Message unavailable (deleted?)")
            return
        await self._edit(event, "💾 Saving…")
        result = await self._saver.save_single(replied)
        await self._edit(event, self._summary(result))

    async def _cmd_recent(self, event: events.NewMessage.Event, count: int) -> None:
        """Save the last ``count`` messages of the current chat."""
        if count < 1:
            await self._edit(event, "⚠ Count must be ≥ 1")
            return
        count = min(count, _MAX_RECENT)
        await self._edit(event, f"💾 Saving last {count}…")
        result = await self._saver.save_recent(
            event.chat_id, count, exclude_ids=(int(event.message.id),)
        )
        await self._edit(event, self._summary(result))

    @staticmethod
    def _summary(result: SaveResult) -> str:
        """Render a :class:`SaveResult` as a short status line."""
        if result.saved and not result.failed:
            return f"✓ Saved {result.saved} → Saved Messages"
        if result.saved and result.failed:
            return f"✓ Saved {result.saved}, {result.failed} failed"
        return f"⚠ {result.note or 'Nothing saved'}"

    async def _edit(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text, link_preview=False)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .save message")
