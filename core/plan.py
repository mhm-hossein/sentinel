"""Subscription plans and limit enforcement.

A :class:`Plan` is a named bundle of limits stored as JSON in the central
database (``plans.limits_json``), so limit keys can grow per module without a
schema change. Two enforcement points consume it:

    1. **Module enablement** (setup time): ``plan.allows("archive")`` decides
       whether a feature module is wired at all for this user.
    2. **Quota guards** (command time): ``plan.check("max_banners", current)``
       raises :class:`LimitExceeded` when a create/consume would exceed the cap.

A limit value of ``-1`` (or a missing key) means *unlimited*, so default plans
can leave existing behavior untouched simply by omitting caps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class LimitExceeded(Exception):
    """Raised when an action would exceed the user's plan limit.

    Attributes:
        feature: The limit key that was exceeded (e.g. ``"max_banners"``).
        limit: The configured limit.
        current: The current usage that triggered the rejection.
    """

    def __init__(self, feature: str, limit: int, current: int) -> None:
        self.feature = feature
        self.limit = limit
        self.current = current
        super().__init__(f"Plan limit reached for {feature}: {current}/{limit}")


def plan_limit_text(exc: "LimitExceeded") -> str:
    """Render a user-facing message for a breached plan limit.

    Args:
        exc: The raised :class:`LimitExceeded`.

    Returns:
        A concise message in the modules' house style (the caller may add its
        own ``⚠`` prefix if it does not already include one).
    """
    feature = exc.feature.replace("_", " ")
    return f"Plan limit reached: {feature} ({exc.current}/{exc.limit}). Upgrade your plan."


def check_limit(
    plan: "Plan | None", feature: str, current: int, *, adding: int = 1
) -> str | None:
    """Return a limit message if ``feature`` would be exceeded, else ``None``.

    A convenience wrapper for command handlers that hold an optional plan: when
    ``plan`` is ``None`` (single-user / tests) or the limit is not breached, it
    returns ``None`` so the caller proceeds unchanged.

    Args:
        plan: The active plan, or ``None`` to disable enforcement.
        feature: The limit key (e.g. ``"archive_chats_max"``).
        current: Current usage count.
        adding: Units being added (default 1).

    Returns:
        A user-facing message (no ``⚠`` prefix) when the limit is exceeded,
        otherwise ``None``.
    """
    if plan is None:
        return None
    try:
        plan.check(feature, current, adding=adding)
        return None
    except LimitExceeded as exc:
        return plan_limit_text(exc)


#: The canonical set of feature-module keys a plan may enable/disable. Matches
#: the ``_setup_<key>`` wiring names in ``sentinel.features``.
ALL_MODULES: tuple[str, ...] = (
    "banner", "ai", "variables", "save", "info", "archive", "export",
    "search", "ghost", "hunter", "tracking", "logger", "system", "backup",
)


@dataclass(slots=True)
class Plan:
    """A subscription plan: enabled modules + numeric limits.

    Attributes:
        name: Plan name (e.g. ``"free"`` / ``"pro"``).
        modules: The set of enabled module keys (subset of :data:`ALL_MODULES`).
        limits: Numeric caps keyed by feature (``-1`` / missing = unlimited).
        id: Central-database primary key (``None`` for ad-hoc plans).
    """

    name: str
    modules: set[str] = field(default_factory=lambda: set(ALL_MODULES))
    limits: dict[str, int] = field(default_factory=dict)
    id: int | None = None

    # ------------------------------------------------------------------ #
    # Enablement
    # ------------------------------------------------------------------ #
    def allows(self, module: str) -> bool:
        """Return ``True`` if ``module`` is enabled for this plan."""
        return module in self.modules

    # ------------------------------------------------------------------ #
    # Quotas
    # ------------------------------------------------------------------ #
    def limit_for(self, feature: str) -> int:
        """Return the numeric limit for ``feature`` (``-1`` = unlimited)."""
        value = self.limits.get(feature, -1)
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def is_unlimited(self, feature: str) -> bool:
        """Return ``True`` if ``feature`` has no cap."""
        return self.limit_for(feature) < 0

    def check(self, feature: str, current: int, *, adding: int = 1) -> None:
        """Raise :class:`LimitExceeded` if adding would breach ``feature``.

        Args:
            feature: The limit key.
            current: Current usage count.
            adding: How many new units are being added (default 1).

        Raises:
            LimitExceeded: If ``current + adding`` would exceed the limit.
        """
        limit = self.limit_for(feature)
        if limit < 0:
            return
        if current + adding > limit:
            raise LimitExceeded(feature, limit, current)

    def remaining(self, feature: str, current: int) -> int | None:
        """Return remaining quota for ``feature`` (``None`` if unlimited)."""
        limit = self.limit_for(feature)
        if limit < 0:
            return None
        return max(0, limit - current)

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_json(self) -> str:
        """Serialise this plan's modules + limits to the stored JSON form."""
        return json.dumps(
            {"modules": sorted(self.modules), "limits": self.limits},
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_row(cls, name: str, limits_json: str, *, id: int | None = None) -> "Plan":
        """Build a :class:`Plan` from a central-database row.

        Unknown/blank JSON degrades to *all modules, no limits* so a bad row can
        never lock a user out of every feature.

        Args:
            name: Plan name.
            limits_json: The stored JSON document.
            id: Optional primary key.

        Returns:
            A populated :class:`Plan`.
        """
        modules: set[str] = set(ALL_MODULES)
        limits: dict[str, int] = {}
        try:
            data: dict[str, Any] = json.loads(limits_json) if limits_json else {}
            if isinstance(data.get("modules"), list):
                modules = {str(m) for m in data["modules"]}
            if isinstance(data.get("limits"), dict):
                limits = {str(k): int(v) for k, v in data["limits"].items()}
        except (TypeError, ValueError):
            pass
        return cls(name=name, modules=modules, limits=limits, id=id)


#: The plan limit key governing how many selfbot instances a user may run.
LIMIT_MAX_INSTANCES = "max_instances"
#: The plan limit key governing how many Gemini keys a user may store.
LIMIT_GEMINI_KEYS = "gemini_keys_max"


def instance_limit(plan: "Plan | None") -> int:
    """Return the max number of instances a plan allows (``-1`` = unlimited)."""
    if plan is None:
        return -1
    return plan.limit_for(LIMIT_MAX_INSTANCES)


def gemini_key_limit(plan: "Plan | None") -> int:
    """Return the max number of Gemini keys a plan allows (``-1`` = unlimited)."""
    if plan is None:
        return -1
    return plan.limit_for(LIMIT_GEMINI_KEYS)


#: Built-in customer tiers seeded into a fresh central database. The tiers are
#: differentiated primarily by ``max_instances`` (how many selfbot accounts a
#: user may run concurrently): Bronze=1, Silver=2, Gold=unlimited. ``unlimited``
#: is retained as an internal fallback (dev / run_local / admin).
DEFAULT_PLANS: tuple[Plan, ...] = (
    Plan(
        name="bronze",
        modules={"banner", "ai", "variables", "save", "info", "system"},
        limits={
            "max_instances": 1,
            "gemini_keys_max": 1,
            "max_banners": 5,
            "ai_requests_per_day": 100,
            "archive_chats_max": 0,
            "ghost_targets_max": 0,
            "hunter_targets_max": 0,
        },
    ),
    Plan(
        name="silver",
        modules=set(ALL_MODULES),
        limits={
            "max_instances": 2,
            "gemini_keys_max": 5,
            "max_banners": 50,
            "ai_requests_per_day": 1000,
            "archive_chats_max": 25,
            "ghost_targets_max": 25,
            "hunter_targets_max": 25,
        },
    ),
    Plan(
        name="gold",
        modules=set(ALL_MODULES),
        limits={"max_instances": -1, "gemini_keys_max": -1},  # unlimited
    ),
    Plan(
        name="unlimited",
        modules=set(ALL_MODULES),
        limits={"max_instances": -1, "gemini_keys_max": -1},  # internal fallback
    ),
)

#: Fallback plan used when a user has no plan row yet (safe, generous default).
DEFAULT_PLAN_NAME = "unlimited"

#: Default tier assigned to newly onboarded users (overridable via env).
DEFAULT_TIER_NAME = "bronze"
