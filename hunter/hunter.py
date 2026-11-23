"""Username Hunter engine: availability checks, reserve entity and claiming.

The hunting workflow is strictly *monitor → detect availability → attempt claim
→ report*. A claim is attempted onto a dedicated reserve entity (a channel or
supergroup that Hunter creates once and reuses); the claim result — success or
failure with a reason — is always recorded and reported, never assumed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UsernameOccupiedError,
)
from telethon.tl.functions.channels import CreateChannelRequest, UpdateUsernameRequest

try:  # available from Telethon 1.36+, guarded for older patch releases
    from telethon.errors import UsernamePurchaseAvailableError
except ImportError:  # pragma: no cover - defines a never-raised sentinel
    class UsernamePurchaseAvailableError(Exception):  # type: ignore[no-redef]
        """Fallback when the Telethon build lacks this error class."""

from core.store import (
    COUNTER_HUNTER_CHECKS,
    COUNTER_HUNTER_CLAIMS,
    COUNTER_HUNTER_FAILURES,
    COUNTER_HUNTER_SUCCESSES,
    SharedStore,
)

from .database import HunterDatabase
from .models import (
    EVENT_AVAILABLE,
    EVENT_CHECK,
    EVENT_CLAIM_FAILURE,
    EVENT_CLAIM_SUCCESS,
    HunterTarget,
    UsernameStatus,
)
from .utils import entity_title, owner_type

logger = logging.getLogger("sentinel.hunter.hunter")

#: Destination for claim alerts — the account's own Saved Messages.
_SAVED = "me"

#: Allowed reserve-entity claim targets.
_ALLOWED_TARGETS = ("channel", "group")

#: Title/description for the auto-created reserve entity.
_RESERVE_TITLES = {
    "channel": "Sentinel Reserve Channel",
    "group": "Sentinel Reserve Group",
}
_RESERVE_ABOUT = "Reserved by Sentinel Username Hunter."


@dataclass(slots=True)
class HunterConfig:
    """Configuration for Username Hunter.

    Attributes:
        check_interval_seconds: Base seconds between scan ticks (also the
            default per-target interval).
        claim_target: ``"channel"`` or ``"group"`` — the reserve entity type a
            freed username is claimed onto.
    """

    check_interval_seconds: int = 60
    claim_target: str = "channel"

    @classmethod
    def from_env(cls) -> "HunterConfig":
        """Build configuration from environment variables."""
        target = (os.environ.get("HUNTER_CLAIM_TARGET", "channel") or "channel").lower()
        if target not in _ALLOWED_TARGETS:
            logger.warning("Invalid HUNTER_CLAIM_TARGET=%r; using 'channel'", target)
            target = "channel"
        return cls(
            check_interval_seconds=_int_env("HUNTER_CHECK_INTERVAL_SECONDS", 60),
            claim_target=target,
        )

    @property
    def interval(self) -> int:
        """Scan interval in seconds (minimum 15s to stay FloodWait-safe)."""
        return max(15, self.check_interval_seconds)


class UsernameHunter:
    """Coordinates username monitoring, claiming and reporting."""

    def __init__(
        self,
        client: TelegramClient,
        database: HunterDatabase,
        store: SharedStore,
        config: HunterConfig,
    ) -> None:
        """Initialise the hunter.

        Args:
            client: The Telethon client.
            database: Hunter persistence layer.
            store: Shared store (counters + reserve-entity id).
            config: Hunter configuration.
        """
        self._client = client
        self._db = database
        self._store = store
        self._config = config
        self._reserve_lock = asyncio.Lock()

    @property
    def config(self) -> HunterConfig:
        """The active configuration."""
        return self._config

    @property
    def database(self) -> HunterDatabase:
        """The backing database (used by the command handler)."""
        return self._db

    # ------------------------------------------------------------------ #
    # Availability check
    # ------------------------------------------------------------------ #
    async def check_username(self, username: str) -> UsernameStatus:
        """Check a username's live status.

        Args:
            username: A normalised username (no ``@``).

        Returns:
            A :class:`UsernameStatus` describing availability and owner type.

        Raises:
            FloodWaitError: Propagated so callers can back off.
        """
        try:
            entity = await self._client.get_entity(username)
        except (UsernameNotOccupiedError, UsernamePurchaseAvailableError):
            return UsernameStatus(username, available=True, owner_type="-")
        except UsernameInvalidError:
            return UsernameStatus(
                username, available=False, owner_type="Unknown", detail="invalid"
            )
        except FloodWaitError:
            raise
        except ValueError:
            # Telethon raises ValueError when no entity corresponds to the
            # username — i.e. it is unoccupied / claimable.
            return UsernameStatus(username, available=True, owner_type="-")
        except Exception as exc:  # noqa: BLE001 - unknown resolution failure
            logger.debug("Hunter check error for %s: %s", username, exc)
            return UsernameStatus(
                username, available=False, owner_type="Unknown", detail=str(exc)
            )

        return UsernameStatus(
            username,
            available=False,
            owner_type=owner_type(entity),
            detail=entity_title(entity),
        )

    # ------------------------------------------------------------------ #
    # Reserve entity
    # ------------------------------------------------------------------ #
    def _reserve_key(self) -> str:
        """Settings key under which the reserve entity id is stored."""
        return f"hunter_reserve_{self._config.claim_target}_id"

    async def ensure_reserve_entity(self) -> object:
        """Return the reserve entity, creating it on first use (and reusing it).

        The entity id is persisted in the shared store keyed by claim-target
        type, so a single reserve channel/group is reused across restarts.

        Returns:
            The resolved reserve entity.

        Raises:
            Exception: If the entity can neither be resolved nor created.
        """
        async with self._reserve_lock:
            stored = await self._store.get_setting(self._reserve_key(), "")
            if stored:
                try:
                    return await self._client.get_entity(int(stored))
                except Exception:  # noqa: BLE001 - stale id; recreate below
                    logger.warning("Hunter reserve entity %s unresolved; recreating", stored)

            title = _RESERVE_TITLES[self._config.claim_target]
            result = await self._client(
                CreateChannelRequest(
                    title=title,
                    about=_RESERVE_ABOUT,
                    megagroup=self._config.claim_target == "group",
                    broadcast=self._config.claim_target == "channel",
                )
            )
            entity = result.chats[0]
            peer_id = tl_utils.get_peer_id(entity)
            await self._store.set_setting(self._reserve_key(), str(peer_id))
            logger.info("Hunter created reserve %s (%s)", title, peer_id)
            return entity

    # ------------------------------------------------------------------ #
    # Claiming
    # ------------------------------------------------------------------ #
    async def attempt_claim(self, username: str) -> tuple[bool, str]:
        """Attempt to claim ``username`` onto the reserve entity.

        Args:
            username: The (believed available) username to claim.

        Returns:
            ``(success, reason)`` — ``reason`` is the reserve title on success
            or a human-readable failure reason otherwise.
        """
        await self._store.incr(COUNTER_HUNTER_CLAIMS)
        try:
            entity = await self.ensure_reserve_entity()
        except FloodWaitError as exc:
            return False, f"FloodWait {int(exc.seconds)}s"
        except Exception as exc:  # noqa: BLE001 - reserve creation failed
            logger.exception("Hunter could not prepare reserve entity")
            return False, f"reserve error: {exc}"

        try:
            ok = await self._client(
                UpdateUsernameRequest(channel=entity, username=username)
            )
        except UsernameOccupiedError:
            return False, "Already Claimed"
        except UsernameInvalidError:
            return False, "Invalid Username"
        except UsernamePurchaseAvailableError:
            return False, "Purchase Required (collectible)"
        except FloodWaitError as exc:
            return False, f"FloodWait {int(exc.seconds)}s"
        except Exception as exc:  # noqa: BLE001 - any other RPC failure
            logger.debug("Hunter claim error for %s: %s", username, exc)
            return False, str(exc)

        if ok:
            return True, _RESERVE_TITLES[self._config.claim_target]
        return False, "Rejected by Telegram"

    # ------------------------------------------------------------------ #
    # Scanning
    # ------------------------------------------------------------------ #
    async def check_target(self, target: HunterTarget) -> Optional[UsernameStatus]:
        """Check one target; claim + report if it became available.

        Args:
            target: The hunted username.

        Returns:
            The :class:`UsernameStatus` from the check, or ``None`` on FloodWait.
        """
        try:
            status = await self.check_username(target.username)
        except FloodWaitError as exc:
            logger.warning("Hunter FloodWait %ss for %s", exc.seconds, target.username)
            await asyncio.sleep(int(exc.seconds) + 1)
            return None

        await self._store.incr(COUNTER_HUNTER_CHECKS)
        await self._db.add_event(target.username, EVENT_CHECK, status.owner_type)
        await self._db.set_last_check(target.username, time.time())

        if status.available:
            logger.info("Hunter: %s is AVAILABLE — attempting claim", target.username)
            await self._db.add_event(target.username, EVENT_AVAILABLE, None)
            await self._claim_and_report(target.username)

        return status

    async def _claim_and_report(self, username: str) -> None:
        """Attempt a claim and report success/failure to Saved Messages."""
        success, reason = await self.attempt_claim(username)
        if success:
            await self._store.incr(COUNTER_HUNTER_SUCCESSES)
            await self._db.add_event(username, EVENT_CLAIM_SUCCESS, reason)
            await self._db.set_enabled(username, False)  # claimed; stop hunting
            logger.info("Hunter CLAIMED %s onto %s", username, reason)
            await self._notify(
                "\n".join(
                    [
                        "🎯 Username Claimed",
                        "",
                        "Username:",
                        username,
                        "",
                        "Entity:",
                        reason,
                    ]
                )
            )
        else:
            await self._store.incr(COUNTER_HUNTER_FAILURES)
            await self._db.add_event(username, EVENT_CLAIM_FAILURE, reason)
            logger.info("Hunter claim FAILED for %s: %s", username, reason)
            await self._notify(
                "\n".join(
                    [
                        "❌ Claim Failed",
                        "",
                        "Username:",
                        username,
                        "",
                        "Reason:",
                        reason,
                    ]
                )
            )

    async def scan_all(self) -> int:
        """Check every enabled target whose interval has elapsed.

        Returns:
            The number of targets actually checked this run.
        """
        now = time.time()
        targets = await self._db.list_targets(enabled_only=True)
        checked = 0
        for target in targets:
            if now - target.last_check < target.check_interval:
                continue
            try:
                await self.check_target(target)
                checked += 1
            except Exception:  # noqa: BLE001 - one target must not break the run
                logger.exception("Hunter scan failed for %s", target.username)
        if checked:
            logger.info("Hunter scan run complete: %d username(s) checked", checked)
        return checked

    async def restore(self) -> int:
        """Log the number of targets resumed at startup; returns that count."""
        targets = await self._db.list_targets(enabled_only=True)
        logger.info("Hunter restored %d active target(s)", len(targets))
        return len(targets)

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #
    async def stat_provider(self) -> dict[str, int]:
        """Return Hunter figures for the global ``.stats`` command."""
        return {
            "hunter_targets": await self._db.count_targets(),
            "hunter_checks": await self._store.get_counter(COUNTER_HUNTER_CHECKS),
            "hunter_claims": await self._store.get_counter(COUNTER_HUNTER_CLAIMS),
            "hunter_successes": await self._store.get_counter(COUNTER_HUNTER_SUCCESSES),
            "hunter_failures": await self._store.get_counter(COUNTER_HUNTER_FAILURES),
        }

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _notify(self, text: str) -> None:
        """Send a Hunter alert to Saved Messages (best-effort)."""
        try:
            await self._client.send_message(_SAVED, text, link_preview=False)
        except FloodWaitError as exc:
            logger.warning("Hunter alert FloodWait %ss", exc.seconds)
            await asyncio.sleep(int(exc.seconds) + 1)
        except Exception:  # noqa: BLE001 - alerting is best-effort
            logger.debug("Failed to send Hunter alert")


def _int_env(name: str, default: int) -> int:
    """Read an integer environment variable, falling back on parse failure."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
