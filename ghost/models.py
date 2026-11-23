"""Domain models for the Ghost Tracker module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class GhostTarget:
    """A user whose profile is being monitored for changes.

    Attributes:
        user_id: Telegram user id (canonical).
        username: Last-known username (without ``@``), or ``None``.
        display_name: Last-known display name.
        enabled: Whether periodic scanning is active for this target.
        created_at: When the target was added (POSIX seconds).
        updated_at: When the target was last updated (POSIX seconds).
        last_check: When the profile was last scanned (POSIX seconds, 0 if never).
        id: Database primary key (``None`` until persisted).
    """

    user_id: int
    username: Optional[str] = None
    display_name: Optional[str] = None
    enabled: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0
    last_check: float = 0.0
    id: Optional[int] = None


@dataclass(slots=True)
class GhostHistoryEntry:
    """A single recorded profile change.

    Attributes:
        user_id: The user whose field changed.
        field_name: Canonical field key (e.g. ``"username"``, ``"bio"``).
        old_value: Previous value rendered as text (``None`` if previously unset).
        new_value: New value rendered as text.
        detected_at: When the change was detected (POSIX seconds).
        id: Database primary key (``None`` until persisted).
    """

    user_id: int
    field_name: str
    old_value: Optional[str]
    new_value: Optional[str]
    detected_at: float = 0.0
    id: Optional[int] = None


@dataclass(slots=True)
class ProfileChange:
    """An in-memory diff result for one field (before persistence/alerting)."""

    field_name: str
    old_value: Optional[str]
    new_value: Optional[str]
