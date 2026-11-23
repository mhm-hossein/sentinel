"""Advanced Info module — rich reports on users, groups and channels.

``.info`` inspects the current target (the user in a private chat, the current
group/channel, or a replied-to user) and renders a compact English report from
everything the Telegram API exposes. ``.info me`` delivers that report to Saved
Messages instead of the current chat.
"""

from __future__ import annotations

from .commands import InfoCommands, help_section
from .info_manager import InfoManager

__all__ = ["InfoManager", "InfoCommands", "help_section"]
