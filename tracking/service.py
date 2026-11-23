"""Tracking service: autosave, anti-delete and edit tracking.

Registers Telethon event handlers and toggle commands for three features that
share one message cache and the Sentinel shared store:

    * **Autosave** -- downloads view-once / self-destruct media and mirrors a
      copy to Saved Messages.
    * **Anti-delete** -- caches incoming private messages and, on deletion,
      reports the lost content (and re-sends media) to Saved Messages.
    * **Edit tracker** -- reports message edits (old vs new) to Saved Messages.

Every feature is independently toggleable and persists its on/off state.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient, events

from core.store import (
    COUNTER_DELETED_CAPTURED,
    COUNTER_EDITS_TRACKED,
    COUNTER_SAVED_MEDIA,
    SharedStore,
)

from .database import CachedMessage, TrackingDatabase

logger = logging.getLogger("tracking.service")

#: Persisted toggle keys (in the shared settings table).
SET_AUTOSAVE = "autosave_enabled"
SET_ANTIDELETE = "antidelete_enabled"
SET_ANTIEDIT = "antiedit_enabled"

#: Saved Messages destination.
_SAVED = "me"

#: Minimum seconds between cache prunes.
_PRUNE_INTERVAL = 3_600


@dataclass(slots=True)
class TrackingConfig:
    """Configuration for the tracking module.

    Attributes:
        autosave_default: Default autosave state when unset.
        antidelete_default: Default anti-delete state when unset.
        antiedit_default: Default edit-tracking state when unset.
        max_file_size_bytes: Skip caching/saving media larger than this.
        cache_dir: Directory for cached (anti-delete) media.
        autosave_dir: Directory for saved view-once media.
        cache_retention_seconds: Max age of cache rows before pruning.
    """

    autosave_default: bool = False
    antidelete_default: bool = False
    antiedit_default: bool = False
    max_file_size_bytes: int = 20 * 1024 * 1024
    cache_dir: str = os.path.join("data", "tracking_cache")
    autosave_dir: str = os.path.join("data", "autosave")
    cache_retention_seconds: float = 2 * 86_400

    @classmethod
    def from_env(cls) -> "TrackingConfig":
        """Build configuration from environment variables."""
        def flag(name: str) -> bool:
            return os.environ.get(name, "").strip().lower() in ("1", "true", "on", "yes")

        try:
            max_mb = int(os.environ.get("MAX_FILE_SIZE_MB", "20"))
        except ValueError:
            max_mb = 20

        return cls(
            autosave_default=flag("AUTOSAVE_ENABLED"),
            antidelete_default=flag("ANTIDELETE_ENABLED"),
            antiedit_default=flag("ANTIEDIT_ENABLED"),
            max_file_size_bytes=max_mb * 1024 * 1024,
        )


def help_section() -> str:
    """Return the compact Tracking help block for the ``.help`` menu."""
    return "\n".join(
        [
            "🛡 Tracking",
            ".autosave on|off|status",
            ".antidel on|off|status",
            ".antiedit on|off|status",
        ]
    )


class TrackingService:
    """Owns tracking state, event handlers and toggle commands."""

    def __init__(
        self,
        client: TelegramClient,
        database: TrackingDatabase,
        store: SharedStore,
        config: TrackingConfig,
        *,
        prefix: str = ".",
    ) -> None:
        """Initialise the service.

        Args:
            client: The Telethon client.
            database: Tracking cache database.
            store: Shared store for toggles and counters.
            config: Tracking configuration.
            prefix: Command prefix.
        """
        self._client = client
        self._db = database
        self._store = store
        self._config = config
        self._prefix = prefix
        self._last_prune = 0.0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Connect the cache DB, seed toggle defaults and create directories."""
        await self._db.connect()
        os.makedirs(self._config.cache_dir, exist_ok=True)
        os.makedirs(self._config.autosave_dir, exist_ok=True)
        await self._seed_toggle(SET_AUTOSAVE, self._config.autosave_default)
        await self._seed_toggle(SET_ANTIDELETE, self._config.antidelete_default)
        await self._seed_toggle(SET_ANTIEDIT, self._config.antiedit_default)

    async def _seed_toggle(self, key: str, default: bool) -> None:
        """Persist a toggle's default the first time it is seen."""
        if await self._store.get_setting(key, "") == "":
            await self._store.set_bool(key, default)

    def register(self) -> None:
        """Attach all event handlers and toggle commands."""
        self._client.add_event_handler(
            self._on_new_message, events.NewMessage(incoming=True)
        )
        self._client.add_event_handler(self._on_deleted, events.MessageDeleted())
        self._client.add_event_handler(
            self._on_edited, events.MessageEdited(incoming=True)
        )

        p = self._escaped_prefix()
        self._client.add_event_handler(
            self._on_autosave_cmd,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}autosave(?:\s+(on|off|status))?$"),
        )
        self._client.add_event_handler(
            self._on_antidel_cmd,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}antidel(?:\s+(on|off|status))?$"),
        )
        self._client.add_event_handler(
            self._on_antiedit_cmd,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}antiedit(?:\s+(on|off|status))?$"),
        )
        logger.info("Tracking handlers registered")

    def stop(self) -> None:
        """Unregister all event handlers (call before DB close on shutdown)."""
        for cb in (
            self._on_new_message,
            self._on_deleted,
            self._on_edited,
            self._on_autosave_cmd,
            self._on_antidel_cmd,
            self._on_antiedit_cmd,
        ):
            try:
                self._client.remove_event_handler(cb)
            except Exception:  # noqa: BLE001 - best-effort unwind
                pass

    async def close(self) -> None:
        """Alias for :meth:`stop` so teardown closeables can hold the service."""
        self.stop()

    def _escaped_prefix(self) -> str:
        import re

        return re.escape(self._prefix)

    # ------------------------------------------------------------------ #
    # Event: new message
    # ------------------------------------------------------------------ #
    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Autosave view-once media and cache private messages."""
        try:
            message = event.message
            if _is_self_destruct(message) and await self._enabled(
                SET_AUTOSAVE, self._config.autosave_default
            ):
                await self._autosave(event, message)

            if event.is_private:
                want_media = await self._enabled(SET_ANTIDELETE, self._config.antidelete_default)
                want_edit = await self._enabled(SET_ANTIEDIT, self._config.antiedit_default)
                if want_media or want_edit:
                    await self._cache_message(event, message, download_media=want_media)
        except Exception:  # noqa: BLE001 - never let tracking crash the bot
            logger.exception("Tracking new-message handler error")

    async def _autosave(self, event: events.NewMessage.Event, message: object) -> None:
        """Download and mirror a view-once/self-destruct media to Saved Messages."""
        sender = await event.get_sender()
        name = self._display_name(sender)
        path = await self._download(message, self._config.autosave_dir)
        if not path:
            return
        try:
            await self._client.send_file(
                _SAVED, path, caption=f"💾 Auto-saved view-once from {name}"
            )
        except Exception:  # noqa: BLE001 - mirroring is best-effort
            logger.exception("Failed to mirror auto-saved media")
            return
        finally:
            # The mirror is in Saved Messages now; never retain view-once
            # bytes on disk (unbounded growth + sensitive retention).
            self._remove_file(path)
        await self._store.incr(COUNTER_SAVED_MEDIA)
        logger.info("Autosaved view-once media from %s", name)

    async def _cache_message(
        self, event: events.NewMessage.Event, message: object, *, download_media: bool
    ) -> None:
        """Cache a message snapshot (and optionally its media) for tracking."""
        sender = await event.get_sender()
        chat = await event.get_chat()
        media_path: Optional[str] = None
        if download_media and getattr(message, "media", None) and not _is_self_destruct(message):
            if self._within_size(message):
                media_path = await self._download(message, self._config.cache_dir)

        await self._db.upsert(
            CachedMessage(
                chat_id=int(event.chat_id),
                message_id=int(message.id),
                sender_id=getattr(sender, "id", None),
                username=getattr(sender, "username", None),
                chat_title=self._chat_title(chat, sender),
                text=getattr(message, "message", None) or "",
                message_type=_message_type(message),
                media_path=media_path,
                date=time.time(),
            )
        )
        await self._maybe_prune()

    # ------------------------------------------------------------------ #
    # Event: deletion
    # ------------------------------------------------------------------ #
    async def _on_deleted(self, event: events.MessageDeleted.Event) -> None:
        """Report deleted private messages to Saved Messages."""
        try:
            if not await self._enabled(SET_ANTIDELETE, self._config.antidelete_default):
                return
            chat_id = event.chat_id
            if chat_id is None:
                return
            for message_id in event.deleted_ids:
                cached = await self._db.get(int(chat_id), int(message_id))
                if cached is None:
                    continue
                await self._notify_deleted(cached)
                await self._store.incr(COUNTER_DELETED_CAPTURED)
                await self._db.delete(cached.chat_id, cached.message_id)
                self._remove_file(cached.media_path)
        except Exception:  # noqa: BLE001 - never crash on deletion events
            logger.exception("Tracking deletion handler error")

    async def _notify_deleted(self, cached: CachedMessage) -> None:
        """Send a deletion report (and re-send media) to Saved Messages."""
        handle = f"@{cached.username}" if cached.username else "Unknown"
        lines = [
            "🗑 Deleted Message",
            "",
            f"User: {handle} ({cached.sender_id})",
            f"Chat: {cached.chat_title or '-'}",
            f"Type: {cached.message_type or 'text'}",
            f"Time: {_now_str()}",
        ]
        if cached.text:
            lines += ["", cached.text]
        await self._client.send_message(_SAVED, "\n".join(lines))

        if cached.media_path and os.path.exists(cached.media_path):
            try:
                await self._client.send_file(
                    _SAVED, cached.media_path, caption="🗑 Deleted media"
                )
            except Exception:  # noqa: BLE001 - media re-send is best-effort
                logger.debug("Failed to re-send deleted media")

    # ------------------------------------------------------------------ #
    # Event: edit
    # ------------------------------------------------------------------ #
    async def _on_edited(self, event: events.MessageEdited.Event) -> None:
        """Report message edits (old vs new) to Saved Messages."""
        try:
            if not event.is_private:
                return
            if not await self._enabled(SET_ANTIEDIT, self._config.antiedit_default):
                return
            message = event.message
            cached = await self._db.get(int(event.chat_id), int(message.id))
            new_text = getattr(message, "message", None) or ""
            old_text = cached.text if cached else None

            if cached and old_text is not None and old_text != new_text:
                await self._notify_edited(cached, new_text)
                await self._store.incr(COUNTER_EDITS_TRACKED)

            # Refresh the cache with the latest text.
            if cached:
                cached.text = new_text
                cached.date = time.time()
                await self._db.upsert(cached)
        except Exception:  # noqa: BLE001 - never crash on edit events
            logger.exception("Tracking edit handler error")

    async def _notify_edited(self, cached: CachedMessage, new_text: str) -> None:
        """Send an edit report to Saved Messages."""
        handle = f"@{cached.username}" if cached.username else "Unknown"
        lines = [
            "✏️ Edited Message",
            "",
            f"User: {handle} ({cached.sender_id})",
            "",
            "Old:",
            cached.text or "(empty)",
            "",
            "New:",
            new_text or "(empty)",
            "",
            _now_str(),
        ]
        await self._client.send_message(_SAVED, "\n".join(lines))

    # ------------------------------------------------------------------ #
    # Toggle commands
    # ------------------------------------------------------------------ #
    async def _on_autosave_cmd(self, event: events.NewMessage.Event) -> None:
        await self._handle_toggle(event, SET_AUTOSAVE, "Autosave", self._config.autosave_default)

    async def _on_antidel_cmd(self, event: events.NewMessage.Event) -> None:
        await self._handle_toggle(event, SET_ANTIDELETE, "AntiDelete", self._config.antidelete_default)

    async def _on_antiedit_cmd(self, event: events.NewMessage.Event) -> None:
        await self._handle_toggle(event, SET_ANTIEDIT, "AntiEdit", self._config.antiedit_default)

    async def _handle_toggle(
        self, event: events.NewMessage.Event, key: str, label: str, default: bool
    ) -> None:
        """Apply an on/off/status toggle command and edit the message."""
        try:
            arg = (event.pattern_match.group(1) or "status").lower()
            if arg == "on":
                await self._store.set_bool(key, True)
                logger.info("%s enabled", label)
                await self._edit(event, f"✓ {label}: ON")
            elif arg == "off":
                await self._store.set_bool(key, False)
                logger.info("%s disabled", label)
                await self._edit(event, f"✓ {label}: OFF")
            else:
                state = await self._enabled(key, default)
                await self._edit(event, f"{label}: {'ON' if state else 'OFF'}")
        except Exception:  # noqa: BLE001 - toggle must not crash
            logger.exception("Toggle command failed for %s", label)
            await self._edit(event, "⚠ Error")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _enabled(self, key: str, default: bool) -> bool:
        """Return the effective state of a toggle."""
        return await self._store.get_bool(key, default)

    async def _maybe_prune(self) -> None:
        """Prune old cache rows at most once per :data:`_PRUNE_INTERVAL`."""
        now = time.time()
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        paths = await self._db.prune_older_than(self._config.cache_retention_seconds)
        for path in paths:
            self._remove_file(path)

    def _within_size(self, message: object) -> bool:
        """Return whether a message's media is within the size limit."""
        file = getattr(message, "file", None)
        size = int(getattr(file, "size", 0) or 0) if file else 0
        return size <= self._config.max_file_size_bytes

    async def _download(self, message: object, directory: str) -> Optional[str]:
        """Download a message's media into ``directory``; return the path."""
        try:
            os.makedirs(directory, exist_ok=True)
            return await self._client.download_media(message, file=directory)
        except Exception:  # noqa: BLE001 - download failures are non-fatal
            logger.exception("Media download failed")
            return None

    @staticmethod
    def _remove_file(path: Optional[str]) -> None:
        """Delete a file if it exists, ignoring errors."""
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.debug("Could not remove file %s", path)

    @staticmethod
    def _display_name(sender: object) -> str:
        """Build a readable display name for a sender."""
        username = getattr(sender, "username", None)
        if username:
            return f"@{username}"
        first = getattr(sender, "first_name", "") or ""
        last = getattr(sender, "last_name", "") or ""
        name = f"{first} {last}".strip()
        return name or "Unknown"

    @classmethod
    def _chat_title(cls, chat: object, sender: object) -> str:
        """Return the chat title, falling back to the sender's name."""
        title = getattr(chat, "title", None)
        return title or cls._display_name(sender)

    async def _edit(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit tracking command message")


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #
def _is_self_destruct(message: object) -> bool:
    """Return whether a message carries view-once / self-destruct media."""
    media = getattr(message, "media", None)
    if media is None:
        return False
    return bool(getattr(media, "ttl_seconds", None))


def _message_type(message: object) -> str:
    """Classify a message's content type for reporting."""
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "gif", None):
        return "gif"
    if getattr(message, "document", None):
        return "document"
    return "text"


def _now_str() -> str:
    """Return the current UTC time as ``YYYY-MM-DD HH:MM:SS``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
