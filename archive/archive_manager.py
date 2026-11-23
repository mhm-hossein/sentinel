"""Archive manager: backfill, incremental sync and target resolution.

Backfills a configurable slice of recent history when a chat is added, then
incrementally syncs only messages newer than the stored watermark. Telethon's
``iter_messages`` handles FloodWait internally; calls are additionally guarded
so a single failing chat never aborts a whole sync run.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.errors import FloodWaitError

from core.store import COUNTER_ARCHIVE_SYNCS, SharedStore

from .database import ArchiveDatabase
from .models import ArchiveChat
from .utils import to_archive_message

logger = logging.getLogger("sentinel.archive.manager")


@dataclass(slots=True)
class ArchiveConfig:
    """Configuration for Archive Watch.

    Attributes:
        initial_messages: How many recent messages to backfill on add.
        sync_interval_minutes: Minutes between automatic sync runs.
    """

    initial_messages: int = 1000
    sync_interval_minutes: int = 30

    @classmethod
    def from_env(cls) -> "ArchiveConfig":
        """Build configuration from environment variables."""
        return cls(
            initial_messages=_int_env("ARCHIVE_INITIAL_MESSAGES", 1000),
            sync_interval_minutes=_int_env("ARCHIVE_SYNC_INTERVAL_MINUTES", 30),
        )

    @property
    def sync_interval_seconds(self) -> int:
        """Sync interval expressed in seconds (minimum 60s)."""
        return max(60, self.sync_interval_minutes * 60)


class ArchiveManager:
    """Coordinates backfill and incremental sync of archived chats."""

    def __init__(
        self,
        client: TelegramClient,
        database: ArchiveDatabase,
        store: SharedStore,
        config: ArchiveConfig,
    ) -> None:
        """Initialise the manager.

        Args:
            client: The Telethon client.
            database: Archive persistence layer.
            store: Shared store (sync-run counter).
            config: Archive configuration.
        """
        self._client = client
        self._db = database
        self._store = store
        self._config = config

    @property
    def config(self) -> ArchiveConfig:
        """The active configuration."""
        return self._config

    # ------------------------------------------------------------------ #
    # Target resolution
    # ------------------------------------------------------------------ #
    async def resolve(self, key: object) -> tuple[object, int, str]:
        """Resolve an entity key to ``(entity, chat_id, name)``.

        Args:
            key: A username, numeric id, or entity.

        Returns:
            ``(entity, chat_id, display_name)``.
        """
        entity = await self._client.get_entity(key)
        chat_id = tl_utils.get_peer_id(entity)
        name = tl_utils.get_display_name(entity) or str(chat_id)
        return entity, chat_id, name

    # ------------------------------------------------------------------ #
    # Add / remove
    # ------------------------------------------------------------------ #
    async def add_chat(self, entity: object, chat_id: int, name: str) -> tuple[ArchiveChat, int]:
        """Add a chat and backfill its recent history.

        Args:
            entity: The resolved chat entity.
            chat_id: Canonical chat id.
            name: Display name.

        Returns:
            ``(chat, stored_count)``.
        """
        chat = await self._db.add_chat(chat_id, name)
        stored = await self._backfill(entity, chat_id, self._config.initial_messages)
        logger.info("Archive added %s (%s); backfilled %d messages", name, chat_id, stored)
        return await self._db.get_chat(chat_id) or chat, stored

    async def remove_chat(self, chat_id: int) -> bool:
        """Stop archiving a chat and drop its stored messages."""
        removed = await self._db.remove_chat(chat_id)
        if removed:
            logger.info("Archive removed chat %s", chat_id)
        return removed

    # ------------------------------------------------------------------ #
    # Sync
    # ------------------------------------------------------------------ #
    async def _backfill(self, entity: object, chat_id: int, limit: int) -> int:
        """Download up to ``limit`` recent messages and store them."""
        collected = []
        max_id = 0
        try:
            async for message in self._client.iter_messages(entity, limit=limit):
                collected.append(
                    to_archive_message(chat_id, message, getattr(message, "sender", None))
                )
                max_id = max(max_id, int(message.id))
        except FloodWaitError as exc:
            logger.warning("Backfill FloodWait %ss for %s", exc.seconds, chat_id)
            await asyncio.sleep(int(exc.seconds) + 1)
        except Exception:  # noqa: BLE001 - backfill is best-effort
            logger.exception("Backfill failed for %s", chat_id)

        stored = await self._db.store_messages(collected)
        if max_id:
            await self._db.set_watermark(chat_id, max_id)
        return stored

    async def sync_chat(self, chat: ArchiveChat) -> int:
        """Sync one chat: store only messages newer than its watermark.

        Args:
            chat: The tracked chat.

        Returns:
            The number of new messages stored.
        """
        if not chat.enabled:
            return 0
        try:
            entity = await self._client.get_entity(chat.chat_id)
        except Exception:  # noqa: BLE001 - chat gone/inaccessible
            logger.warning("Archive sync: cannot resolve chat %s", chat.chat_id)
            return 0

        collected = []
        max_id = chat.last_message_id
        try:
            async for message in self._client.iter_messages(
                entity, min_id=chat.last_message_id
            ):
                collected.append(
                    to_archive_message(chat.chat_id, message, getattr(message, "sender", None))
                )
                max_id = max(max_id, int(message.id))
        except FloodWaitError as exc:
            logger.warning("Sync FloodWait %ss for %s", exc.seconds, chat.chat_id)
            await asyncio.sleep(int(exc.seconds) + 1)
        except Exception:  # noqa: BLE001 - one chat must not break the run
            logger.exception("Archive sync failed for %s", chat.chat_id)
            return 0

        stored = await self._db.store_messages(collected)
        if max_id > chat.last_message_id:
            await self._db.set_watermark(chat.chat_id, max_id)
        if stored:
            logger.info("Archive synced %s: +%d messages", chat.chat_id, stored)
        return stored

    async def sync_all(self) -> int:
        """Sync every enabled chat; record one sync run. Returns total stored."""
        chats = await self._db.list_chats()
        total = 0
        for chat in chats:
            total += await self.sync_chat(chat)
        await self._store.incr(COUNTER_ARCHIVE_SYNCS)
        logger.info("Archive sync run complete: +%d messages across %d chats", total, len(chats))
        return total

    # ------------------------------------------------------------------ #
    # Stats provider
    # ------------------------------------------------------------------ #
    async def stat_provider(self) -> dict[str, int]:
        """Return archive figures for the global ``.stats`` command."""
        return {
            "archive_chats": await self._db.count_chats(),
            "archived_messages": await self._db.count_messages(),
        }


def _int_env(name: str, default: int) -> int:
    """Read an integer environment variable, falling back on parse failure."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
