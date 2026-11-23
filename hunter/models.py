"""Domain models for the Username Hunter module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class HunterTarget:
    """A username being monitored for availability.

    Attributes:
        username: The username being hunted (normalised, without ``@``).
        enabled: Whether periodic checking is active.
        check_interval: Per-target check interval in seconds.
        last_check: When the username was last checked (POSIX seconds, 0 if never).
        created_at: When the target was added (POSIX seconds).
        updated_at: When the target was last updated (POSIX seconds).
        id: Database primary key (``None`` until persisted).
    """

    username: str
    enabled: bool = True
    check_interval: int = 60
    last_check: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    id: Optional[int] = None


@dataclass(slots=True)
class HunterEvent:
    """A recorded Hunter event (check / detection / claim outcome).

    Attributes:
        username: The username the event concerns.
        event_type: One of ``check`` / ``available`` / ``claim_success`` /
            ``claim_failure``.
        details: Free-text detail (owner type, failure reason, …).
        created_at: When the event occurred (POSIX seconds).
        id: Database primary key (``None`` until persisted).
    """

    username: str
    event_type: str
    details: Optional[str]
    created_at: float = 0.0
    id: Optional[int] = None


@dataclass(slots=True)
class UsernameStatus:
    """The live status of a username, as returned by a check.

    Attributes:
        username: The checked username.
        available: ``True`` if the username is unoccupied (claimable).
        owner_type: ``User`` / ``Bot`` / ``Group`` / ``Supergroup`` /
            ``Channel`` / ``Unknown`` (or ``-`` when available).
        detail: Optional extra detail (entity title / error).
    """

    username: str
    available: bool
    owner_type: str
    detail: Optional[str] = None

    @property
    def status_text(self) -> str:
        """Human label for the current status."""
        return "Available" if self.available else "Taken"


# Canonical event-type constants.
EVENT_CHECK = "check"
EVENT_AVAILABLE = "available"
EVENT_CLAIM_SUCCESS = "claim_success"
EVENT_CLAIM_FAILURE = "claim_failure"
