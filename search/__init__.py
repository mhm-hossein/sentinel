"""Global Search module.

Performs *live* searches directly against Telegram (never the Archive database,
caches or exports) across all of the account's chats, channels and Saved
Messages, with optional media filtering and TXT/JSON export.
"""

from __future__ import annotations

from .service import SearchResult, SearchService
from .commands import SearchCommands, help_section

__all__ = ["SearchResult", "SearchService", "SearchCommands", "help_section"]
