"""Dynamic feature registry — every capability is a database-backed feature.

At control-plane startup, :func:`register_default_features` upserts the catalog
below into the central ``features`` table (auto-creating any missing rows), so
the admin panel always lists every capability. **Feature access is never
hardcoded in Python** — it is read from ``plan_features`` at runtime via
:class:`core.feature_gate.FeatureGate` / :meth:`CentralDatabase.resolve_plan`.

Adding a future module (marketplace, analytics, scheduler, autoresponder, crm,
notifications, …) requires only appending one :class:`FeatureSpec` here (or
calling :meth:`CentralDatabase.register_feature` at startup) — no schema change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.central import CentralDatabase
from core.plan import DEFAULT_PLANS

logger = logging.getLogger("sentinel.registry")


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """A registrable capability: a stable key + human title + description."""

    key: str
    title: str
    description: str


#: The built-in capability catalog. Future modules add one entry here.
FEATURE_CATALOG: tuple[FeatureSpec, ...] = (
    FeatureSpec("banner", "Banner", "Repeating message banners (forward/copy)"),
    FeatureSpec("ai", "AI Assistant", "Gemini Q&A, vision, docs, translate"),
    FeatureSpec("variables", "Variables", "Reusable text aliases"),
    FeatureSpec("save", "Save", "Copy protected/restricted media to Saved Messages"),
    FeatureSpec("info", "Info", "Rich reports on users/groups/channels"),
    FeatureSpec("archive", "Archive", "Chat archiving / incremental history mirror"),
    FeatureSpec("export", "Export", "Live chat export to TXT/JSON"),
    FeatureSpec("search", "Search", "Live global Telegram search"),
    FeatureSpec("ghost", "Ghost Tracker", "Profile-change monitoring"),
    FeatureSpec("hunter", "Username Hunter", "Username availability monitor + claim"),
    FeatureSpec("tracking", "Tracking", "Autosave / anti-delete / edit tracker"),
    FeatureSpec("logger", "Logger", "Chat logging + export"),
    FeatureSpec("backup", "Backup", "Zip DB/config/session backups"),
    FeatureSpec("system", "System Monitor", "CPU/RAM/disk/net/uptime/ping"),
)


async def register_default_features(central: CentralDatabase) -> int:
    """Upsert the catalog into the ``features`` table. Returns the count."""
    for spec in FEATURE_CATALOG:
        await central.register_feature(spec.key, title=spec.title, description=spec.description)
    logger.info("Registered %d features", len(FEATURE_CATALOG))
    return len(FEATURE_CATALOG)


async def seed_default_plan_features(central: CentralDatabase) -> None:
    """Seed ``plan_features`` for the built-in tiers **only if empty**.

    Maps each built-in plan's default module set to feature rows, so a fresh
    install has working tiers. Idempotent and non-destructive: a plan that
    already has feature links (e.g. edited by an admin) is left untouched.
    """
    for default_plan in DEFAULT_PLANS:
        plan = await central.get_plan(default_plan.name)
        if plan is None:
            continue
        existing = await central.get_plan_feature_keys(plan.id)
        if existing:
            continue  # admin-managed already; never overwrite
        for key in default_plan.modules:
            await central.attach_feature(plan.id, key)
    logger.info("Seeded default plan_features for built-in tiers")


async def bootstrap_registry(central: CentralDatabase) -> None:
    """One call for startup: register features, seed roles, seed plan_features."""
    await register_default_features(central)
    await central.seed_roles()
    await seed_default_plan_features(central)
