"""``.ghost`` command handler for Ghost Tracker (owner only).

Subcommands: ``add``, ``remove``, ``list``, ``status``, ``info``, ``export``.
Targets are given as ``@username`` / numeric id, or taken from a replied user.
All replies edit the command message in place; exports are sent to Saved
Messages.
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

from .database import GhostDatabase
from .models import GhostHistoryEntry
from .tracker import GhostTracker
from .utils import field_label

_SAVED = "me"

logger = logging.getLogger("sentinel.ghost.commands")


def help_section() -> str:
    """Return the compact Ghost help block for the ``.help`` menu."""
    return "\n".join(
        [
            "👻 Ghost Tracker",
            ".ghost add <@user|id>",
            ".ghost remove <@user|id>",
            ".ghost list | status",
            ".ghost info <@user|id>",
            ".ghost export <@user|id>   (history → Saved)",
        ]
    )


class GhostCommands:
    """Register and serve the ``.ghost`` command."""

    def __init__(
        self,
        client: TelegramClient,
        tracker: GhostTracker,
        database: GhostDatabase,
        *,
        db_path: str,
        exports_dir: str = os.path.join("data", "exports"),
        prefix: str = ".",
        plan: object | None = None,
    ) -> None:
        """Initialise the handler.

        Args:
            client: The Telethon client.
            tracker: The Ghost tracker.
            database: Ghost database (reads for list/info/status/export).
            db_path: Database file path (status size readout).
            exports_dir: Directory for export files.
            prefix: Command prefix.
            plan: Optional subscription plan for quota enforcement (``None`` =
                unlimited / unchanged behavior).
        """
        self._client = client
        self._tracker = tracker
        self._db = database
        self._db_path = db_path
        self._exports_dir = exports_dir
        self._prefix = prefix
        self._plan = plan

    def register(self) -> None:
        """Attach the ``.ghost`` handler (owner only)."""
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_ghost,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}ghost(?:\s+(.*))?$"),
        )
        logger.info("Ghost .ghost command registered")

    async def _on_ghost(self, event: events.NewMessage.Event) -> None:
        """Dispatch a ``.ghost`` subcommand, never crashing."""
        if not event.out:
            return
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
            elif sub == "status":
                await self._cmd_status(event)
            elif sub == "info":
                await self._cmd_info(event, arg)
            elif sub == "export":
                await self._cmd_export(event, arg)
            else:
                await self._edit(
                    event, "Usage: .ghost add|remove|list|status|info|export <@user|id>"
                )
        except Exception:  # noqa: BLE001 - ghost must not crash the bot
            logger.exception("Ghost command failed")
            await self._edit(event, "⚠ Ghost Error")

    # ------------------------------------------------------------------ #
    # Subcommands
    # ------------------------------------------------------------------ #
    async def _cmd_add(self, event: events.NewMessage.Event, arg: str) -> None:
        """Begin monitoring a user (argument or replied user)."""
        key = await self._resolve_key(event, arg)
        if key is None:
            await self._edit(event, "Usage: .ghost add <@user|id>  (or reply to a user)")
            return
        from core.plan import check_limit

        limit_msg = check_limit(self._plan, "ghost_targets_max", await self._db.count_targets())
        if limit_msg:
            await self._edit(event, f"⚠ {limit_msg}")
            return
        await self._edit(event, "👻 Capturing profile…")
        target = await self._tracker.add_target(key)
        handle = f"@{target.username}" if target.username else (target.display_name or "")
        await self._edit(
            event,
            f"👻 Now tracking {handle}\nID: {target.user_id}",
        )

    async def _cmd_remove(self, event: events.NewMessage.Event, arg: str) -> None:
        """Stop monitoring a user."""
        user_id, label = await self._resolve_user_id(event, arg)
        if user_id is None:
            await self._edit(event, "Usage: .ghost remove <@user|id>")
            return
        removed = await self._tracker.remove_target(user_id)
        await self._edit(
            event, f"🗑 Untracked: {label or user_id}" if removed else "⚠ Not tracked"
        )

    async def _cmd_list(self, event: events.NewMessage.Event) -> None:
        """List monitored users: username, id, last check, changes, status."""
        targets = await self._db.list_targets()
        if not targets:
            await self._edit(event, "👻 Ghost Tracker\n\nNo users tracked")
            return
        lines = ["👻 Ghost Tracker", ""]
        for t in targets:
            handle = f"@{t.username}" if t.username else (t.display_name or "—")
            changes = await self._db.count_history(t.user_id)
            status = "active" if t.enabled else "paused"
            lines += [
                f"{handle}",
                f"  ID: {t.user_id} | {changes} changes | {status}",
                f"  Last check: {_ago(t.last_check)}",
            ]
        lines += ["", f"Total: {len(targets)}"]
        await self._edit(event, "\n".join(lines))

    async def _cmd_status(self, event: events.NewMessage.Event) -> None:
        """Show a global Ghost overview."""
        tracked = await self._db.count_targets()
        total_changes = await self._db.count_history()
        last_scan = await self._db.last_scan()
        size = _db_size(self._db_path)
        lines = [
            "👻 Ghost Status",
            "",
            f"Tracked Users: {tracked}",
            f"Total Changes: {total_changes}",
            f"Last Scan: {_ago(last_scan) if last_scan else '-'}",
            f"Database Size: {size}",
        ]
        await self._edit(event, "\n".join(lines))

    async def _cmd_info(self, event: events.NewMessage.Event, arg: str) -> None:
        """Show details and recent history for one monitored user."""
        user_id, _ = await self._resolve_user_id(event, arg)
        if user_id is None:
            await self._edit(event, "Usage: .ghost info <@user|id>")
            return
        target = await self._db.get_target(user_id)
        if target is None:
            await self._edit(event, "⚠ Not tracked")
            return
        history = await self._db.get_history(user_id)
        handle = f"@{target.username}" if target.username else (target.display_name or "—")
        lines = [
            f"👻 {handle}",
            "",
            f"User ID: {target.user_id}",
            f"Display: {target.display_name or '—'}",
            f"Status: {'active' if target.enabled else 'paused'}",
            f"Changes: {len(history)}",
            f"Added: {_ago(target.created_at)}",
            f"Last check: {_ago(target.last_check)}",
        ]
        if history:
            lines += ["", "Recent changes:"]
            for h in history[:5]:
                lines.append(
                    f"  • {field_label(h.field_name)}: "
                    f"{_short(h.old_value)} → {_short(h.new_value)} ({_ago(h.detected_at)})"
                )
        await self._edit(event, "\n".join(lines))

    async def _cmd_export(self, event: events.NewMessage.Event, arg: str) -> None:
        """Export a user's complete change history to TXT + JSON → Saved."""
        user_id, _ = await self._resolve_user_id(event, arg)
        if user_id is None:
            await self._edit(event, "Usage: .ghost export <@user|id>")
            return
        target = await self._db.get_target(user_id)
        if target is None:
            await self._edit(event, "⚠ Not tracked")
            return
        history = await self._db.get_history(user_id)

        await self._edit(event, f"👻 Exporting {len(history)} change(s)…")
        os.makedirs(self._exports_dir, exist_ok=True)
        base = f"ghost_export_{user_id}"
        txt_path = os.path.join(self._exports_dir, base + ".txt")
        json_path = os.path.join(self._exports_dir, base + ".json")
        handle = f"@{target.username}" if target.username else (target.display_name or str(user_id))
        await asyncio.to_thread(self._write_txt, txt_path, handle, user_id, history)
        await asyncio.to_thread(self._write_json, json_path, handle, user_id, history)

        await self._client.send_file(
            _SAVED,
            [txt_path, json_path],
            caption=f"👻 Ghost export: {handle} ({len(history)} changes)",
        )
        logger.info("Ghost exported %d changes for %s", len(history), user_id)
        await self._edit(event, f"✓ Exported {len(history)} change(s) → Saved Messages")

    @staticmethod
    def _write_txt(
        path: str, handle: str, user_id: int, history: list[GhostHistoryEntry]
    ) -> None:
        """Write change history to a human-readable TXT file."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"Sentinel Ghost Tracker — {handle} ({user_id})\n")
            fh.write("=" * 50 + "\n\n")
            if not history:
                fh.write("No changes recorded yet.\n")
                return
            for h in history:
                ts = (
                    datetime.fromtimestamp(h.detected_at, tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                    if h.detected_at
                    else "?"
                )
                fh.write(f"[{ts}] {field_label(h.field_name)} Changed\n")
                fh.write(f"  Old: {h.old_value if h.old_value not in (None, '') else '—'}\n")
                fh.write(f"  New: {h.new_value if h.new_value not in (None, '') else '—'}\n\n")

    @staticmethod
    def _write_json(
        path: str, handle: str, user_id: int, history: list[GhostHistoryEntry]
    ) -> None:
        """Write change history to a structured JSON file."""
        payload = {
            "user_id": user_id,
            "handle": handle,
            "count": len(history),
            "history": [
                {
                    "field_name": h.field_name,
                    "field_label": field_label(h.field_name),
                    "old_value": h.old_value,
                    "new_value": h.new_value,
                    "detected_at": (
                        datetime.fromtimestamp(h.detected_at, tz=timezone.utc).isoformat()
                        if h.detected_at
                        else None
                    ),
                }
                for h in history
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # Resolution helpers
    # ------------------------------------------------------------------ #
    async def _resolve_key(
        self, event: events.NewMessage.Event, arg: str
    ) -> Optional[object]:
        """Resolve an add-target key from arg or replied user."""
        if arg:
            cleaned = arg.lstrip("@")
            return int(cleaned) if cleaned.lstrip("-").isdigit() else cleaned
        if event.is_reply:
            replied = await event.get_reply_message()
            sender = await replied.get_sender() if replied else None
            if sender is not None:
                return sender.id
        return None

    async def _resolve_user_id(
        self, event: events.NewMessage.Event, arg: str
    ) -> tuple[Optional[int], Optional[str]]:
        """Resolve to ``(user_id, label)`` from arg or replied user."""
        if arg:
            cleaned = arg.lstrip("@")
            if cleaned.lstrip("-").isdigit():
                return int(cleaned), arg
            _, user_id, _, name = await self._tracker.resolve(cleaned)
            return user_id, name
        if event.is_reply:
            replied = await event.get_reply_message()
            sender = await replied.get_sender() if replied else None
            if sender is not None:
                return tl_utils.get_peer_id(sender), tl_utils.get_display_name(sender)
        return None, None

    async def _edit(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text, link_preview=False)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .ghost message")


def _short(value: Optional[str], limit: int = 24) -> str:
    """Truncate a value for compact inline display."""
    if value in (None, ""):
        return "—"
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
