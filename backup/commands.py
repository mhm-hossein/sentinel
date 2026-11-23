"""``.backup`` command handler.

Owner-only. ``create`` builds an archive and sends it to Saved Messages;
``list`` shows history; ``restore`` extracts an archive to a safe location.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from telethon import TelegramClient, events

from .manager import BackupManager

logger = logging.getLogger("backup.commands")

_SAVED = "me"


def help_section() -> str:
    """Return the compact Backup help block for the ``.help`` menu."""
    return "\n".join(
        [
            "💾 Backup",
            ".backup create",
            ".backup list",
            ".backup restore <id>",
        ]
    )


class BackupCommands:
    """Register and serve the ``.backup`` command."""

    def __init__(
        self,
        client: TelegramClient,
        manager: BackupManager,
        *,
        prefix: str = ".",
    ) -> None:
        """Initialise the command handler.

        Args:
            client: The Telethon client.
            manager: The backup manager.
            prefix: Command prefix.
        """
        self._client = client
        self._manager = manager
        self._prefix = prefix

    def register(self) -> None:
        """Attach the ``.backup`` handler (owner only)."""
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_backup,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}backup(?:\s+(.*))?$"),
        )
        logger.info("Backup .backup command registered")

    async def _on_backup(self, event: events.NewMessage.Event) -> None:
        """Dispatch a ``.backup`` subcommand, never crashing."""
        try:
            raw = (event.pattern_match.group(1) or "").strip()
            parts = raw.split()
            sub = parts[0].lower() if parts else "list"
            arg = parts[1] if len(parts) > 1 else ""

            if sub == "create":
                await self._cmd_create(event)
            elif sub == "list":
                await self._cmd_list(event)
            elif sub == "restore":
                await self._cmd_restore(event, arg)
            else:
                await self._edit(event, "Usage: .backup create|list|restore <id>")
        except Exception:  # noqa: BLE001 - backup must not crash the bot
            logger.exception("Backup command failed")
            await self._edit(event, "⚠ Backup Error")

    async def _cmd_create(self, event: events.NewMessage.Event) -> None:
        """Create a backup and mirror it to Saved Messages."""
        await self._edit(event, "💾 Creating backup…")
        record = await self._manager.create()
        try:
            await self._client.send_file(
                _SAVED,
                record.path,
                caption=f"💾 {record.filename} ({_mb(record.size)} MB)",
            )
        except Exception:  # noqa: BLE001 - the archive still exists locally
            logger.exception("Failed to mirror backup to Saved Messages")
        await self._edit(event, f"✓ Backup: {record.filename} ({_mb(record.size)} MB)")

    async def _cmd_list(self, event: events.NewMessage.Event) -> None:
        """List backup history."""
        records = await self._manager.list_backups()
        if not records:
            await self._edit(event, "💾 Backups\n\nNone")
            return
        lines = ["💾 Backups", ""]
        for r in records:
            ts = datetime.fromtimestamp(r.created_at, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
            lines.append(f"#{r.id} | {r.filename} | {_mb(r.size)} MB | {ts}")
        lines += ["", f"Total: {len(records)}"]
        await self._edit(event, "\n".join(lines))

    async def _cmd_restore(self, event: events.NewMessage.Event, arg: str) -> None:
        """Restore a backup to a safe directory (non-destructive)."""
        if not arg:
            await self._edit(event, "Usage: .backup restore <id>")
            return
        target = await self._manager.restore(arg)
        await self._edit(event, f"✓ Restored to: {target}\n(restart to apply DB)")

    @staticmethod
    async def _edit(event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .backup message")


def _mb(num_bytes: int) -> str:
    """Format a byte count as megabytes with one decimal."""
    return f"{num_bytes / (1024 ** 2):.1f}"
