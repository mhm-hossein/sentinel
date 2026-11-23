"""Domain models for the Archive Watch module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class ArchiveChat:
    """A chat being archived.

    Attributes:
        chat_id: Telegram chat id (canonical, signed).
        chat_name: Display name/title of the chat.
        enabled: Whether periodic sync is active.
        last_message_id: Highest archived message id (sync watermark).
        created_at: When the chat was added (POSIX seconds).
        updated_at: When the chat was last synced (POSIX seconds).
        id: Database primary key (``None`` until persisted).
    """

    chat_id: int
    chat_name: str
    enabled: bool = True
    last_message_id: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    id: Optional[int] = None


@dataclass(slots=True)
class ArchiveMessage:
    """A single archived message."""

    chat_id: int
    message_id: int
    sender_id: Optional[int]
    sender_name: Optional[str]
    date: float
    text: Optional[str]
    media_type: Optional[str]
    reply_to: Optional[int]
