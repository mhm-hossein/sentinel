"""AI Assistant module for the Telegram self-bot.

A Google Gemini-powered assistant exposed through a single unified ``.ai``
command. It supports direct questions, reply-based analysis, image and document
understanding, summarization, translation, code review and error-log diagnosis,
plus a trusted-user system, persistent model selection and rate limiting.

The module is self-contained and plugs into the host bot via
``ai.commands.AICommandHandler`` (wired up in ``start.py``'s
``setup_features`` hook). It reuses the project's SQLite database file and
logging conventions but keeps its own tables and connection so the banner
system is never touched.
"""

from __future__ import annotations

from .config import AIConfig
from .database import AIDatabase
from .assistant import AIAssistant
from .commands import AICommandHandler

__all__ = [
    "AIConfig",
    "AIDatabase",
    "AIAssistant",
    "AICommandHandler",
]

__version__ = "1.0.0"
