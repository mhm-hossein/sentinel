"""Message logger service: capture, manage targets and export.

Registers a message handler that records both incoming and outgoing messages
from targeted chats, plus a ``.logger`` command for managing targets and
exporting captured history to TXT/JSON files sent to Saved Messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon import utils as tl_utils

from core.store import COUNTER_EXPORTS, COUNTER_MESSAGES_LOGGED, SharedStore

from .database import LoggedMessage, LoggerDatabase

logger = logging.getLogger("sentinel.logger.service")

_SAVED = "me"


@dataclass(slots=True)
class LoggerConfig:
    """Configuration for the logger module.

    Attributes:
        exports_dir: Directory where export files are written.
    """

    exports_dir: str = os.path.join("data", "exports")

    @classmethod
    def from_env(cls) -> "LoggerConfig":
        """Build configuration from environment variables."""
        return cls(exports_dir=os.environ.get("EXPORTS_PATH", os.path.join("data", "exports")))


def help_section() -> str:
    """Return the compact Logger help block for the ``.help`` menu."""
    return "\n".join(
        [
            "🧾 Logger",
            ".logger start [chat]",
            ".logger stop [chat]",
            ".logger status",
            ".logger export [chat]",
        ]
    )


class LoggerService:
    """Owns logger state, the capture handler and the ``.logger`` command."""

    def __init__(
        self,
        client: TelegramClient,
        database: LoggerDatabase,
        store: SharedStore,
        config: LoggerConfig,
        *,
        prefix: str = ".",
    ) -> None:
        """Initialise the service.

        Args:
            client: The Telethon client.
            database: Logger database.
            store: Shared store (statistics counters).
            config: Logger configuration.
            prefix: Command prefix.
        """
        self._client = client
        self._db = database
        self._store = store
        self._config = config
        self._prefix = prefix
        self._targets: set[int] = set()

    async def start(self) -> None:
        """Connect the DB, create the exports dir and load active targets."""
        await self._db.connect()
        os.makedirs(self._config.exports_dir, exist_ok=True)
        self._targets = {chat_id for chat_id, _ in await self._db.list_targets()}
        logger.info("Logger started with %d target(s)", len(self._targets))

    def register(self) -> None:
        """Attach the capture handler and the ``.logger`` command."""
        # No incoming= filter: the owner expects full conversations,
        # including their own messages in the target chat.
        self._client.add_event_handler(self._on_message, events.NewMessage())
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_logger_cmd,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}logger(?:\s+(.*))?$"),
        )
        logger.info("Logger handlers registered")

    def stop(self) -> None:
        """Unregister all event handlers (call before DB close on shutdown)."""
        for cb in (self._on_message, self._on_logger_cmd):
            try:
                self._client.remove_event_handler(cb)
            except Exception:  # noqa: BLE001 - best-effort unwind
                pass

    async def close(self) -> None:
        """Alias for :meth:`stop` so teardown closeables can hold the service."""
        self.stop()

    # ------------------------------------------------------------------ #
    # Capture
    # ------------------------------------------------------------------ #
    async def _on_message(self, event: events.NewMessage.Event) -> None:
        """Record a message if its chat is a logging target."""
        try:
            if int(event.chat_id) not in self._targets:
                return
            message = event.message
            sender = await event.get_sender()
            await self._db.log_message(
                LoggedMessage(
                    chat_id=int(event.chat_id),
                    message_id=int(message.id),
                    sender_id=getattr(sender, "id", None),
                    username=getattr(sender, "username", None),
                    date=message.date.timestamp() if message.date else 0.0,
                    text=getattr(message, "message", None) or "",
                    media_type=_media_type(message),
                )
            )
            await self._store.incr(COUNTER_MESSAGES_LOGGED)
        except Exception:  # noqa: BLE001 - logging must not crash the bot
            logger.exception("Logger capture error")

    # ------------------------------------------------------------------ #
    # Command
    # ------------------------------------------------------------------ #
    async def _on_logger_cmd(self, event: events.NewMessage.Event) -> None:
        """Dispatch a ``.logger`` subcommand."""
        try:
            raw = (event.pattern_match.group(1) or "").strip()
            parts = raw.split()
            sub = parts[0].lower() if parts else "status"
            arg = parts[1] if len(parts) > 1 else ""

            if sub == "start":
                await self._cmd_start(event, arg)
            elif sub == "stop":
                await self._cmd_stop(event, arg)
            elif sub == "status":
                await self._cmd_status(event)
            elif sub == "export":
                await self._cmd_export(event, arg)
            else:
                await self._edit(event, "Usage: .logger start|stop|status|export [chat]")
        except Exception:  # noqa: BLE001 - never crash on command errors
            logger.exception("Logger command error")
            await self._edit(event, "⚠ Logger Error")

    async def _cmd_start(self, event: events.NewMessage.Event, arg: str) -> None:
        """Start logging a chat (current, replied user, or explicit target)."""
        chat_id, title = await self._resolve_target(event, arg)
        await self._db.add_target(chat_id, title)
        self._targets.add(chat_id)
        logger.info("Logger started for %s (%s)", title, chat_id)
        await self._edit(event, f"🧾 Logging: {title}")

    async def _cmd_stop(self, event: events.NewMessage.Event, arg: str) -> None:
        """Stop logging a chat."""
        chat_id, title = await self._resolve_target(event, arg)
        removed = await self._db.remove_target(chat_id)
        self._targets.discard(chat_id)
        logger.info("Logger stopped for %s (%s)", title, chat_id)
        await self._edit(event, f"🛑 Stopped: {title}" if removed else "⚠ Not logged")

    async def _cmd_status(self, event: events.NewMessage.Event) -> None:
        """Show active targets and total logged-message count."""
        targets = await self._db.list_targets()
        total = await self._db.count_messages()
        lines = ["🧾 Logger", "", f"Active: {len(targets)}", f"Logged: {total}"]
        if targets:
            lines.append("")
            for chat_id, title in targets:
                count = await self._db.count_messages(chat_id)
                lines.append(f"• {title} ({count})")
        await self._edit(event, "\n".join(lines))

    async def _cmd_export(self, event: events.NewMessage.Event, arg: str) -> None:
        """Export a chat's logged messages to TXT + JSON in Saved Messages."""
        chat_id, title = await self._resolve_target(event, arg)
        messages = await self._db.fetch_messages(chat_id)
        if not messages:
            await self._edit(event, "⚠ Nothing logged for this chat")
            return

        await self._edit(event, f"🧾 Exporting {len(messages)} messages…")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        base = re.sub(r"[^\w.-]", "_", f"log_{chat_id}_{stamp}")
        txt_path = os.path.join(self._config.exports_dir, base + ".txt")
        json_path = os.path.join(self._config.exports_dir, base + ".json")

        await asyncio.to_thread(self._write_txt, txt_path, title, messages)
        await asyncio.to_thread(self._write_json, json_path, chat_id, title, messages)

        await self._client.send_file(
            _SAVED, [txt_path, json_path], caption=f"🧾 Export: {title} ({len(messages)})"
        )
        await self._db.record_export(chat_id, "txt", len(messages))
        await self._db.record_export(chat_id, "json", len(messages))
        await self._store.incr(COUNTER_EXPORTS)
        await asyncio.to_thread(self._prune_exports, chat_id, self._config.exports_dir)
        logger.info("Exported %d messages for %s", len(messages), title)
        await self._edit(event, f"✓ Exported {len(messages)} → Saved Messages")

    #: How many export file-pairs to retain per chat on disk.
    _MAX_EXPORTS_PER_CHAT = 10

    @classmethod
    def _prune_exports(cls, chat_id: int, exports_dir: str = "") -> None:
        """Delete old export files for a chat, keeping the newest few."""
        directory = exports_dir or os.environ.get("EXPORTS_PATH", os.path.join("data", "exports"))
        try:
            names = sorted(
                n for n in os.listdir(directory)
                if n.startswith(f"log_{chat_id}_") and n.endswith((".txt", ".json"))
            )
        except OSError:
            return
        # Group by stamp: each export = one .txt + one .json sharing the base.
        bases = sorted({n.rsplit(".", 1)[0] for n in names})
        for stale in bases[: max(0, len(bases) - cls._MAX_EXPORTS_PER_CHAT)]:
            for ext in (".txt", ".json"):
                try:
                    os.remove(os.path.join(directory, stale + ext))
                except OSError:
                    pass

    # ------------------------------------------------------------------ #
    # File writers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _write_txt(path: str, title: str, messages: list[LoggedMessage]) -> None:
        """Write messages to a human-readable TXT file."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"Sentinel Log Export — {title}\n")
            fh.write("=" * 50 + "\n\n")
            for m in messages:
                ts = datetime.fromtimestamp(m.date, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                handle = f"@{m.username}" if m.username else str(m.sender_id)
                fh.write(f"[{ts}] {handle} (msg {m.message_id}) [{m.media_type}]\n")
                if m.text:
                    fh.write(m.text + "\n")
                fh.write("\n")

    @staticmethod
    def _write_json(path: str, chat_id: int, title: str, messages: list[LoggedMessage]) -> None:
        """Write messages to a structured JSON file."""
        payload = {
            "chat_id": chat_id,
            "title": title,
            "count": len(messages),
            "messages": [
                {
                    "message_id": m.message_id,
                    "sender_id": m.sender_id,
                    "username": m.username,
                    "date": datetime.fromtimestamp(m.date, tz=timezone.utc).isoformat(),
                    "text": m.text,
                    "media_type": m.media_type,
                }
                for m in messages
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _resolve_target(
        self, event: events.NewMessage.Event, arg: str
    ) -> tuple[int, str]:
        """Resolve a target chat from an explicit arg, a reply, or the current chat.

        Args:
            event: The command event.
            arg: Optional username or chat id.

        Returns:
            ``(chat_id, title)``.

        Raises:
            ValueError: If an explicit target cannot be resolved.
        """
        if arg:
            key: object = int(arg) if arg.lstrip("-").isdigit() else arg
            entity = await self._client.get_entity(key)
            return tl_utils.get_peer_id(entity), tl_utils.get_display_name(entity) or str(arg)

        if event.is_reply:
            replied = await event.get_reply_message()
            sender = await replied.get_sender() if replied else None
            if sender is not None:
                return tl_utils.get_peer_id(sender), tl_utils.get_display_name(sender) or "user"

        chat = await event.get_chat()
        return int(event.chat_id), tl_utils.get_display_name(chat) or str(event.chat_id)

    async def _edit(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit logger command message")


def _media_type(message: object) -> str:
    """Classify a message's media type for logging."""
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "document", None):
        return "document"
    if getattr(message, "sticker", None):
        return "sticker"
    return "text"
