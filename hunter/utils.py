"""Username normalisation/validation and owner-type detection for Hunter."""

from __future__ import annotations

import re
from typing import Optional

from telethon.tl.types import Channel, Chat, User

#: Telegram public-username rule: 5–32 chars, letters/digits/underscore, must
#: start with a letter. (Telegram also forbids trailing/double underscores for
#: *new* usernames, but existing ones may bend the rules — we stay permissive.)
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def normalise_username(raw: str) -> Optional[str]:
    """Normalise a user-supplied username.

    Strips a leading ``@`` and surrounding whitespace and lower-cases the value
    (Telegram usernames are case-insensitive).

    Args:
        raw: The raw argument.

    Returns:
        The normalised username, or ``None`` if empty.
    """
    cleaned = raw.strip().lstrip("@").strip()
    return cleaned.lower() if cleaned else None


def is_valid_username(username: str) -> bool:
    """Return ``True`` if ``username`` is a structurally valid public username."""
    return bool(_USERNAME_RE.match(username))


def owner_type(entity: object) -> str:
    """Classify the owner of an occupied username.

    Args:
        entity: A resolved Telethon entity.

    Returns:
        One of ``User`` / ``Bot`` / ``Group`` / ``Supergroup`` / ``Channel`` /
        ``Unknown``.
    """
    if isinstance(entity, User):
        return "Bot" if getattr(entity, "bot", False) else "User"
    if isinstance(entity, Chat):
        return "Group"
    if isinstance(entity, Channel):
        # A megagroup is a supergroup; a broadcast channel is a channel.
        if getattr(entity, "megagroup", False):
            return "Supergroup"
        if getattr(entity, "gigagroup", False):
            return "Supergroup"
        return "Channel"
    return "Unknown"


def entity_title(entity: object) -> Optional[str]:
    """Return a readable title/name for an entity, or ``None``."""
    title = getattr(entity, "title", None)
    if title:
        return title
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    name = " ".join(p for p in (first, last) if p)
    return name or None
