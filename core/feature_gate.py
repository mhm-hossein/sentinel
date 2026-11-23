"""FeatureGate — the single, DB-driven feature-permission check.

Permissions flow **subscription → plan → features**, always read from the
central database. Nothing hardcodes which plan grants which feature; a future
module gains gating simply by registering its feature and being attached to a
plan by an admin.

Usage (every current and future module)::

    gate = FeatureGate(central)
    if await gate.has_feature(user_id, "ghost"):
        ...

``has_feature`` returns ``True`` only when the user has an **active, non-expired,
non-suspended** subscription (or, for backward compatibility, a directly
assigned ``users.plan_id`` when no subscription rows exist) whose plan grants the
feature. A short in-process cache avoids re-querying on hot paths.
"""

from __future__ import annotations

import time
from typing import Optional

from core.central import CentralDatabase


class FeatureGate:
    """Resolves a user's feature access from the central database."""

    def __init__(self, central: CentralDatabase, *, cache_ttl: float = 30.0) -> None:
        """Initialise the gate.

        Args:
            central: A connected central database.
            cache_ttl: Seconds to cache a user's resolved feature set (0 disables).
        """
        self._central = central
        self._cache_ttl = cache_ttl
        #: telegram_user_id -> (expires_at, frozenset[feature_key]).
        self._cache: dict[int, tuple[float, frozenset[str]]] = {}

    async def features_for(self, telegram_user_id: int) -> frozenset[str]:
        """Return the set of feature keys the user currently has access to."""
        now = time.time()
        cached = self._cache.get(telegram_user_id)
        if cached and cached[0] > now:
            return cached[1]

        keys = await self._resolve(telegram_user_id)
        if self._cache_ttl > 0:
            self._cache[telegram_user_id] = (now + self._cache_ttl, keys)
        return keys

    async def has_feature(self, telegram_user_id: int, feature_key: str) -> bool:
        """Return ``True`` if the user's active plan grants ``feature_key``."""
        return feature_key in await self.features_for(telegram_user_id)

    def invalidate(self, telegram_user_id: Optional[int] = None) -> None:
        """Drop cached entries (call after a plan/subscription change)."""
        if telegram_user_id is None:
            self._cache.clear()
        else:
            self._cache.pop(telegram_user_id, None)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _resolve(self, telegram_user_id: int) -> frozenset[str]:
        """Resolve feature keys via subscription → plan → plan_features."""
        user = await self._central.get_user(telegram_user_id)
        if user is None:
            return frozenset()

        # Prefer an active subscription; fall back to a direct plan assignment
        # only when the user has never had a subscription (legacy/admin-set).
        sub = await self._central.get_active_subscription(user.id)
        if sub is not None:
            plan_id = int(sub["plan_id"])
        else:
            latest = await self._central.get_latest_subscription(user.id)
            if latest is not None:
                # A subscription exists but is not active (expired/suspended/…):
                # no premium features until it is active again.
                return frozenset()
            plan_id = user.plan_id
        if plan_id is None:
            return frozenset()

        keys = await self._central.get_plan_feature_keys(plan_id)
        if not keys:
            # Backward-compat: plan predates plan_features seeding → fall back to
            # the plan's JSON modules so existing installs keep working.
            plan = await self._central.get_plan_by_id(plan_id)
            if plan is not None:
                keys = set(plan.modules)
        return frozenset(keys)
