"""Validation helpers for the Quick Variables module."""

from __future__ import annotations

import re

#: Keyword charset: letters, digits and underscore (matched case-insensitively
#: at parse time; keywords are stored lowercased).
_KEYWORD_RE = re.compile(r"^[A-Za-z0-9_]+$")

#: Maximum keyword length (keeps aliases short and the command line sane).
MAX_KEYWORD_LEN = 32

#: Keywords that must never become aliases — they collide with existing
#: top-level commands. Blocking them at creation time guarantees the alias
#: executor can never shadow a real command. Keep in sync with the registered
#: command verbs across modules.
RESERVED: frozenset[str] = frozenset(
    {
        "help",
        "stats",
        "status",
        "var",
        "vars",
        "variable",
        "variables",
        "save",
        "info",
        "ai",
        "banner",
        "archive",
        "export",
        "search",
        "backup",
        "system",
        "sys",
        "track",
        "tracking",
        "log",
        "logger",
        "vps",
        "ghost",
        "hunter",
        "autosave",
        "antidel",
        "antiedit",
    }
)


def normalize_keyword(raw: str) -> str:
    """Lowercase and strip a candidate keyword.

    Args:
        raw: The user-supplied keyword token.

    Returns:
        The normalised keyword (may still be invalid; call :func:`validate`).
    """
    return (raw or "").strip().lower()


def validate_keyword(keyword: str) -> str:
    """Validate a normalised keyword, returning an error string or ``""``.

    Args:
        keyword: An already-normalised keyword (see :func:`normalize_keyword`).

    Returns:
        An empty string if valid, otherwise a short human-readable reason.
    """
    if not keyword:
        return "Keyword is required"
    if len(keyword) > MAX_KEYWORD_LEN:
        return f"Keyword too long (max {MAX_KEYWORD_LEN})"
    if not _KEYWORD_RE.match(keyword):
        return "Keyword may only contain letters, digits and underscore"
    if keyword in RESERVED:
        return f"'{keyword}' is a reserved command and cannot be an alias"
    return ""
