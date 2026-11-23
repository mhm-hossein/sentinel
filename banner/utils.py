"""Utility helpers for the Banner system.

Includes:
    * a robust human-readable duration parser (``parse_interval``)
    * compact duration formatting (``format_interval`` / ``format_countdown``)
    * timestamp formatting helpers
    * Telegram message-link generation
    * a structured-logging configuration helper
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# --------------------------------------------------------------------------- #
# Duration parsing / formatting
# --------------------------------------------------------------------------- #

#: Mapping of unit suffixes to their length in seconds.
_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3_600,
    "d": 86_400,
}

#: A single ``<number><unit>`` token, e.g. ``20m`` or ``1h``.
_TOKEN_RE = re.compile(r"(\d+)([smhd])")

#: A full interval string: one or more tokens and nothing else.
_INTERVAL_RE = re.compile(r"^(?:\d+[smhd])+$")

#: Minimum allowed interval (seconds). Guards against hammering the API.
MIN_INTERVAL_SECONDS = 5


def parse_interval(text: str) -> int:
    """Parse a human duration string into a number of seconds.

    Supports single units (``30s``, ``5m``, ``20m``, ``1h``, ``2h``, ``1d``)
    and combined forms (``1h30m``, ``2h15m``, ``1d4h30m``). Units may appear
    in any order and are summed.

    Args:
        text: The duration string (case-insensitive, surrounding whitespace
            ignored).

    Returns:
        The total duration in whole seconds.

    Raises:
        ValueError: If the string is empty, malformed, or resolves to a
            duration shorter than :data:`MIN_INTERVAL_SECONDS`.
    """
    if not text:
        raise ValueError("Empty interval")

    normalized = text.strip().lower()
    if not _INTERVAL_RE.match(normalized):
        raise ValueError(f"Invalid interval format: {text!r}")

    total = 0
    for amount, unit in _TOKEN_RE.findall(normalized):
        total += int(amount) * _UNIT_SECONDS[unit]

    if total < MIN_INTERVAL_SECONDS:
        raise ValueError(
            f"Interval too short (minimum {MIN_INTERVAL_SECONDS}s): {text!r}"
        )
    return total


def format_interval(seconds: int) -> str:
    """Format a number of seconds as a compact duration string.

    The output uses the largest sensible units, e.g. ``5400`` -> ``"1h30m"``
    and ``1800`` -> ``"30m"``. Returns ``"0s"`` for non-positive input.

    Args:
        seconds: Duration in seconds.

    Returns:
        A compact duration string such as ``"1h30m"``.
    """
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"

    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs:
        parts.append(f"{secs}s")
    return "".join(parts)


def format_countdown(target: Optional[float], *, now: Optional[float] = None) -> str:
    """Format the time remaining until ``target`` as a compact string.

    Args:
        target: Absolute POSIX timestamp of the upcoming event, or ``None``.
        now: Reference time (defaults to the current time). Mainly for tests.

    Returns:
        ``"-"`` if ``target`` is ``None``, ``"now"`` if it is due, otherwise a
        compact duration like ``"12m"``.
    """
    if target is None:
        return "-"
    now = time.time() if now is None else now
    remaining = int(target - now)
    if remaining <= 0:
        return "now"
    return format_interval(remaining)


def format_uptime(seconds: float) -> str:
    """Format an uptime duration compactly, e.g. ``"3d 12h"`` or ``"42m"``.

    Args:
        seconds: Elapsed seconds.

    Returns:
        A short, human-readable uptime string (at most two units).
    """
    seconds = int(seconds)
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, _ = divmod(rem, 60)

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def format_timestamp(ts: Optional[float]) -> str:
    """Format a POSIX timestamp as a compact UTC ``YYYY-MM-DD HH:MM`` string.

    Args:
        ts: POSIX timestamp or ``None``.

    Returns:
        ``"-"`` if ``ts`` is ``None``, otherwise the formatted UTC time.
    """
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def format_ago(ts: Optional[float], *, now: Optional[float] = None) -> str:
    """Format how long ago ``ts`` was, e.g. ``"5m ago"``.

    Args:
        ts: Past POSIX timestamp or ``None``.
        now: Reference time (defaults to current time).

    Returns:
        ``"-"`` if ``ts`` is ``None``, otherwise ``"<duration> ago"``.
    """
    if ts is None:
        return "-"
    now = time.time() if now is None else now
    delta = int(now - ts)
    if delta <= 0:
        return "now"
    return f"{format_interval(delta)} ago"


# --------------------------------------------------------------------------- #
# Telegram link generation
# --------------------------------------------------------------------------- #


def build_message_link(
    chat_id: int,
    message_id: int,
    *,
    username: Optional[str] = None,
) -> str:
    """Build a ``t.me`` link to a specific message.

    For chats with a public ``username`` the canonical
    ``https://t.me/<username>/<id>`` form is used. For private supergroups and
    channels (``-100…`` IDs) the ``https://t.me/c/<internal>/<id>`` form is
    used. Plain private chats / small groups have no shareable link and yield
    an empty string.

    Args:
        chat_id: The Telegram chat ID.
        message_id: The target message ID.
        username: Public username of the chat, if known.

    Returns:
        A clickable ``t.me`` URL, or ``""`` if no link can be generated.
    """
    if username:
        return f"https://t.me/{username}/{message_id}"

    text = str(chat_id)
    if text.startswith("-100"):
        internal = text[4:]
        return f"https://t.me/c/{internal}/{message_id}"

    # Private user chats and basic groups are not linkable by message.
    return ""


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


#: Top-level logger namespaces used across the Sentinel platform. A shared
#: handler is attached to each so every module logs in a consistent format.
_LOG_NAMESPACES: tuple[str, ...] = (
    "sentinel",
    "banner",
    "ai",
    "tracking",
    "system",
    "backup",
)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure structured logging for the whole Sentinel platform.

    Installs a single stream handler with a consistent, parseable format on
    each platform namespace. Safe to call more than once; handlers are not
    duplicated.

    Args:
        level: The logging level for the Sentinel logger namespaces.

    Returns:
        The configured top-level ``sentinel`` logger.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root_logger = logging.getLogger("sentinel")
    for name in _LOG_NAMESPACES:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if not logger.handlers:
            # Each namespace gets its own handler instance to keep formatting
            # identical without relying on root propagation (avoids Telethon
            # noise leaking through the root logger).
            ns_handler = logging.StreamHandler(sys.stdout)
            ns_handler.setFormatter(handler.formatter)
            logger.addHandler(ns_handler)
            logger.propagate = False

    return root_logger
