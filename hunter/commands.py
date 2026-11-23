"""``.hunter`` command handler for Username Hunter (owner only).

Subcommands: ``add``, ``remove``, ``list``, ``status``, ``check``, ``export``.
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

from telethon import TelegramClient, events

from core.store import (
    COUNTER_HUNTER_CHECKS,
    COUNTER_HUNTER_CLAIMS,
    COUNTER_HUNTER_FAILURES,
    COUNTER_HUNTER_SUCCESSES,
    SharedStore,
)

from .database import HunterDatabase
from .hunter import UsernameHunter
from .models import HunterEvent
from .utils import is_valid_username, normalise_username

_SAVED = "me"

logger = logging.getLogger("sentinel.hunter.commands")


def help_section() -> str:
    """Return the compact Hunter help block for the ``.help`` menu."""
    return "\n".join(
        [
            "🎯 Username Hunter",
            ".hunter add <username>",
            ".hunter remove <username>",
            ".hunter list | status",
            ".hunter check <username>",
            ".hunter export   (targets+events → Saved)",
        ]
    )


class HunterCommands:
    """Register and serve the ``.hunter`` command."""

    def __init__(
        self,
        client: TelegramClient,
        hunter: UsernameHunter,
        database: HunterDatabase,
        store: SharedStore,
        *,
        db_path: str,
        exports_dir: str = os.path.join("data", "exports"),
        prefix: str = ".",
        plan: object | None = None,
    ) -> None:
        """Initialise the handler.

        Args:
            client: The Telethon client.
            hunter: The username hunter.
            database: Hunter database (reads for list/status/export).
            store: Shared store (counter readouts for status).
            db_path: Database file path (status size readout).
            exports_dir: Directory for export files.
            prefix: Command prefix.
            plan: Optional subscription plan for quota enforcement (``None`` =
                unlimited / unchanged behavior).
        """
        self._client = client
        self._hunter = hunter
        self._db = database
        self._store = store
        self._db_path = db_path
        self._exports_dir = exports_dir
        self._prefix = prefix
        self._plan = plan

    def register(self) -> None:
        """Attach the ``.hunter`` handler (owner only)."""
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_hunter,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}hunter(?:\s+(.*))?$"),
        )
        logger.info("Hunter .hunter command registered")

    async def _on_hunter(self, event: events.NewMessage.Event) -> None:
        """Dispatch a ``.hunter`` subcommand, never crashing."""
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
            elif sub == "check":
                await self._cmd_check(event, arg)
            elif sub == "export":
                await self._cmd_export(event)
            else:
                await self._edit(
                    event, "Usage: .hunter add|remove|list|status|check|export"
                )
        except Exception:  # noqa: BLE001 - hunter must not crash the bot
            logger.exception("Hunter command failed")
            await self._edit(event, "⚠ Hunter Error")

    # ------------------------------------------------------------------ #
    # Subcommands
    # ------------------------------------------------------------------ #
    async def _cmd_add(self, event: events.NewMessage.Event, arg: str) -> None:
        """Begin hunting a username."""
        username = normalise_username(arg)
        if not username:
            await self._edit(event, "Usage: .hunter add <username>")
            return
        if not is_valid_username(username):
            await self._edit(event, f"⚠ Invalid username: {username}")
            return
        from core.plan import check_limit

        limit_msg = check_limit(self._plan, "hunter_targets_max", await self._db.count_targets())
        if limit_msg:
            await self._edit(event, f"⚠ {limit_msg}")
            return
        target = await self._db.add_target(
            username, self._hunter.config.interval
        )
        await self._edit(
            event,
            f"🎯 Hunting @{target.username}\nInterval: {target.check_interval}s",
        )

    async def _cmd_remove(self, event: events.NewMessage.Event, arg: str) -> None:
        """Stop hunting a username."""
        username = normalise_username(arg)
        if not username:
            await self._edit(event, "Usage: .hunter remove <username>")
            return
        removed = await self._db.remove_target(username)
        await self._edit(
            event, f"🗑 Removed: @{username}" if removed else "⚠ Not hunted"
        )

    async def _cmd_list(self, event: events.NewMessage.Event) -> None:
        """List hunted usernames with status, last check and interval."""
        targets = await self._db.list_targets()
        if not targets:
            await self._edit(event, "🎯 Username Hunter\n\nNo usernames hunted")
            return
        lines = ["🎯 Username Hunter", ""]
        for t in targets:
            status = "active" if t.enabled else "stopped"
            lines += [
                f"@{t.username}",
                f"  {status} | every {t.check_interval}s | last: {_ago(t.last_check)}",
            ]
        lines += ["", f"Total: {len(targets)}"]
        await self._edit(event, "\n".join(lines))

    async def _cmd_status(self, event: events.NewMessage.Event) -> None:
        """Show a global Hunter overview."""
        targets = await self._db.count_targets()
        checks = await self._store.get_counter(COUNTER_HUNTER_CHECKS)
        claims = await self._store.get_counter(COUNTER_HUNTER_CLAIMS)
        successes = await self._store.get_counter(COUNTER_HUNTER_SUCCESSES)
        failures = await self._store.get_counter(COUNTER_HUNTER_FAILURES)
        last_check = await self._db.last_check()
        lines = [
            "🎯 Hunter Status",
            "",
            f"Tracked Usernames: {targets}",
            f"Total Checks: {checks}",
            f"Claim Attempts: {claims}",
            f"Successes: {successes}",
            f"Failures: {failures}",
            f"Claim Target: {self._hunter.config.claim_target}",
            f"Last Check: {_ago(last_check) if last_check else '-'}",
            f"Database Size: {_db_size(self._db_path)}",
        ]
        await self._edit(event, "\n".join(lines))

    async def _cmd_check(self, event: events.NewMessage.Event, arg: str) -> None:
        """Perform a one-off live availability check."""
        username = normalise_username(arg)
        if not username:
            await self._edit(event, "Usage: .hunter check <username>")
            return
        await self._edit(event, f"🎯 Checking @{username}…")
        status = await self._hunter.check_username(username)
        await self._store.incr(COUNTER_HUNTER_CHECKS)
        owner = status.owner_type if not status.available else "-"
        lines = [
            f"🎯 @{username}",
            "",
            f"Current Status: {status.status_text}",
            f"Owner Type: {owner}",
            f"Availability: {'✅ Available' if status.available else '❌ Taken'}",
        ]
        if status.detail and not status.available:
            lines.append(f"Detail: {status.detail}")
        await self._edit(event, "\n".join(lines))

    async def _cmd_export(self, event: events.NewMessage.Event) -> None:
        """Export all targets + events to TXT + JSON → Saved."""
        targets = await self._db.list_targets()
        events_ = await self._db.get_events()
        await self._edit(
            event, f"🎯 Exporting {len(targets)} target(s), {len(events_)} event(s)…"
        )
        os.makedirs(self._exports_dir, exist_ok=True)
        txt_path = os.path.join(self._exports_dir, "hunter_export.txt")
        json_path = os.path.join(self._exports_dir, "hunter_export.json")
        await asyncio.to_thread(self._write_txt, txt_path, targets, events_)
        await asyncio.to_thread(self._write_json, json_path, targets, events_)

        await self._client.send_file(
            _SAVED,
            [txt_path, json_path],
            caption=f"🎯 Hunter export: {len(targets)} targets / {len(events_)} events",
        )
        logger.info("Hunter exported %d targets, %d events", len(targets), len(events_))
        await self._edit(event, "✓ Exported → Saved Messages")

    @staticmethod
    def _write_txt(path: str, targets, events_: list[HunterEvent]) -> None:
        """Write targets and events to a human-readable TXT file."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Sentinel Username Hunter Export\n")
            fh.write("=" * 50 + "\n\n")
            fh.write("Targets:\n")
            if not targets:
                fh.write("  (none)\n")
            for t in targets:
                state = "active" if t.enabled else "stopped"
                fh.write(
                    f"  @{t.username} — {state}, every {t.check_interval}s, "
                    f"last check {_fmt(t.last_check)}\n"
                )
            fh.write("\nEvents:\n")
            if not events_:
                fh.write("  (none)\n")
            for e in events_:
                fh.write(
                    f"  [{_fmt(e.created_at)}] @{e.username} {e.event_type}"
                    f"{(' — ' + e.details) if e.details else ''}\n"
                )

    @staticmethod
    def _write_json(path: str, targets, events_: list[HunterEvent]) -> None:
        """Write targets and events to a structured JSON file."""
        payload = {
            "targets": [
                {
                    "username": t.username,
                    "enabled": t.enabled,
                    "check_interval": t.check_interval,
                    "last_check": _iso(t.last_check),
                    "created_at": _iso(t.created_at),
                }
                for t in targets
            ],
            "events": [
                {
                    "username": e.username,
                    "event_type": e.event_type,
                    "details": e.details,
                    "created_at": _iso(e.created_at),
                }
                for e in events_
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    async def _edit(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text, link_preview=False)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .hunter message")


def _ago(timestamp: float) -> str:
    """Format how long ago a timestamp was, compactly."""
    if not timestamp:
        return "never"
    delta = int(time.time() - timestamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fmt(timestamp: float) -> str:
    """Format a timestamp as a UTC datetime string."""
    if not timestamp:
        return "never"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _iso(timestamp: float) -> str | None:
    """Return an ISO-8601 UTC string, or ``None`` for a zero timestamp."""
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _db_size(path: str) -> str:
    """Return the database file size as a compact MB string."""
    try:
        size = os.path.getsize(path)
        return f"{size / (1024 ** 2):.1f} MB"
    except OSError:
        return "n/a"
