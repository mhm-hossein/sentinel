"""Core save logic: download protected content and re-upload to Saved Messages.

The defining trick of this module is **download → re-upload** rather than
forward. Telegram blocks *forwarding* from Protected-Content / forward-restricted
chats, but the account can still *download* media it can see; re-uploading that
download to Saved Messages produces a clean, unrestricted copy.

All operations target Saved Messages (``"me"``), preserve captions, attach
source metadata + a best-effort link, and never raise — failures are reported
as short status strings.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.errors import FloodWaitError
from telethon.tl.custom import Message
from telethon.tl.types import MessageMediaDocument

from core.store import COUNTER_SAVED_MESSAGES, SharedStore

from .utils import build_link, format_source, has_downloadable_media

logger = logging.getLogger("sentinel.save.saver")

#: Saved Messages pseudo-peer.
_SAVED = "me"

#: Telegram limits.
_CAPTION_LIMIT = 1024
_MESSAGE_LIMIT = 4096

#: Upper bound (seconds) we are willing to wait out a FloodWait inline.
_MAX_FLOOD_WAIT = 60


@dataclass(slots=True)
class SaveResult:
    """Outcome of a save operation.

    Attributes:
        saved: Number of source messages successfully saved.
        failed: Number of source messages that could not be saved.
        note: Optional short status note (e.g. an error reason).
    """

    saved: int = 0
    failed: int = 0
    note: str = ""


class MessageSaver:
    """Download and re-upload messages into Saved Messages."""

    def __init__(self, client: TelegramClient, store: SharedStore) -> None:
        """Initialise the saver.

        Args:
            client: The Telethon client.
            store: Shared store (saved-messages counter).
        """
        self._client = client
        self._store = store

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def save_single(self, message: Message) -> SaveResult:
        """Save one message (or its whole album if it belongs to a group).

        Args:
            message: The source message to save.

        Returns:
            A :class:`SaveResult` describing the outcome.
        """
        if message is None:
            return SaveResult(failed=1, note="Message unavailable")
        if getattr(message, "grouped_id", None):
            album = await self._fetch_album(message)
            return await self._save_album(album)
        return await self._save_one(message)

    async def save_recent(
        self, chat_id: int, count: int, *, exclude_ids: tuple[int, ...] = ()
    ) -> SaveResult:
        """Save the last ``count`` messages of a chat.

        Albums are saved once as a group (their sibling messages are skipped to
        avoid duplicates).

        Args:
            chat_id: The chat to read from.
            count: How many recent messages to save (already bounded by caller).
            exclude_ids: Message ids to skip (e.g. the ``.save <n>`` command
                message itself, which would otherwise displace one real
                history message).

        Returns:
            A :class:`SaveResult` aggregating every saved message.
        """
        result = SaveResult()
        seen_groups: set[int] = set()
        excluded = set(exclude_ids)
        try:
            messages = await self._client.get_messages(
                chat_id, limit=count + len(excluded)
            )
        except FloodWaitError as exc:
            return SaveResult(failed=1, note=f"FloodWait {exc.seconds}s — try later")
        except Exception:  # noqa: BLE001 - unreachable chat / permission error
            logger.exception("save_recent: could not read chat %s", chat_id)
            return SaveResult(failed=1, note="Cannot read this chat")

        # Oldest first so Saved Messages preserves chronological order.
        saved_real = 0
        for message in reversed(list(messages)):
            if message is None:
                continue
            if int(getattr(message, "id", 0) or 0) in excluded:
                continue
            if saved_real >= count:
                break
            saved_real += 1
            group = getattr(message, "grouped_id", None)
            if group:
                if group in seen_groups:
                    continue
                seen_groups.add(group)
                part = await self._save_album(await self._fetch_album(message))
            else:
                part = await self._save_one(message)
            result.saved += part.saved
            result.failed += part.failed
        return result

    # ------------------------------------------------------------------ #
    # Internal save paths
    # ------------------------------------------------------------------ #
    async def _save_one(self, message: Message) -> SaveResult:
        """Save a single (non-album) message."""
        try:
            chat_name, username = await self._chat_meta(message)
            footer = self._footer(message, chat_name, username)
            caption = message.message or ""

            if has_downloadable_media(message):
                tmp_dir = tempfile.mkdtemp(prefix="sentinel_save_")
                try:
                    path = await self._download(message, tmp_dir)
                    if not path:
                        return SaveResult(failed=1, note="Media unavailable")
                    await self._send_media([path], caption, footer, message)
                finally:
                    _cleanup(tmp_dir)
            else:
                await self._send_text_message(caption, footer)

            await self._store.incr(COUNTER_SAVED_MESSAGES)
            return SaveResult(saved=1)
        except FloodWaitError as exc:
            return SaveResult(failed=1, note=f"FloodWait {exc.seconds}s — try later")
        except Exception:  # noqa: BLE001 - never crash the bot
            logger.exception("Failed to save message %s", getattr(message, "id", "?"))
            return SaveResult(failed=1, note="Save failed")

    async def _save_album(self, album: list[Message]) -> SaveResult:
        """Save a media group as a single grouped upload."""
        if not album:
            return SaveResult(failed=1, note="Album unavailable")
        anchor = album[0]
        try:
            chat_name, username = await self._chat_meta(anchor)
            footer = self._footer(anchor, chat_name, username, count=len(album))
            caption = next((m.message for m in album if m.message), "")

            tmp_dir = tempfile.mkdtemp(prefix="sentinel_save_")
            try:
                paths: list[str] = []
                for m in album:
                    if has_downloadable_media(m):
                        path = await self._download(m, tmp_dir)
                        if path:
                            paths.append(path)
                if not paths:
                    return SaveResult(failed=1, note="Album media unavailable")
                await self._send_media(paths, caption, footer, None)
            finally:
                _cleanup(tmp_dir)

            await self._store.incr(COUNTER_SAVED_MESSAGES, len(album))
            return SaveResult(saved=len(album))
        except FloodWaitError as exc:
            return SaveResult(failed=1, note=f"FloodWait {exc.seconds}s — try later")
        except Exception:  # noqa: BLE001 - never crash the bot
            logger.exception("Failed to save album")
            return SaveResult(failed=1, note="Save failed")

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #
    async def _send_media(
        self,
        paths: list[str],
        caption: str,
        footer: str,
        message: Optional[Message],
    ) -> None:
        """Upload downloaded files to Saved Messages, then the metadata footer.

        For a single document we preserve the original Telegram attributes
        (voice waveform, filename, sticker/animation flags) so the copy keeps
        its type. The metadata footer is sent as a separate message so captions
        stay pristine and never hit the caption length limit.
        """
        attributes = None
        if message is not None and isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            attributes = getattr(doc, "attributes", None)

        send_caption = caption if len(caption) <= _CAPTION_LIMIT else ""
        files: object = paths if len(paths) > 1 else paths[0]

        await self._with_flood_retry(
            self._client.send_file,
            _SAVED,
            files,
            caption=send_caption,
            attributes=attributes if len(paths) == 1 else None,
        )
        # Caption too long for a caption slot → send it as its own message.
        if caption and not send_caption:
            await self._send_text(caption)
        await self._send_text(footer)

    async def _send_text_message(self, text: str, footer: str) -> None:
        """Re-send a text-only message followed by its metadata footer."""
        if text:
            await self._send_text(text)
        await self._send_text(footer)

    async def _send_text(self, text: str) -> None:
        """Send text to Saved Messages, chunked to the per-message limit."""
        for chunk in _chunk(text, _MESSAGE_LIMIT):
            if chunk:
                await self._with_flood_retry(
                    self._client.send_message, _SAVED, chunk, link_preview=False
                )

    async def _with_flood_retry(self, func, *args, **kwargs):
        """Call ``func`` once, waiting out a bounded FloodWait and retrying."""
        try:
            return await func(*args, **kwargs)
        except FloodWaitError as exc:
            if exc.seconds > _MAX_FLOOD_WAIT:
                raise
            logger.warning("FloodWait %ss while saving; waiting", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            return await func(*args, **kwargs)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _download(self, message: Message, dest_dir: str) -> Optional[str]:
        """Download a message's media into ``dest_dir``; return the file path."""
        try:
            return await self._client.download_media(message, file=dest_dir)
        except Exception:  # noqa: BLE001 - media may be expired/unavailable
            logger.exception("Download failed for message %s", message.id)
            return None

    async def _fetch_album(self, message: Message) -> list[Message]:
        """Gather every message belonging to ``message``'s media group."""
        chat_id = message.chat_id
        window_ids = list(range(message.id - 9, message.id + 10))
        try:
            fetched = await self._client.get_messages(chat_id, ids=window_ids)
        except Exception:  # noqa: BLE001 - fall back to the single message
            return [message]
        album = [
            m for m in fetched if m is not None and m.grouped_id == message.grouped_id
        ]
        album.sort(key=lambda m: m.id)
        return album or [message]

    async def _chat_meta(self, message: Message) -> tuple[str, Optional[str]]:
        """Return ``(chat_display_name, username)`` for a message's chat."""
        try:
            chat = await message.get_chat()
        except Exception:  # noqa: BLE001 - entity may be inaccessible
            chat = None
        name = tl_utils.get_display_name(chat) if chat else str(message.chat_id)
        username = getattr(chat, "username", None) if chat else None
        return name or str(message.chat_id), username

    def _footer(
        self,
        message: Message,
        chat_name: str,
        username: Optional[str],
        *,
        count: int = 1,
    ) -> str:
        """Build the source-metadata footer for a saved message/album."""
        link = build_link(message.chat_id, message.id, username)
        footer = format_source(
            chat_name=chat_name,
            chat_id=message.chat_id,
            message_id=message.id,
            date=message.date,
            link=link,
        )
        if count > 1:
            footer += f"\nAlbum: {count} items"
        return footer


def _cleanup(path: str) -> None:
    """Remove a temporary directory tree, ignoring errors."""
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        logger.debug("Failed to clean temp dir %s", path)


def _chunk(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
