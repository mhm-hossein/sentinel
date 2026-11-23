"""Tracking module: timed-media saver, anti-delete and edit tracker.

Provides three independently toggleable features that observe incoming
messages and react to deletions/edits, mirroring captured content to Saved
Messages. All three share one small SQLite cache table and the Sentinel shared
store (for toggles and counters).
"""

from __future__ import annotations

from .database import TrackingDatabase
from .service import TrackingConfig, TrackingService

__all__ = ["TrackingDatabase", "TrackingConfig", "TrackingService"]
