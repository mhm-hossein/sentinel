"""Username Hunter — monitor Telegram usernames and claim them when freed.

Hunter watches a list of usernames, periodically checks whether each is still
occupied, and — the moment one becomes available — attempts to claim it onto a
dedicated *reserve* entity (a channel or supergroup that Hunter creates and
reuses). Every check, detection and claim attempt is recorded and reported.

Claiming is never assumed to succeed: the workflow is always
*monitor → detect availability → attempt claim → report success/failure*.

The module follows the Sentinel star topology: it depends only on ``core`` and
the Telethon client, and stores everything in the shared ``sentinel.db``.
"""

from __future__ import annotations

from .database import HunterDatabase
from .hunter import HunterConfig, UsernameHunter
from .models import HunterEvent, HunterTarget
from .scheduler import HunterScheduler

__all__ = [
    "HunterDatabase",
    "HunterConfig",
    "UsernameHunter",
    "HunterEvent",
    "HunterTarget",
    "HunterScheduler",
]
