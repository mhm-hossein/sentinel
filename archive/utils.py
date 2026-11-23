"""Utility helpers for the Archive Watch module."""

from __future__ import annotations

from typing import Optional

from telethon import utils as tl_utils

from .models import ArchiveMessage


def message_type(message: object) -> str:
    """Classify a Telethon message's content type.

    Args:
        message: A Telethon message.

    Returns:
        One of ``photo``/``video``/``voice``/``audio``/``document``/
        ``sticker``/``gif``/``text``.
    """
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


def sender_display(sender: object) -> Optional[str]:
    """Return a readable sender name, or ``None`` if unknown."""
    if sender is None:
        return None
    username = getattr(sender, "username", None)
    if username:
        return f"@{username}"
    name = tl_utils.get_display_name(sender)
    return name or None


def to_archive_message(chat_id: int, message: object, sender: object) -> ArchiveMessage:
    """Build an :class:`ArchiveMessage` from a Telethon message and its sender.

    Args:
        chat_id: Canonical chat id.
        message: The Telethon message.
        sender: The resolved sender entity (may be ``None``).

    Returns:
        A populated :class:`ArchiveMessage`.
    """
    reply_to = None
    reply_header = getattr(message, "reply_to", None)
    if reply_header is not None:
        reply_to = getattr(reply_header, "reply_to_msg_id", None)

    date = getattr(message, "date", None)
    return ArchiveMessage(
        chat_id=chat_id,
        message_id=int(message.id),
        sender_id=getattr(sender, "id", None),
        sender_name=sender_display(sender),
        date=date.timestamp() if date else 0.0,
        text=getattr(message, "message", None) or "",
        media_type=message_type(message),
        reply_to=int(reply_to) if reply_to else None,
    )
