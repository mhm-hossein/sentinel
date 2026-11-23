"""``.archive`` command handler for Archive Watch (owner only).

Supports adding the current chat (no argument), or an explicit username / chat
id / replied user, plus remove/list/sync/info/status. All replies edit the
command message in place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient, events
from telethon import utils as tl_utils

from core.store import COUNTER_EXPORTS, SharedStore

from .archive_manager import ArchiveManager
from .database import ArchiveDatabase
from .models import ArchiveMessage

_SAVED = "me"

logger = logging.getLogger("sentinel.archive.commands")


def help_section() -> str:
    """Return the compact Archive help block for the ``.help`` menu."""
    return "\n".join(
        [
            "📦 Archive",
            ".archive add [target]   (current chat if blank)",
            ".archive remove <target>",
            ".archive list | sync | status",
            ".archive info <target>",
            ".archive export <target>   (stored messages → Saved)",
        ]
    )


class ArchiveCommands:
    """Register and serve the ``.archive`` command."""

    def __init__(
        self,
        client: TelegramClient,
        manager: ArchiveManager,
        database: ArchiveDatabase,
        *,
        db_path: str,
        store: SharedStore,
        exports_dir: str = os.path.join("data", "exports"),
        prefix: str = ".",
        plan: object | None = None,
    ) -> None:
        """Initialise the handler.

        Args:
            client: The Telethon client.
            manager: The archive manager.
            database: Archive database (reads for list/info/status/export).
            db_path: Database file path (for the status size readout).
            store: Shared store (exports counter).
            exports_dir: Directory for archive export files.
            prefix: Command prefix.
            plan: Optional subscription plan for quota enforcement (``None`` =
                unlimited / unchanged behavior).
        """
        self._client = client
        self._manager = manager
        self._db = database
        self._db_path = db_path
        self._store = store
        self._exports_dir = exports_dir
        self._prefix = prefix
        self._plan = plan

    def register(self) -> None:
        """Attach the ``.archive`` handler (owner only)."""
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_archive,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}archive(?:\s+(.*))?$"),
        )
        logger.info("Archive .archive command registered")

    async def _on_archive(self, event: events.NewMessage.Event) -> None:
        """Dispatch an ``.archive`` subcommand, never crashing."""
        try:
            raw = (event.pattern_match.group(1) or "").strip()
            parts = raw.split()
            sub = parts[0].lower() if parts else "list"
            arg = parts[1] if len(parts) > 1 else ""

            if sub == "add":
                await self._cmd_add(event, arg)
            elif sub in ("remove", "rm", "del"):
                await self._cmd_remove(event, arg)
            elif sub == "list":
                await self._cmd_list(event)
            elif sub == "sync":
                await self._cmd_sync(event)
            elif sub == "info":
                await self._cmd_info(event, arg)
            elif sub == "status":
                await self._cmd_status(event)
            elif sub == "export":
                await self._cmd_export(event, arg)
            else:
                await self._edit(
                    event, "Usage: .archive add|remove|list|sync|info|export|status"
                )
        except Exception:  # noqa: BLE001 - archive must not crash the bot
            logger.exception("Archive command failed")
            await self._edit(event, "⚠ Archive Error")

    # ------------------------------------------------------------------ #
    # Subcommands
    # ------------------------------------------------------------------ #
    async def _cmd_add(self, event: events.NewMessage.Event, arg: str) -> None:
        """Add the current chat (no arg) or an explicit target."""
        from core.plan import check_limit

        limit_msg = check_limit(self._plan, "archive_chats_max", await self._db.count_chats())
        if limit_msg:
            await self._edit(event, f"⚠ {limit_msg}")
            return
        await self._edit(event, "📦 Archiving…")
        entity, chat_id, name = await self._resolve(event, arg)
        chat, stored = await self._manager.add_chat(entity, chat_id, name)
        await self._edit(event, f"📦 Archived: {name}\nBackfilled: {stored} messages")

    async def _cmd_remove(self, event: events.NewMessage.Event, arg: str) -> None:
        """Remove a tracked chat."""
        chat_id, name = await self._resolve_chat_id(event, arg)
        removed = await self._manager.remove_chat(chat_id)
        await self._edit(
            event, f"🗑 Removed: {name or chat_id}" if removed else "⚠ Not archived"
        )

    async def _cmd_list(self, event: events.NewMessage.Event) -> None:
        """List tracked chats with stored counts, last sync and status."""
        chats = await self._db.list_chats()
        if not chats:
            await self._edit(event, "📦 Archive\n\nNo chats tracked")
            return
        lines = ["📦 Archive", ""]
        for chat in chats:
            count = await self._db.count_messages(chat.chat_id)
            status = "active" if chat.enabled else "paused"
            lines += [
                f"{chat.chat_name}",
                f"  ID: {chat.chat_id} | {count} msgs | {status}",
                f"  Last sync: {_ago(chat.updated_at)}",
            ]
        lines += ["", f"Total: {len(chats)}"]
        await self._edit(event, "\n".join(lines))

    async def _cmd_sync(self, event: events.NewMessage.Event) -> None:
        """Force an immediate sync of every tracked chat."""
        await self._edit(event, "📦 Syncing…")
        total = await self._manager.sync_all()
        await self._edit(event, f"✓ Sync complete: +{total} new messages")

    async def _cmd_info(self, event: events.NewMessage.Event, arg: str) -> None:
        """Show details for one tracked chat."""
        chat_id, _ = await self._resolve_chat_id(event, arg)
        chat = await self._db.get_chat(chat_id)
        if chat is None:
            await self._edit(event, "⚠ Not archived")
            return
        count = await self._db.count_messages(chat_id)
        lines = [
            f"📦 {chat.chat_name}",
            "",
            f"Chat ID: {chat.chat_id}",
            f"Status: {'active' if chat.enabled else 'paused'}",
            f"Stored: {count}",
            f"Last message id: {chat.last_message_id}",
            f"Added: {_ago(chat.created_at)}",
            f"Last sync: {_ago(chat.updated_at)}",
        ]
        await self._edit(event, "\n".join(lines))

    async def _cmd_status(self, event: events.NewMessage.Event) -> None:
        """Show a global archive overview."""
        chats = await self._db.list_chats()
        total_msgs = await self._db.count_messages()
        last_sync = max((c.updated_at for c in chats), default=0.0)
        size = _db_size(self._db_path)
        lines = [
            "📦 Archive Status",
            "",
            f"Tracked Chats: {len(chats)}",
            f"Stored Messages: {total_msgs}",
            f"Database Size: {size}",
            f"Last Sync: {_ago(last_sync) if last_sync else '-'}",
        ]
        await self._edit(event, "\n".join(lines))

    async def _cmd_export(self, event: events.NewMessage.Event, arg: str) -> None:
        """Export a tracked chat's *stored* messages to TXT + JSON → Saved.

        Unlike ``.export`` (which pulls live from Telegram), this exports what
        Archive Watch has already stored in the database.
        """
        chat_id, _ = await self._resolve_chat_id(event, arg)
        chat = await self._db.get_chat(chat_id)
        if chat is None:
            await self._edit(event, "⚠ Not archived")
            return
        messages = await self._db.fetch_messages(chat_id)
        if not messages:
            await self._edit(event, "⚠ No stored messages for this chat")
            return

        await self._edit(event, f"📦 Exporting {len(messages)} stored messages…")
        os.makedirs(self._exports_dir, exist_ok=True)
        base = f"archive_export_{chat_id}"
        txt_path = os.path.join(self._exports_dir, base + ".txt")
        json_path = os.path.join(self._exports_dir, base + ".json")
        await asyncio.to_thread(self._write_txt, txt_path, chat.chat_name, messages)
        await asyncio.to_thread(self._write_json, json_path, chat_id, chat.chat_name, messages)

        await self._client.send_file(
            _SAVED,
            [txt_path, json_path],
            caption=f"📦 Archive export: {chat.chat_name} ({len(messages)})",
        )
        await self._store.incr(COUNTER_EXPORTS)
        logger.info("Archive exported %d messages for %s", len(messages), chat_id)
        await self._edit(event, f"✓ Exported {len(messages)} stored → Saved Messages")

    @staticmethod
    def _write_txt(path: str, chat_name: str, messages: list[ArchiveMessage]) -> None:
        """Write archived messages to a human-readable TXT file."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"Sentinel Archive Export — {chat_name}\n")
            fh.write("=" * 50 + "\n\n")
            for m in messages:
                ts = (
                    datetime.fromtimestamp(m.date, tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    if m.date
                    else "?"
                )
                sender = m.sender_name or str(m.sender_id)
                fh.write(f"[{ts}] {sender} (msg {m.message_id}) [{m.media_type}]\n")
                if m.text:
                    fh.write(m.text + "\n")
                fh.write("\n")

    @staticmethod
    def _write_json(
        path: str, chat_id: int, chat_name: str, messages: list[ArchiveMessage]
    ) -> None:
        """Write archived messages to a structured JSON file."""
        payload = {
            "chat_id": chat_id,
            "chat_name": chat_name,
            "count": len(messages),
            "messages": [
                {
                    "message_id": m.message_id,
                    "sender_id": m.sender_id,
                    "sender_name": m.sender_name,
                    "date": (
                        datetime.fromtimestamp(m.date, tz=timezone.utc).isoformat()
                        if m.date
                        else None
                    ),
                    "text": m.text,
                    "media_type": m.media_type,
                    "reply_to": m.reply_to,
                }
                for m in messages
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # Resolution helpers
    # ------------------------------------------------------------------ #
    async def _resolve(
        self, event: events.NewMessage.Event, arg: str
    ) -> tuple[object, int, str]:
        """Resolve to ``(entity, chat_id, name)`` from arg / reply / current."""
        if arg:
            key: object = int(arg) if arg.lstrip("-").isdigit() else arg
            return await self._manager.resolve(key)
        if event.is_reply:
            replied = await event.get_reply_message()
            sender = await replied.get_sender() if replied else None
            if sender is not None:
                return await self._manager.resolve(sender.id)
        return await self._manager.resolve(event.chat_id)

    async def _resolve_chat_id(
        self, event: events.NewMessage.Event, arg: str
    ) -> tuple[int, Optional[str]]:
        """Resolve to ``(chat_id, name)`` without requiring a live entity."""
        if arg and arg.lstrip("-").isdigit():
            return int(arg), arg
        if arg:
            _, chat_id, name = await self._manager.resolve(arg)
            return chat_id, name
        if event.is_reply:
            replied = await event.get_reply_message()
            sender = await replied.get_sender() if replied else None
            if sender is not None:
                return tl_utils.get_peer_id(sender), tl_utils.get_display_name(sender)
        return int(event.chat_id), None

    async def _edit(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .archive message")


def _ago(timestamp: float) -> str:
    """Format how long ago a timestamp was, compactly."""
    if not timestamp:
        return "-"
    delta = int(time.time() - timestamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _db_size(path: str) -> str:
    """Return the database file size as a compact MB string."""
    try:
        size = os.path.getsize(path)
        return f"{size / (1024 ** 2):.1f} MB"
    except OSError:
        return "n/a"
