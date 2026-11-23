"""Domain model for the Quick Variables module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class Variable:
    """A reusable text alias.

    Attributes:
        keyword: Unique, lowercased alias name (``[A-Za-z0-9_]+``).
        value: The stored text, kept verbatim (multiline supported).
        created_at: When first created (POSIX seconds).
        updated_at: When last edited (POSIX seconds).
        id: Database primary key (``None`` until persisted).
    """

    keyword: str
    value: str
    created_at: float = 0.0
    updated_at: float = 0.0
    id: Optional[int] = None
