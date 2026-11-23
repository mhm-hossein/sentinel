"""Sentinel Platform — official production entry point.

Run the whole platform with::

    python start.py

This performs an ordered, diagnosed startup and then keeps the event loop alive:

    1. Validate environment (API credentials, control-bot token, owner id).
    2. Validate + open the central database.
    3. Bootstrap the feature registry (auto-create feature rows).
    4. Bootstrap RBAC (seed roles + permissions, default plan_features).
    5. Bootstrap the owner account (SENTINEL_OWNER_ID → full access).
    6. Start the Subscription Checker (expiry enforcement loop).
    7. Start the Alert Manager (owner alerts + health loop).
    8. Start the Supervisor (one worker process per runnable instance).
    9. Start the Control Bot (onboarding + user/admin panels).
    10. Keep the event loop alive until Ctrl+C / SIGTERM.

Each step prints a ``✓``/``✗`` diagnostic line under a startup banner, so an
operator can see exactly how far boot progressed.

Development / single-user debugging uses a different entry point — see
``python -m sentinel.run_local`` (documented in the README). ``start.py`` is the
production launcher for the multi-user control plane.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger("sentinel.start")

#: Public platform version surfaced in the startup banner.
try:  # keep the banner working even if core import order changes
    from core import __version__ as _CORE_VERSION
except Exception:  # noqa: BLE001
    _CORE_VERSION = "3.x"

_BANNER = r"""
   ____             _   _            _
  / ___|  ___ _ __ | |_(_)_ __   ___| |
  \___ \ / _ \ '_ \| __| | '_ \ / _ \ |   Sentinel Platform
   ___) |  __/ | | | |_| | | | |  __/ |   multi-user · multi-instance
  |____/ \___|_| |_|\__|_|_| |_|\___|_|   control plane v{version}
"""


def _enable_utf8_output() -> None:
    """Best-effort switch stdout/stderr to UTF-8 so ``✓``/emoji never crash.

    Windows consoles default to cp1252, which cannot encode the diagnostic
    glyphs; reconfiguring (Python 3.7+) avoids a ``UnicodeEncodeError`` at boot.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - not all streams support reconfigure
            pass


