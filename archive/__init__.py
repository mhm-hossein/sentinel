"""Archive Watch module.

Monitors a *selected* set of chats (unlike the logger, which can capture
everything). Each tracked chat is backfilled with a configurable slice of
recent history, then incrementally synced on a schedule — only new messages are
ever downloaded, tracked via the last-seen message id.
"""

from __future__ import annotations

from .database import ArchiveDatabase
from .models import ArchiveChat, ArchiveMessage
from .archive_manager import ArchiveConfig, ArchiveManager
from .scheduler import ArchiveScheduler
from .commands import ArchiveCommands, help_section

__all__ = [
    "ArchiveDatabase",
    "ArchiveChat",
    "ArchiveMessage",
    "ArchiveConfig",
    "ArchiveManager",
    "ArchiveScheduler",
    "ArchiveCommands",
    "help_section",
]
