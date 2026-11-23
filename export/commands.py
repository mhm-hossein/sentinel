"""``.export`` command handler for live chat export (owner only).

Forms::

    .export                 export the current chat (full)
    .export <target>        export a chat by username/id (full)
    .export text <target>   text-only
    .export media <target>  media-only

Files are delivered to Saved Messages and the global exports counter is bumped.
"""

from __future__ import annotations

import logging
import re

from telethon import TelegramClient, events

from core.store import COUNTER_EXPORTS, SharedStore

from .exporter import MODE_FULL, MODE_MEDIA, MODE_TEXT, ChatExporter

logger = logging.getLogger("sentinel.export.commands")

_SAVED = "me"
_MODES = {MODE_FULL, MODE_TEXT, MODE_MEDIA}


def help_section() -> str:
    """Return the compact Export help block for the ``.help`` menu."""
    return "\n".join(
        [
            "📤 Export",
            ".export [target]        (current chat if blank)",
            ".export text <target>",
            ".export media <target>",
        ]
    )


class ExportCommands:
    """Register and serve the ``.export`` command."""

    def __init__(
        self,
        client: TelegramClient,
        exporter: ChatExporter,
        store: SharedStore,
        *,
        prefix: str = ".",
    ) -> None:
        """Initialise the handler.

        Args:
            client: The Telethon client.
            exporter: The live chat exporter.
            store: Shared store (exports counter).
            prefix: Command prefix.
        """
        self._client = client
        self._exporter = exporter
        self._store = store
        self._prefix = prefix

    def register(self) -> None:
        """Attach the ``.export`` handler (owner only)."""
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_export,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}export(?:\s+(.*))?$"),
        )
        logger.info("Export .export command registered")

    async def _on_export(self, event: events.NewMessage.Event) -> None:
        """Parse args, run the export and deliver the files, never crashing."""
        try:
            raw = (event.pattern_match.group(1) or "").strip()
            mode, target = self._parse(raw)

            entity = await self._resolve(event, target)
            await self._edit(event, "📤 Exporting…")
            result = await self._exporter.export(entity, mode=mode)
            if result.count == 0:
                await self._edit(event, "⚠ Nothing to export for this mode")
                return

            await self._client.send_file(
                _SAVED,
                result.files,
                caption=f"📤 {result.chat_name} — {result.count} messages ({mode})",
            )
            await self._store.incr(COUNTER_EXPORTS)
            await self._edit(
                event, f"✓ Exported {result.count} messages → Saved Messages"
            )
        except Exception:  # noqa: BLE001 - export must not crash the bot
            logger.exception("Export command failed")
            await self._edit(event, "⚠ Export Error")

    def _parse(self, raw: str) -> tuple[str, str]:
        """Split ``raw`` into ``(mode, target)``.

        The first token is a mode if it is one of full/text/media; otherwise it
        is part of the target and the mode defaults to full.
        """
        if not raw:
            return MODE_FULL, ""
        parts = raw.split(maxsplit=1)
        if parts[0].lower() in _MODES:
            return parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        return MODE_FULL, raw

    async def _resolve(self, event: events.NewMessage.Event, target: str) -> object:
        """Resolve the export target (explicit, replied user, or current chat)."""
        if target:
            key: object = int(target) if target.lstrip("-").isdigit() else target
            return await self._client.get_entity(key)
        if event.is_reply:
            replied = await event.get_reply_message()
            sender = await replied.get_sender() if replied else None
            if sender is not None:
                return sender
        return await event.get_chat()

    async def _edit(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .export message")
