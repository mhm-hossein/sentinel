"""Decoupled global statistics registry for the ``.stats`` command.

Feature modules register an async *provider* that returns a mapping of
canonical stat keys to integer values. The global ``.stats`` command merges all
providers and renders them in a fixed, human-friendly order. Modules never
import one another — they only depend on this registry.

Providers must be cheap: they should use aggregate SQL (``COUNT``/``SUM``),
never full-table scans in Python.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger("sentinel.stats")

#: A provider returns a mapping of canonical stat key -> value.
StatProvider = Callable[[], Awaitable[dict[str, int]]]

#: Registered providers (populated at startup).
_providers: list[StatProvider] = []

#: Ordered (key, label) pairs controlling how ``.stats`` renders values.
STAT_LAYOUT: tuple[tuple[str, str], ...] = (
    ("banners", "Banners"),
    ("banner_sends", "Banner Sends"),
    ("ai_requests", "AI Requests"),
    ("saved_media", "Saved Media"),
    ("deleted_captured", "Deleted Captured"),
    ("edits_tracked", "Edits Tracked"),
    ("messages_logged", "Messages Logged"),
    ("exports", "Exports"),
    ("backups", "Backups"),
    ("monitor_requests", "Monitor Requests"),
    ("archive_chats", "Tracked Archive Chats"),
    ("archived_messages", "Archived Messages"),
    ("archive_syncs", "Archive Sync Runs"),
    ("search_queries", "Search Queries"),
    ("search_exports", "Search Exports"),
    ("ai_fallbacks", "AI Fallback Activations"),
    ("variables", "Variables"),
    ("saved_messages", "Saved Messages"),
    ("info_requests", "Info Requests"),
    ("ghost_users", "Tracked Ghost Users"),
    ("ghost_changes", "Ghost Changes"),
    ("hunter_targets", "Hunter Targets"),
    ("hunter_checks", "Hunter Checks"),
    ("hunter_claims", "Hunter Claims"),
    ("hunter_successes", "Hunter Successes"),
    ("hunter_failures", "Hunter Failures"),
)


#: Maps shared-counter keys to canonical stat keys used by :data:`STAT_LAYOUT`.
_COUNTER_TO_STAT: dict[str, str] = {
    "saved_media": "saved_media",
    "deleted_captured": "deleted_captured",
    "edits_tracked": "edits_tracked",
    "messages_logged": "messages_logged",
    "exports_generated": "exports",
    "backups_created": "backups",
    "monitor_requests": "monitor_requests",
    "archive_sync_runs": "archive_syncs",
    "search_queries": "search_queries",
    "search_exports": "search_exports",
    "ai_fallback_activations": "ai_fallbacks",
    "saved_messages": "saved_messages",
    "info_requests": "info_requests",
}


def make_store_provider(store: "object") -> StatProvider:
    """Build a stats provider over a :class:`~core.store.SharedStore`.

    Translates the shared counters (saved media, captured deletions, edits,
    logged messages, exports, backups, monitor requests) into canonical stat
    keys in a single query.

    Args:
        store: A connected ``SharedStore`` instance.

    Returns:
        An async provider suitable for :func:`register_stat_provider`.
    """

    async def provider() -> dict[str, int]:
        counters = await store.all_counters()  # type: ignore[attr-defined]
        return {
            stat_key: int(counters.get(counter_key, 0))
            for counter_key, stat_key in _COUNTER_TO_STAT.items()
        }

    return provider


def register_stat_provider(provider: StatProvider) -> None:
    """Register a statistics provider.

    Args:
        provider: An async callable returning ``dict[str, int]``.
    """
    _providers.append(provider)


def clear_providers() -> None:
    """Remove all registered providers (used by tests)."""
    _providers.clear()


async def collect_stats() -> dict[str, int]:
    """Run every provider and merge their results.

    A provider that raises is skipped (logged), so one faulty module never
    breaks the ``.stats`` command.

    Returns:
        Merged mapping of stat key -> value (summed across providers).
    """
    merged: dict[str, int] = {}
    for provider in _providers:
        try:
            data = await provider()
        except Exception:  # noqa: BLE001 - one provider must not break stats
            logger.exception("A stats provider failed; skipping it")
            continue
        for key, value in data.items():
            merged[key] = merged.get(key, 0) + int(value)
    return merged
