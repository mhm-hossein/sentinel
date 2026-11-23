"""Target detection and report building for the Advanced Info module.

Resolves the inspection target from the command context, queries the Telegram
API (entity + full-info request) and renders a compact English report. Every
field is read defensively (``getattr``) so a model lacking an attribute simply
omits that line rather than failing.
"""

from __future__ import annotations

import logging
from typing import Optional

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, Chat, User

from core.store import COUNTER_INFO_REQUESTS, SharedStore

from .utils import join_lines, line, usernames_of, yesno

logger = logging.getLogger("sentinel.info.info_manager")


class InfoManager:
    """Build user/group/channel reports for the ``.info`` command."""

    def __init__(self, client: TelegramClient, store: SharedStore) -> None:
        """Initialise the manager.

        Args:
            client: The Telethon client.
            store: Shared store (info-requests counter).
        """
        self._client = client
        self._store = store

    async def build_report(self, event) -> str:
        """Detect the target from ``event`` and render its report.

        Detection order: a reply targets the replied-to user; otherwise a
        private chat targets the other user and a group/channel targets itself.

        Args:
            event: The triggering ``.info`` command event.

        Returns:
            A compact English report (never raises; returns an error string on
            failure).
        """
        try:
            entity = await self._resolve_target(event)
            if entity is None:
                return "⚠ Could not resolve a target"

            if isinstance(entity, User):
                report = await self._user_report(entity)
            elif isinstance(entity, Channel):
                report = await self._channel_report(entity)
            elif isinstance(entity, Chat):
                report = await self._basic_group_report(entity)
            else:
                report = "⚠ Unsupported target type"

            await self._store.incr(COUNTER_INFO_REQUESTS)
            return report
        except Exception:  # noqa: BLE001 - info must never crash the bot
            logger.exception("Failed to build info report")
            return "⚠ Info Error"

    # ------------------------------------------------------------------ #
    # Target resolution
    # ------------------------------------------------------------------ #
    async def _resolve_target(self, event) -> Optional[object]:
        """Resolve the entity to inspect from the command context."""
        if event.is_reply:
            replied = await event.get_reply_message()
            sender = await replied.get_sender() if replied else None
            if sender is not None:
                return sender
        # Private chat → the other user; group/channel → the chat itself.
        return await event.get_chat()

    # ------------------------------------------------------------------ #
    # User report
    # ------------------------------------------------------------------ #
    async def _user_report(self, user: User) -> str:
        """Render a report for a user."""
        full = None
        try:
            full = await self._client(GetFullUserRequest(user))
        except Exception:  # noqa: BLE001 - full info is best-effort
            logger.debug("GetFullUserRequest failed for %s", user.id)

        full_user = getattr(full, "full_user", None)
        bio = getattr(full_user, "about", None)
        common = getattr(full_user, "common_chats_count", None)
        photos = await self._photo_count(user)
        stories = self._stories_count(full_user)

        first = getattr(user, "first_name", None)
        last = getattr(user, "last_name", None)
        display = tl_utils.get_display_name(user) or str(user.id)

        parts = [
            "👤 User Info",
            "",
            line("Name", display),
            line("First Name", first),
            line("Last Name", last),
            line("Username", usernames_of(user)),
            line("ID", user.id),
            line("Bio", bio),
            line("Phone", getattr(user, "phone", None)),
            line("Language", getattr(user, "lang_code", None)),
            line("Premium", yesno(getattr(user, "premium", False))),
            line("Verified", yesno(getattr(user, "verified", False))),
            line("Bot", yesno(getattr(user, "bot", False))),
            line("Scam", yesno(getattr(user, "scam", False))),
            line("Fake", yesno(getattr(user, "fake", False))),
            line("Common Chats", common),
            line("Profile Photos", photos),
            line("Stories", stories),
        ]
        return join_lines(parts)

    async def _photo_count(self, user: User) -> Optional[int]:
        """Return the user's profile-photo count, or ``None`` if unavailable."""
        try:
            photos = await self._client.get_profile_photos(user, limit=1)
            return getattr(photos, "total", None)
        except Exception:  # noqa: BLE001 - count is best-effort
            return None

    @staticmethod
    def _stories_count(full_user: object) -> Optional[int]:
        """Best-effort story count from full-user stories, if present."""
        stories = getattr(full_user, "stories", None)
        if stories is None:
            return None
        return getattr(stories, "count", None)

    # ------------------------------------------------------------------ #
    # Channel / supergroup report
    # ------------------------------------------------------------------ #
    async def _channel_report(self, channel: Channel) -> str:
        """Render a report for a supergroup or broadcast channel."""
        full_chat = None
        linked: Optional[int] = None
        try:
            full = await self._client(GetFullChannelRequest(channel))
            full_chat = getattr(full, "full_chat", None)
            linked = getattr(full_chat, "linked_chat_id", None)
        except Exception:  # noqa: BLE001 - full info is best-effort
            logger.debug("GetFullChannelRequest failed for %s", channel.id)

        is_group = bool(getattr(channel, "megagroup", False))
        heading = "👥 Group Info" if is_group else "📢 Channel Info"
        members_label = "Members" if is_group else "Subscribers"

        members = getattr(full_chat, "participants_count", None)
        if members is None:
            members = getattr(channel, "participants_count", None)
        admins = getattr(full_chat, "admins_count", None)
        about = getattr(full_chat, "about", None)
        slowmode = getattr(full_chat, "slowmode_seconds", None)
        noforwards = getattr(full_chat, "noforwards", None)
        if noforwards is None:
            noforwards = getattr(channel, "noforwards", False)

        parts = [
            heading,
            "",
            line("Title", getattr(channel, "title", None)),
            line("Username", usernames_of(channel)),
            line("ID", tl_utils.get_peer_id(channel)),
            line(members_label, members),
            line("Admins", admins),
            line("Description", about),
            line("Forum", yesno(getattr(channel, "forum", False))) if is_group else None,
            line("Protected Content", yesno(noforwards)),
            line("Slow Mode", f"{slowmode}s") if slowmode else None,
            line("Linked Chat", linked),
        ]
        return join_lines(parts)

    # ------------------------------------------------------------------ #
    # Basic (small) group report
    # ------------------------------------------------------------------ #
    async def _basic_group_report(self, chat: Chat) -> str:
        """Render a report for a basic (non-supergroup) group."""
        full_chat = None
        try:
            full = await self._client(GetFullChatRequest(chat.id))
            full_chat = getattr(full, "full_chat", None)
        except Exception:  # noqa: BLE001 - full info is best-effort
            logger.debug("GetFullChatRequest failed for %s", chat.id)

        about = getattr(full_chat, "about", None)
        members = getattr(chat, "participants_count", None)
        noforwards = getattr(chat, "noforwards", False)

        parts = [
            "👥 Group Info",
            "",
            line("Title", getattr(chat, "title", None)),
            line("ID", tl_utils.get_peer_id(chat)),
            line("Members", members),
            line("Description", about),
            line("Protected Content", yesno(noforwards)),
        ]
        return join_lines(parts)
