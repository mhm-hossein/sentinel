"""Quick Variables module — reusable text aliases for the owner.

Lets the owner store named snippets (card numbers, phone numbers, signatures,
addresses, …) and recall them instantly. ``.var add card 6037…`` registers an
alias; typing ``.card`` afterwards replaces the command with the stored value.

Like every Sentinel feature this module depends only on ``core`` and the banner
help registry — never on another feature module (star topology).
"""

from __future__ import annotations

from .commands import VariablesCommands, help_section
from .database import VariablesDatabase
from .manager import VariablesManager
from .models import Variable

__all__ = [
    "Variable",
    "VariablesDatabase",
    "VariablesManager",
    "VariablesCommands",
    "help_section",
]
