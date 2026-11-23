"""Ghost Tracker — continuous Telegram profile-change monitoring.

Ghost Tracker watches selected users and records every detected change to their
public profile (username, name, bio, badges, photo/story/gift counts, emoji
status, username list, …). A background scheduler periodically re-reads each
target's profile, diffs it against the last stored snapshot and, whenever a
field actually changes, persists a history row and reports the change to Saved
Messages.

The module follows the Sentinel star topology: it depends only on ``core`` and
the Telethon client, never on another feature module. All of its tables live in
the shared ``sentinel.db`` and are created idempotently.
"""

from __future__ import annotations

from .database import GhostDatabase
from .models import GhostHistoryEntry, GhostTarget
from .scheduler import GhostScheduler
from .tracker import GhostConfig, GhostTracker

__all__ = [
    "GhostDatabase",
    "GhostTarget",
    "GhostHistoryEntry",
    "GhostScheduler",
    "GhostTracker",
    "GhostConfig",
]
