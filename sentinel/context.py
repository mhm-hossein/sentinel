"""The per-user context injected into every feature module.

``UserContext`` replaces process-global state (the shared client, env-derived
paths, the global Gemini key, the process-wide prefix) with a single object
scoped to one tenant. A worker builds exactly one ``UserContext`` and hands it to
``sentinel.features.setup_features``; from there every module reads its client,
database, store, settings, plan and paths off the context.

The context does not *own* the client's connection lifecycle (the worker does) —
it is a typed bundle of already-constructed collaborators plus light helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from telethon import TelegramClient

from core.plan import Plan
from core.store import SharedStore

from .paths import UserPaths


@dataclass(slots=True)
class UserSettings:
    """Per-user runtime settings (thin convenience over the shared store).

    Attributes:
        prefix: Command prefix for this user (default ``"."``).
        default_ai_model: The user's preferred Gemini model (may be overridden
            by the value persisted in ``ai_settings``).
    """

    prefix: str = "."
    default_ai_model: str = "gemini-2.5-flash"


@dataclass(slots=True)
class UserContext:
    """Everything a feature module needs, scoped to one tenant.

    Attributes:
        user_id: The tenant's Telegram user id (canonical identity).
        client: This user's authorized Telethon client.
        db_path: Path to the user's per-tenant ``sentinel.db``.
        store: The user's :class:`~core.store.SharedStore` (settings + counters).
        settings: Per-user runtime settings.
        plan: The user's resolved subscription :class:`~core.plan.Plan`.
        gemini_keys: The user's Gemini API keys (rotated on failure).
        paths: The user's isolated filesystem layout.
    """

    user_id: int
    client: TelegramClient
    db_path: str
    store: SharedStore
    plan: Plan
    paths: UserPaths
    settings: UserSettings = field(default_factory=UserSettings)
    gemini_keys: list[str] = field(default_factory=list)
    #: Central-DB instance id this context serves (0 for single-user run_local).
    instance_id: int = 0
    #: Owner (panel user) Telegram id; equals ``user_id`` in single-user mode.
    owner_id: int = 0

    @property
    def prefix(self) -> str:
        """The command prefix for this user."""
        return self.settings.prefix

    @property
    def primary_gemini_key(self) -> str:
        """The first available Gemini key, or ``""`` if the user has none."""
        return self.gemini_keys[0] if self.gemini_keys else ""

    def allows(self, module: str) -> bool:
        """Return ``True`` if this user's plan enables ``module``."""
        return self.plan.allows(module)
