"""Formatting and link helpers for the Save Message module."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from telethon.tl.custom import Message
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
)

from banner.utils import build_message_link


def build_link(chat_id: int, message_id: int, username: Optional[str]) -> str:
    """Build a best-effort ``t.me`` link to a message.

    Priority: public ``t.me/<username>/<id>`` → private ``t.me/c/<internal>/<id>``
    → ``<chat_id>:<message_id>`` text fallback. Reuses
    :func:`banner.utils.build_message_link` for the first two forms.

    Args:
        chat_id: Source chat id.
        message_id: Source message id.
        username: Public username of the chat, if any.

    Returns:
        A clickable link, or the ``chat_id:message_id`` fallback string.
    """
    link = build_message_link(chat_id, message_id, username=username)
    if link:
        return link
    return f"{chat_id}:{message_id}"


def media_type_label(message: Message) -> str:
    """Return a short human label for a message's content type."""
    if message.sticker is not None:
        return "Sticker"
    if message.gif is not None:
        return "GIF"
    if message.voice is not None:
        return "Voice"
    if message.video_note is not None:
        return "Video Note"
    if message.video is not None:
        return "Video"
    if message.audio is not None:
        return "Audio"
    if message.photo is not None:
        return "Photo"
    media = message.media
    if isinstance(media, MessageMediaDocument):
        return "Document"
    if isinstance(media, MessageMediaPhoto):
        return "Photo"
    if media is not None and not isinstance(media, MessageMediaWebPage):
        return "Media"
    return "Text"


def has_downloadable_media(message: Message) -> bool:
    """Return ``True`` if the message carries re-uploadable media.

    Web-page previews are treated as text (nothing to download).
    """
    return bool(message.media) and not isinstance(
        message.media, MessageMediaWebPage
    )


def format_source(
    *,
    chat_name: str,
    chat_id: int,
    message_id: int,
    date,
    link: str,
) -> str:
    """Build the compact source-metadata footer attached to saved messages.

    Args:
        chat_name: Display name/title of the source chat.
        chat_id: Source chat id.
        message_id: Source message id.
        date: The message ``datetime`` (or ``None``).
        link: Pre-built message link (or fallback string).

    Returns:
        A multi-line English metadata block.
    """
    when = "?"
    if isinstance(date, datetime):
        when = date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "💾 Saved Message",
        f"Chat: {chat_name}",
        f"Chat ID: {chat_id}",
        f"Message ID: {message_id}",
        f"Date: {when}",
        f"Link: {link}",
    ]
    return "\n".join(lines)
