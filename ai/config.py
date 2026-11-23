"""Configuration for the AI Assistant module.

Values are read from environment variables (already populated from the project
``.env`` by the host bot at startup). Nothing here is logged or echoed back to
users, keeping secrets out of the chat and logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: Curated default Gemini models offered by ``.ai model``. ``gemini-2.5-flash``
#: is the default for all requests per the module specification.
DEFAULT_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
)

#: Default model used until the owner switches it.
DEFAULT_MODEL = "gemini-2.5-flash"

#: Default fallback models tried (in order) when the active model fails with a
#: transient/availability error. Extendable via ``AI_FALLBACK_MODELS``.
DEFAULT_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
)


@dataclass(slots=True)
class AIConfig:
    """Runtime configuration for the AI module.

    Attributes:
        api_key: Gemini API key (``GEMINI_API_KEY``). Empty disables the module.
        default_model: Model used when no choice is persisted yet.
        models: Ordered list of selectable model names.
        max_file_size_mb: Upper bound on downloadable file size.
        rate_limit_per_minute: Allowed requests per user per rolling minute.
        request_timeout: Per-request Gemini timeout in seconds.
        max_input_chars: Hard cap on text sent to Gemini (graceful truncation).
        db_path: SQLite database file shared with the rest of the bot.
    """

    api_key: str
    default_model: str = DEFAULT_MODEL
    models: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    fallback_models: list[str] = field(
        default_factory=lambda: list(DEFAULT_FALLBACK_MODELS)
    )
    max_file_size_mb: int = 20
    rate_limit_per_minute: int = 10
    request_timeout: int = 60
    max_input_chars: int = 30_000
    db_path: str = "sentinel.db"

    @property
    def max_file_size_bytes(self) -> int:
        """Configured maximum file size expressed in bytes."""
        return self.max_file_size_mb * 1024 * 1024

    def fallback_chain(self, current: str) -> list[str]:
        """Return the ordered model chain to try, starting with ``current``.

        Args:
            current: The active/primary model.

        Returns:
            ``[current, *fallbacks]`` with duplicates removed (order preserved).
        """
        chain: list[str] = []
        for name in (current, *self.fallback_models):
            if name and name not in chain:
                chain.append(name)
        return chain

    @classmethod
    def from_context(cls, ctx: object) -> "AIConfig":
        """Build configuration for a single tenant from its ``UserContext``.

        The base configuration is still read from the environment (models, limits,
        timeouts), but the API key comes from the user's own ``gemini_keys`` and
        the database path from the user's per-tenant ``db_path`` — never the
        process-global ``GEMINI_API_KEY`` / shared DB. ``ctx`` is duck-typed
        (``gemini_keys``, ``db_path``, optional ``plan``/``settings``) so this
        module stays decoupled from the platform layer.

        Args:
            ctx: The user's context (``sentinel.context.UserContext``).

        Returns:
            A per-user :class:`AIConfig`.
        """
        config = cls.from_env()
        keys = list(getattr(ctx, "gemini_keys", []) or [])
        config.api_key = keys[0] if keys else ""
        config.db_path = getattr(ctx, "db_path", config.db_path)

        # Per-user preferred model (from UserSettings), if provided.
        settings = getattr(ctx, "settings", None)
        preferred = getattr(settings, "default_ai_model", "") if settings else ""
        if preferred:
            config.default_model = preferred
            if preferred not in config.models:
                config.models.insert(0, preferred)

        # Plan-driven per-user rate limit override (optional).
        plan = getattr(ctx, "plan", None)
        if plan is not None:
            try:
                per_min = plan.limit_for("ai_requests_per_minute")
                if per_min and per_min > 0:
                    config.rate_limit_per_minute = per_min
            except Exception:  # noqa: BLE001 - plan is best-effort here
                pass
        return config

    @classmethod
    def from_env(cls) -> "AIConfig":
        """Build configuration from environment variables.

        Recognised variables::

            GEMINI_API_KEY, DEFAULT_AI_MODEL, AI_MODELS,
            MAX_FILE_SIZE_MB, AI_RATE_LIMIT_PER_MINUTE,
            AI_REQUEST_TIMEOUT, AI_MAX_INPUT_CHARS, BANNER_DB_PATH

        ``AI_MODELS`` is an optional comma-separated list that extends the
        curated defaults, enabling new models to be added purely via config.
        The database path is resolved the same way as the rest of Sentinel
        (``SENTINEL_DB_PATH`` -> ``BANNER_DB_PATH`` -> ``DB_PATH``).

        Returns:
            A populated :class:`AIConfig`.
        """
        default_model = os.environ.get("DEFAULT_AI_MODEL", DEFAULT_MODEL).strip()

        # Resolve the shared DB path consistently with core.migrations.
        try:
            from core.migrations import resolve_db_path

            db_path = resolve_db_path()
        except Exception:  # noqa: BLE001 - fall back if core is unavailable
            db_path = os.environ.get("BANNER_DB_PATH", "sentinel.db")

        models = list(DEFAULT_MODELS)
        extra = os.environ.get("AI_MODELS", "")
        for name in (m.strip() for m in extra.split(",")):
            if name and name not in models:
                models.append(name)
        if default_model and default_model not in models:
            models.insert(0, default_model)

        # Fallback chain: defaults plus any AI_FALLBACK_MODELS entries.
        fallback_models = list(DEFAULT_FALLBACK_MODELS)
        fb_extra = os.environ.get("AI_FALLBACK_MODELS", "")
        for name in (m.strip() for m in fb_extra.split(",")):
            if name and name not in fallback_models:
                fallback_models.append(name)
        # Fallback targets must be selectable too.
        for name in fallback_models:
            if name not in models:
                models.append(name)

        return cls(
            api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
            default_model=default_model or DEFAULT_MODEL,
            models=models,
            fallback_models=fallback_models,
            max_file_size_mb=_int_env("MAX_FILE_SIZE_MB", 20),
            rate_limit_per_minute=_int_env("AI_RATE_LIMIT_PER_MINUTE", 10),
            request_timeout=_int_env("AI_REQUEST_TIMEOUT", 60),
            max_input_chars=_int_env("AI_MAX_INPUT_CHARS", 30_000),
            db_path=db_path,
        )


def _int_env(name: str, default: int) -> int:
    """Read an integer environment variable, falling back on parse failure."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
