"""Backup manager module.

Creates timestamped ZIP archives of the database, configuration, logs, exports
and Telethon sessions; mirrors them to Saved Messages; and tracks backup
history. Restore extracts an archive to a safe, timestamped location.
"""

from __future__ import annotations

from .manager import BackupConfig, BackupManager
from .commands import BackupCommands

__all__ = ["BackupConfig", "BackupManager", "BackupCommands"]
