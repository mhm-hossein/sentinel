"""Domain models for the Banner system.

This module defines the core data structures used throughout the banner
system: the :class:`BannerMode` and :class:`BannerStatus` enumerations and
the :class:`Banner` dataclass that represents a single persisted banner.

All time values are stored as POSIX timestamps (``float`` seconds since the
epoch) and intervals are stored as whole seconds (``int``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class BannerMode(str, Enum):
    """How a banner re-posts its source message.

    * ``FORWARD`` -- uses Telegram's native forward (shows a "forwarded from"
      header).
    * ``COPY`` -- re-sends the message content as a fresh message (text,
      media, or media group) with no forward header.
    """

    FORWARD = "forward"
    COPY = "copy"

    @property
    def short(self) -> str:
        """Return the single-letter UI label (``F`` or ``C``)."""
        return "F" if self is BannerMode.FORWARD else "C"

    @property
    def title(self) -> str:
        """Return the human-friendly mode name (``Forward`` / ``Copy``)."""
        return self.value.capitalize()

    @classmethod
    def parse(cls, value: str) -> "BannerMode":
        """Parse a mode from user input.

        Args:
            value: A string such as ``"forward"``, ``"copy"``, ``"f"`` or
                ``"c"`` (case-insensitive).

        Returns:
            The matching :class:`BannerMode`.

        Raises:
            ValueError: If ``value`` does not name a known mode.
        """
        normalized = value.strip().lower()
        if normalized in ("forward", "f"):
            return cls.FORWARD
        if normalized in ("copy", "c"):
            return cls.COPY
        raise ValueError(f"Unknown banner mode: {value!r}")


class BannerStatus(str, Enum):
    """Lifecycle state of a banner.

    * ``ACTIVE`` -- scheduled and running.
    * ``PAUSED`` -- retained but not scheduled.
    * ``STOPPED`` -- terminated; retained only for historical statistics.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(slots=True)
class Banner:
    """A single banner task.

    Attributes:
        id: Database primary key (``None`` until persisted).
        chat_id: Telegram chat the banner posts into.
        source_message_id: ID of the message being re-posted.
        interval: Re-post interval in seconds.
        mode: Forward or copy behaviour.
        status: Lifecycle state.
        created_at: Creation timestamp (POSIX seconds).
        last_run: Timestamp of the last successful send, if any.
        next_run: Scheduled timestamp of the next send, if active.
        sent_count: Number of successful sends so far.
    """

    chat_id: int
    source_message_id: int
    interval: int
    mode: BannerMode = BannerMode.FORWARD
    status: BannerStatus = BannerStatus.ACTIVE
    id: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    last_run: Optional[float] = None
    next_run: Optional[float] = None
    sent_count: int = 0

    # ------------------------------------------------------------------ #
    # Serialization helpers
    # ------------------------------------------------------------------ #
    def to_row(self) -> dict[str, Any]:
        """Return a dict suitable for SQLite parameter binding."""
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "source_message_id": self.source_message_id,
            "interval": self.interval,
            "mode": self.mode.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "sent_count": self.sent_count,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Banner":
        """Build a :class:`Banner` from a database row mapping."""
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            source_message_id=row["source_message_id"],
            interval=row["interval"],
            mode=BannerMode(row["mode"]),
            status=BannerStatus(row["status"]),
            created_at=row["created_at"],
            last_run=row["last_run"],
            next_run=row["next_run"],
            sent_count=row["sent_count"],
        )

    # ------------------------------------------------------------------ #
    # Convenience predicates
    # ------------------------------------------------------------------ #
    @property
    def is_active(self) -> bool:
        """Whether the banner is currently active."""
        return self.status is BannerStatus.ACTIVE

    @property
    def is_paused(self) -> bool:
        """Whether the banner is currently paused."""
        return self.status is BannerStatus.PAUSED