def _out(text: str) -> None:
    """Print a line, degrading to ASCII if the console can't encode it."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _load_dotenv() -> None:
    """Load a project ``.env`` if python-dotenv is present (non-override)."""
    try:
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001 - optional
        return
    load_dotenv(override=False)


def _configure_logging() -> None:
    """Configure root logging from ``BANNER_LOG_LEVEL`` (default INFO)."""
    level = getattr(logging, os.environ.get("BANNER_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _diag(ok: bool, label: str, detail: str = "") -> None:
    """Print a single ``✓``/``✗`` startup-diagnostic line."""
    mark = "✓" if ok else "✗"
    line = f"  {mark} {label}"
    if detail:
        line += f" — {detail}"
    _out(line)


def _owner_id() -> int:
    """Return the platform owner Telegram id (``SENTINEL_OWNER_ID``, or 0)."""
    try:
        return int(os.environ.get("SENTINEL_OWNER_ID", "0"))
    except (TypeError, ValueError):
        return 0


def validate_environment() -> tuple[str, str, str]:
    """Validate required environment variables.

    Returns:
        ``(api_id, api_hash, control_bot_token)``.

    Raises:
        RuntimeError: If required credentials are missing.
    """
    api_id = os.environ.get("SENTINEL_API_ID") or os.environ.get("BANNER_API_ID")
    api_hash = os.environ.get("SENTINEL_API_HASH") or os.environ.get("BANNER_API_HASH")
    token = os.environ.get("CONTROL_BOT_TOKEN", "").strip()
    if not api_id or not api_hash:
        raise RuntimeError(
            "Missing API credentials — set SENTINEL_API_ID and SENTINEL_API_HASH "
            "in your .env (get them from https://my.telegram.org)."
        )
    if not token:
        raise RuntimeError(
            "Missing CONTROL_BOT_TOKEN — create a bot with @BotFather and set "
            "CONTROL_BOT_TOKEN in your .env (required for the control plane)."
        )
    return str(api_id), str(api_hash), token


async def run_platform() -> None:
    """Boot every control-plane component with diagnostics and run until stop.

    This is the single source of truth for platform startup; ``__main__`` and
    ``start.py`` both call it (no duplicated wiring).
    """
    import signal

    from core.central import CentralDatabase
    from sentinel.paths import central_db_path
    from sentinel.platform.alerts import AlertManager
    from sentinel.platform.control_bot import ControlBot
    from sentinel.platform.subscription_checker import SubscriptionChecker
    from sentinel.platform.supervisor import Supervisor
    from sentinel.registry import bootstrap_registry

    _out(_BANNER.format(version=_CORE_VERSION))
    _out("Starting Sentinel Platform…\n")

    # 1) Environment ----------------------------------------------------------
    try:
        api_id, api_hash, token = validate_environment()
        _diag(True, "Environment", "API credentials + control-bot token present")
    except RuntimeError as exc:
        _diag(False, "Environment", str(exc))
        raise

    owner = _owner_id()
    _diag(owner > 0, "Owner account",
          f"SENTINEL_OWNER_ID={owner}" if owner else "SENTINEL_OWNER_ID not set (no super-admin)")

    # 2) Central database -----------------------------------------------------
    db_path = central_db_path()
    central = CentralDatabase(db_path)
    await central.connect()
    if not await central.healthy():
        _diag(False, "Central database", db_path)
        raise RuntimeError(f"Central database not healthy at {db_path}")
    _diag(True, "Central database", db_path)

    # 3) Feature registry + 4) RBAC (+ default plan_features) -----------------
    await bootstrap_registry(central)
    feature_count = len(await central.list_features())
    _diag(True, "Feature registry", f"{feature_count} features registered")
    role_count = len(await central.list_roles()) if hasattr(central, "list_roles") else 3
    _diag(True, "RBAC", f"{role_count} roles seeded")

    # 5) Owner account --------------------------------------------------------
    await central.ensure_owner(owner)
    _diag(True, "Owner bootstrap", "owner has full access" if owner else "skipped (no owner id)")

    # Build components (control bot doubles as the alert sender) --------------
    bot = ControlBot(central, api_id=int(api_id), api_hash=str(api_hash), token=token)
    alerts = AlertManager(central, bot.send_message, owner)
    bot._alerts = alerts  # backup-failure alerts route through the manager
    supervisor = Supervisor(central, alerts=alerts)
    checker = SubscriptionChecker(central, alerts=alerts)

    # Shutdown plumbing -------------------------------------------------------
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: rely on KeyboardInterrupt

    # 6/7/8/9) Start the long-running components ------------------------------
    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(checker.run(), name="subscription-checker"))
    _diag(True, "Subscription Checker", "expiry enforcement loop started")
    tasks.append(asyncio.create_task(alerts.run(), name="alert-manager"))
    _diag(True, "Alert Manager", f"health loop started (owner={'set' if owner else 'unset'})")
    tasks.append(asyncio.create_task(supervisor.run(), name="supervisor"))
    _diag(True, "Supervisor", "reconciling instances → worker processes")
    tasks.append(asyncio.create_task(bot.run(), name="control-bot"))
    _diag(True, "Control Bot", "onboarding + user/admin panels online")

    tasks.append(asyncio.create_task(stop_event.wait(), name="signal-wait"))

    _out("\n🛰  Platform Ready — press Ctrl+C to stop.\n")
    logger.info("Sentinel platform running (db=%s, owner=%s)", db_path, owner)

    # 10) Keep alive; exit when any component stops or a signal fires ---------
    failure: Optional[BaseException] = None
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None and task.get_name() != "signal-wait":
                # A real component crashed (not a signal): surface it so the
                # process exits non-zero and systemd Restart=on-failure fires.
                logger.exception(
                    "Component %r failed; shutting down the platform",
                    task.get_name(), exc_info=exc,
                )
                if failure is None:
                    failure = exc
    finally:
        _out("\nShutting down Sentinel Platform…")
        supervisor.stop()
        checker.stop()
        alerts.stop()
        try:
            await bot.stop()
        except Exception:  # noqa: BLE001
            logger.debug("Control bot stop failed")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await central.close()
        logger.info("Shutdown complete.")
    if failure is not None:
        raise failure


def _check_dependencies() -> bool:
    """Verify third-party deps are importable *by this interpreter*.

    The most common startup failure is launching ``start.py`` with a Python that
    doesn't have the requirements installed (e.g. a bare ``uv`` managed Python
    instead of the project venv). Turn that raw ``ModuleNotFoundError`` into a
    clear, actionable message naming the interpreter in use.
    """
    required = ("aiosqlite", "telethon", "aiohttp")
    missing = []
    for name in required:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if not missing:
        return True
    py = sys.executable
    _out("")
    _diag(False, "Dependencies", f"missing: {', '.join(missing)}")
    _out(
        f"\nThis interpreter has no project dependencies:\n    {py}\n\n"
        "Install them into THIS interpreter, or run with the project venv:\n\n"
        "    # create + install once\n"
        "    uv venv --python 3.12 .venv\n"
        "    uv pip install --python .venv -r requirements.txt\n\n"
        "    # then always launch with the venv Python\n"
        "    .venv\\Scripts\\python start.py      (Windows)\n"
        "    .venv/bin/python start.py           (Linux/macOS)\n"
    )
    return False


def main() -> int:
    """Configure logging, load env and run the platform. Returns an exit code."""
    _enable_utf8_output()
    _load_dotenv()
    _configure_logging()
    if not _check_dependencies():
        return 1
    try:
        asyncio.run(run_platform())
        return 0
    except (KeyboardInterrupt, SystemExit):
        logger.info("Interrupted; exiting.")
        return 0
    except RuntimeError as exc:
        # Configuration/validation error already printed a ✗ diagnostic line.
        _out(f"\n✗ Startup aborted: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
