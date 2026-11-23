"""Live chat exporter.

Pulls a chat's history directly from Telegram and writes TXT + JSON files. File
I/O runs off the event loop via ``asyncio.to_thread``; Telethon's
``iter_messages`` handles FloodWait. The exporter is independent of the Archive
database — it always queries Telegram.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.errors import FloodWaitError

logger = logging.getLogger("sentinel.export.exporter")

#: Export modes.
MODE_FULL = "full"
MODE_TEXT = "text"
MODE_MEDIA = "media"


@dataclass(slots=True)
class ExportConfig:
    """Configuration for chat export.

    Attributes:
        exports_dir: Directory where export files are written.
        max_messages: Safety cap on messages pulled per export (0 = unlimited).
    """

    exports_dir: str = os.path.join("data", "exports")
    max_messages: int = 0

    @classmethod
    def from_env(cls) -> "ExportConfig":
        """Build configuration from environment variables."""
        try:
            cap = int(os.environ.get("EXPORT_MAX_MESSAGES", "0"))
        except ValueError:
            cap = 0
        return cls(
            exports_dir=os.environ.get("EXPORTS_PATH", os.path.join("data", "exports")),
            max_messages=cap,
        )


@dataclass(slots=True)
class ExportResult:
    """The outcome of an export."""

    chat_id: int
    chat_name: str
    count: int
    txt_path: str
    json_path: str
    files: list[str] = field(default_factory=list)


class ChatExporter:
    """Exports a chat's history to TXT/JSON files."""

    def __init__(self, client: TelegramClient, config: ExportConfig) -> None:
        """Initialise the exporter.

        Args:
            client: The Telethon client.
            config: Export configuration.
        """
        self._client = client
        self._config = config

    async def export(self, entity: object, *, mode: str = MODE_FULL) -> ExportResult:
        """Export ``entity``'s history in the requested mode.

        Args:
            entity: The resolved chat entity.
            mode: One of ``full``/``text``/``media``.

        Returns:
            An :class:`ExportResult` with the generated file paths.
        """
        chat_id = tl_utils.get_peer_id(entity)
        chat_name = tl_utils.get_display_name(entity) or str(chat_id)
        os.makedirs(self._config.exports_dir, exist_ok=True)

        records = await self._collect(entity, chat_id, mode)

        base = f"chat_export_{chat_id}"
        txt_path = os.path.join(self._config.exports_dir, base + ".txt")
        json_path = os.path.join(self._config.exports_dir, base + ".json")
        await asyncio.to_thread(self._write_txt, txt_path, chat_name, records)
        await asyncio.to_thread(self._write_json, json_path, chat_id, chat_name, records)

        logger.info("Exported %d messages from %s (mode=%s)", len(records), chat_name, mode)
        return ExportResult(
            chat_id=chat_id,
            chat_name=chat_name,
            count=len(records),
            txt_path=txt_path,
            json_path=json_path,
            files=[txt_path, json_path],
        )

    async def _collect(self, entity: object, chat_id: int, mode: str) -> list[dict]:
        """Pull and filter messages for the requested mode."""
        limit = self._config.max_messages or None
        records: list[dict] = []
        try:
            async for message in self._client.iter_messages(entity, limit=limit):
                has_media = bool(getattr(message, "media", None))
                text = getattr(message, "message", None) or ""
                if mode == MODE_TEXT and (has_media or not text):
                    continue
                if mode == MODE_MEDIA and not has_media:
                    continue
                records.append(self._record(message))
        except FloodWaitError as exc:
            logger.warning("Export FloodWait %ss for %s", exc.seconds, chat_id)
            await asyncio.sleep(int(exc.seconds) + 1)
        except Exception:  # noqa: BLE001 - export is best-effort
            logger.exception("Export collection failed for %s", chat_id)
        records.reverse()  # oldest first
        return records

    @staticmethod
    def _record(message: object) -> dict:
        """Build a serialisable record from a message."""
        sender = getattr(message, "sender", None)
        username = getattr(sender, "username", None)
        sender_name = (
            f"@{username}" if username else (tl_utils.get_display_name(sender) or None)
        )
        date = getattr(message, "date", None)
        media = getattr(message, "media", None)
        return {
            "message_id": int(message.id),
            "sender_id": getattr(sender, "id", None),
            "sender_name": sender_name,
            "date": date.isoformat() if date else None,
            "text": getattr(message, "message", None) or "",
            "media_type": type(media).__name__ if media else None,
        }

    @staticmethod
    def _write_txt(path: str, chat_name: str, records: list[dict]) -> None:
        """Write a human-readable TXT export."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"Sentinel Chat Export — {chat_name}\n")
            fh.write("=" * 50 + "\n\n")
            for r in records:
                date = r["date"] or "?"
                sender = r["sender_name"] or str(r["sender_id"])
                fh.write(f"[{date}]\n{sender}:\n{r['text']}\n")
                if r["media_type"]:
                    fh.write(f"<media: {r['media_type']}>\n")
                fh.write("\n")

    @staticmethod
    def _write_json(path: str, chat_id: int, chat_name: str, records: list[dict]) -> None:
        """Write a structured JSON export."""
        payload = {
            "chat_id": chat_id,
            "chat_name": chat_name,
            "count": len(records),
            "messages": records,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)


def _now_stamp() -> str:
    """Return a compact UTC timestamp (unused placeholder for parity)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
