"""Message logger module.

Records messages from selected chats into SQLite and exports them to TXT/JSON
files delivered to Saved Messages. Targets, logged messages and export history
are persisted; counters feed the global statistics.
"""

from __future__ import annotations

from .database import LoggerDatabase
from .service import LoggerConfig, LoggerService

__all__ = ["LoggerDatabase", "LoggerConfig", "LoggerService"]
