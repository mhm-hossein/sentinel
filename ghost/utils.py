"""Profile snapshotting, change detection and alert formatting for Ghost.

The snapshot is a flat ``dict[str, str|int|bool]`` of every profile field we can
read through the Telethon API, each guarded so a missing attribute is simply
omitted (never raised). Change detection compares only the fields present in
*both* the old and new snapshots, so a transient fetch failure (e.g. a
``GetFullUserRequest`` that briefly fails) can never be mistaken for the user
clearing their bio.
"""

from __future__ import annotations

import logging
from typing import Optional

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.errors import FloodWaitError
from telethon.tl.functions.users import GetFullUserRequest

from .models import ProfileChange

logger = logging.getLogger("sentinel.ghost.utils")

#: Canonical field key -> human label, controlling alert wording and ordering.
FIELD_LABELS: dict[str, str] = {
    "username": "Username",
    "first_name": "First Name",
    "last_name": "Last Name",
    "display_name": "Display Name",
    "bio": "Bio",
    "premium": "Premium Status",
    "verified": "Verified Status",
    "fake": "Fake Status",
    "scam": "Scam Status",
    "restricted": "Restricted Status",
    "bot": "Bot Status",
    "profile_photos": "Profile Photos",
    "stories": "Stories",
    "gifts": "Gift Count",
    "emoji_status": "Emoji Status",
    "usernames": "Usernames List",
    "common_chats": "Common Chats",
    "lang_code": "Language",
    "phone": "Phone",
}


def field_label(field: str) -> str:
    """Return the human label for a field key (falls back to a title-cased key)."""
    return FIELD_LABELS.get(field, field.replace("_", " ").title())


async def build_profile_snapshot(client: TelegramClient, entity: object) -> dict:
    """Read every available profile field for ``entity`` into a flat snapshot.

    Every attribute access is guarded; fields that the API does not expose for
    this user are omitted from the result rather than stored as ``None``.

    Args:
        client: The Telethon client.
        entity: A resolved user entity.

    Returns:
        A ``dict`` mapping canonical field keys to JSON-serialisable values.

    Raises:
        FloodWaitError: Propagated so the caller can back off and retry later.
    """
    snap: dict[str, object] = {}

    def put(key: str, value: object) -> None:
        if value is None:
            return
        if isinstance(value, str) and value == "":
            # Keep explicit empties (e.g. a cleared bio) so clearing is detected,
            # but only when the attribute genuinely exists on the entity/full.
            snap[key] = ""
            return
        snap[key] = value

    # --- Basic identity (from the User entity) ---
    put("username", getattr(entity, "username", None))
    put("first_name", getattr(entity, "first_name", None))
    put("last_name", getattr(entity, "last_name", None))
    put("display_name", tl_utils.get_display_name(entity) or None)
    put("lang_code", getattr(entity, "lang_code", None))
    put("phone", getattr(entity, "phone", None))

    # --- Badge / status flags ---
    put("premium", bool(getattr(entity, "premium", False)))
    put("verified", bool(getattr(entity, "verified", False)))
    put("fake", bool(getattr(entity, "fake", False)))
    put("scam", bool(getattr(entity, "scam", False)))
    put("restricted", bool(getattr(entity, "restricted", False)))
    put("bot", bool(getattr(entity, "bot", False)))

    # --- Username list (collectible / multi usernames) ---
    usernames = getattr(entity, "usernames", None)
    if usernames:
        active = [u.username for u in usernames if getattr(u, "active", False)]
        if active:
            put("usernames", ", ".join(active))

    # --- Emoji status (document id of the custom emoji) ---
    emoji_status = getattr(entity, "emoji_status", None)
    if emoji_status is not None:
        put("emoji_status", getattr(emoji_status, "document_id", None))

    # --- Full-user details (bio, stories, gifts, common chats) ---
    try:
        full = await client(GetFullUserRequest(entity))
        full_user = getattr(full, "full_user", None)
        if full_user is not None:
            put("bio", getattr(full_user, "about", None))
            put("common_chats", getattr(full_user, "common_chats_count", None))

            stories = getattr(full_user, "stories", None)
            if stories is not None:
                count = (
                    getattr(stories, "count", None)
                    or getattr(stories, "stories_max_id", None)
                    or getattr(stories, "max_id", None)
                )
                put("stories", count)

            # Gift count — attribute name varies across Telegram layers.
            for attr in ("stargifts_count", "gifts_count", "stars_gifts_count"):
                value = getattr(full_user, attr, None)
                if value is not None:
                    put("gifts", value)
                    break
    except FloodWaitError:
        raise
    except Exception:  # noqa: BLE001 - full-user info is best-effort
        logger.debug("GetFullUserRequest failed for %s", getattr(entity, "id", "?"))

    # --- Profile photo count ---
    try:
        photos = await client.get_profile_photos(entity, limit=1)
        total = getattr(photos, "total", None)
        put("profile_photos", total if total is not None else len(photos))
    except FloodWaitError:
        raise
    except Exception:  # noqa: BLE001 - photo count is best-effort
        logger.debug("get_profile_photos failed for %s", getattr(entity, "id", "?"))

    return snap


def diff_snapshots(old: Optional[dict], new: dict) -> list[ProfileChange]:
    """Return the list of fields that changed between two snapshots.

    Only fields present in *both* snapshots are compared, so a field that is
    temporarily missing from the new snapshot (transient API failure) is never
    reported as a change.

    Args:
        old: The previous snapshot, or ``None`` (first scan → no changes).
        new: The freshly built snapshot.

    Returns:
        A list of :class:`ProfileChange`, ordered by :data:`FIELD_LABELS`.
    """
    if not old:
        return []

    changes: list[ProfileChange] = []
    common_keys = set(old.keys()) & set(new.keys())
    for key in common_keys:
        if old[key] != new[key]:
            changes.append(
                ProfileChange(
                    field_name=key,
                    old_value=_render(old[key]),
                    new_value=_render(new[key]),
                )
            )

    order = list(FIELD_LABELS.keys())

    def sort_key(change: ProfileChange) -> int:
        return order.index(change.field_name) if change.field_name in order else 999

    changes.sort(key=sort_key)
    return changes


def format_alert(target_name: str, user_id: int, change: ProfileChange) -> str:
    """Render a single change alert in the Ghost house style.

    Example::

        👻 Username Changed
        Tom (123456789)

        Old:
        old_username

        New:
        new_username
    """
    return "\n".join(
        [
            f"👻 {field_label(change.field_name)} Changed",
            f"{target_name} ({user_id})",
            "",
            "Old:",
            change.old_value if change.old_value not in (None, "") else "—",
            "",
            "New:",
            change.new_value if change.new_value not in (None, "") else "—",
        ]
    )


def _render(value: object) -> str:
    """Render a snapshot value as display text for alerts and history."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)
