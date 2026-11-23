"""Telegram Self-Bot Banner module.

A production-ready banner system for Telegram self-bots built on Telethon.
A banner repeatedly re-posts a target message back into its chat at a
configurable interval, using either Telegram forwarding or content copying.

Public package surface:
    Banner, BannerMode, BannerStatus  -- domain models
    Database                          -- SQLite persistence layer
    BannerScheduler                   -- async per-banner scheduling
    BannerManager                     -- high-level orchestration
"""

from __future__ import annotations

from .models import Banner, BannerMode, BannerStatus
from .database import Database
from .scheduler import BannerScheduler
from .banner_manager import BannerManager

__all__ = [
    "Banner",
    "BannerMode",
    "BannerStatus",
    "Database",
    "BannerScheduler",
    "BannerManager",
]

__version__ = "1.0.0"
