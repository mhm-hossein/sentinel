"""Single-user dev harness — run one tenant inline without the control plane.

This preserves the classic ``python start.py`` experience: an interactive login
(code + optional 2FA on first run) using the ``BANNER_*`` credentials, then the
full feature stack — but wired through a real :class:`UserContext` and the
per-user storage layout under ``data/users/<id>/``. It is the fastest way to
verify that existing command behavior is unchanged after the multi-user
conversion.

Run with::

    python -m sentinel.run_local
    python start.py            # thin shim → this module
"""

from __future__ import annotations

import asyncio
import logging
import os

from telethon import TelegramClient

from core.plan import DEFAULT_PLAN_NAME, Plan
from core.store import SharedStore

from .context import UserContext, UserSettings
from .paths import UserPaths
from .runtime import serve

logger = logging.getLogger("sentinel.run_local")


def _load_dotenv() -> None:
    """Load a project ``.env`` if python-dotenv is present (non-override)."""
    try:
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001 - optional
        return
    load_dotenv(override=False)


def _configure_logging() -> None:
    """Configure logging using the existing banner helper when available."""
    level = getattr(logging, os.environ.get("BANNER_LOG_LEVEL", "INFO").upper(), logging.INFO)
    try:
        from banner.utils import configure_logging

        configure_logging(level)
    except Exception:  # noqa: BLE001 - fall back to basicConfig
        logging.basicConfig(level=level)


async def _run() -> None:
    """Build one local :class:`UserContext` and serve it."""
    api_id = os.environ.get("SENTINEL_API_ID") or os.environ.get("BANNER_API_ID")
    api_hash = os.environ.get("SENTINEL_API_HASH") or os.environ.get("BANNER_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError(
            "Missing API credentials. Set BANNER_API_ID/BANNER_API_HASH (or "
            "SENTINEL_API_ID/SENTINEL_API_HASH) in your .env."
        )
    phone = os.environ.get("BANNER_PHONE") or None

    # Authenticate first (interactive) using the legacy session location so an
    # existing dev login is reused; the tenant id is only known post-login.
    session_name = os.environ.get("BANNER_SESSION", "banner")
    client = TelegramClient(session_name, int(api_id), str(api_hash))
    await client.start(phone=phone)
    me = await client.get_me()
    user_id = int(me.id)
    logger.info("Local login as %s (id=%s)", getattr(me, "username", "?"), user_id)

    # Per-user storage + store.
    paths = UserPaths.for_user(user_id).ensure()
    store = SharedStore(paths.db_path)
    await store.connect()

    # Local dev plan: unlimited (every module, no caps) unless overridden.
    plan = Plan(name=os.environ.get("SENTINEL_DEFAULT_PLAN", DEFAULT_PLAN_NAME))

    gemini = os.environ.get("GEMINI_API_KEY", "").strip()
    ctx = UserContext(
        user_id=user_id,
        client=client,
        db_path=paths.db_path,
        store=store,
        plan=plan,
        paths=paths,
        settings=UserSettings(prefix=os.environ.get("BANNER_PREFIX", ".")),
        gemini_keys=[gemini] if gemini else [],
    )
    logger.info("Local context ready (data at %s)", paths.root)
    await serve(ctx)


def main() -> None:
    """Entry point: configure logging, load env and run one local tenant."""
    _load_dotenv()
    _configure_logging()
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Interrupted; exiting.")


if __name__ == "__main__":
    main()
