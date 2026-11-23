"""Formatting helpers for the Advanced Info module."""

from __future__ import annotations

from typing import Optional


def yesno(value: object) -> str:
    """Render a truthy/falsey flag as ``Yes``/``No``."""
    return "Yes" if value else "No"


def line(label: str, value: object) -> Optional[str]:
    """Return ``"label: value"`` or ``None`` when the value is empty.

    Used to build reports that silently omit fields the API did not provide.

    Args:
        label: The field label.
        value: The field value (``None``/``""``/empty → omitted).

    Returns:
        The formatted line, or ``None`` to skip the field.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return f"{label}: {value}"


def join_lines(parts: list[Optional[str]]) -> str:
    """Join report lines, dropping ``None`` entries."""
    return "\n".join(p for p in parts if p is not None)


def usernames_of(entity: object) -> Optional[str]:
    """Return a space-separated ``@handle`` list of all usernames, or ``None``.

    Telegram supports multiple usernames per entity (``entity.usernames``) in
    addition to the primary ``entity.username``.
    """
    handles: list[str] = []
    primary = getattr(entity, "username", None)
    if primary:
        handles.append(f"@{primary}")
    for u in getattr(entity, "usernames", None) or []:
        name = getattr(u, "username", None)
        if name and f"@{name}" not in handles:
            handles.append(f"@{name}")
    return " ".join(handles) if handles else None
