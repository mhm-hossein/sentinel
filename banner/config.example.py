"""Example configuration for the Banner self-bot.

Copy this file to ``banner/config.py`` and fill in your own values:

    cp banner/config.example.py banner/config.py

Obtain ``API_ID`` and ``API_HASH`` from https://my.telegram.org (API
development tools). ``config.py`` is git-ignored and overrides environment
variables when present.
"""

from __future__ import annotations

# --- Required Telegram credentials -----------------------------------------
API_ID: int = 123456                 # your numeric API ID
API_HASH: str = "your_api_hash_here"  # your API hash string

# --- Optional settings ------------------------------------------------------
SESSION: str = "banner"   # Telethon session file name (creates banner.session)
DB_PATH: str = "banners.db"  # SQLite database file path
PREFIX: str = "."          # command prefix, e.g. ".banner 20m"
LOG_LEVEL: str = "INFO"     # DEBUG | INFO | WARNING | ERROR
