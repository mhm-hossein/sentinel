"""Per-instance worker process (the data plane).

One OS process serves exactly one *instance* (one selfbot account of one user)::

    python -m sentinel.worker --instance-id <id>

The worker loads the instance + its owner's plan and Gemini keys from the central
database, connects the instance's already-authorized Telethon client from
``…/instances/<id>/session.session`` (never interactively), builds a
:class:`~sentinel.context.UserContext`, and serves the full feature stack via
:func:`sentinel.runtime.serve`. It reports liveness + runtime metrics (active
asyncio tasks, active banners) to the ``instances`` table so the control-bot
worker-status panel and the supervisor can observe/restart it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Optional

from telethon import TelegramClient

from core.central import (
    AUTH_ACTIVE,
    AUTH_REVOKED,
    CentralDatabase,
    INST_CRASHED,
    INST_RUNNING,
    INST_STARTING,
    INST_STOPPED,
    USER_ACTIVE,
)
from core.plan import DEFAULT_PLAN_NAME, Plan
from core.store import SharedStore

from .context import UserContext, UserSettings
from .paths import InstancePaths
from .runtime import serve
from .usage import count_active_banners

logger = logging.getLogger("sentinel.worker")


def _load_dotenv() -> None:
    """Load a project ``.env`` if python-dotenv is present (non-override)."""
    try:
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001 - optional
        return
    load_dotenv(override=False)


def _heartbeat_seconds() -> int:
    """Heartbeat cadence in seconds (``SENTINEL_HEARTBEAT_SECONDS``, default 30)."""
    try:
        return max(5, int(os.environ.get("SENTINEL_HEARTBEAT_SECONDS", "30")))
    except (TypeError, ValueError):
        return 30


async def _heartbeat_loop(
    central: CentralDatabase, instance_id: int, db_path: str, interval: int
) -> None:
    """Stamp heartbeat + runtime metrics (active tasks/banners) until cancelled."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                tasks = len(asyncio.all_tasks())
                banners = await asyncio.to_thread(count_active_banners, db_path)
                await central.heartbeat(
                    instance_id, active_tasks=tasks, active_banners=banners
                )
            except Exception:  # noqa: BLE001 - heartbeat must not kill the worker
                logger.debug("Heartbeat write failed for instance=%s", instance_id)
    except asyncio.CancelledError:
        raise


async def _run(instance_id: int) -> int:
    """Serve one instance; return a process exit code."""
    api_id = os.environ.get("SENTINEL_API_ID") or os.environ.get("BANNER_API_ID")
    api_hash = os.environ.get("SENTINEL_API_HASH") or os.environ.get("BANNER_API_HASH")
    if not api_id or not api_hash:
        logger.error("Missing SENTINEL_API_ID/SENTINEL_API_HASH; cannot start worker")
        return 2

    from .paths import central_db_path

    central = CentralDatabase(central_db_path())
    await central.connect()
    try:
        inst = await central.get_instance(instance_id)
        if inst is None:
            logger.error("No such instance %s in central DB", instance_id)
            return 3
        owner = await central.get_user_by_pk(inst.user_id)
        if owner is None or owner.status != USER_ACTIVE:
            logger.error("Instance %s owner inactive/missing", instance_id)
            return 4
        if not inst.enabled:
            logger.info("Instance %s is disabled; not starting", instance_id)
            return 6

        paths = InstancePaths.for_instance(owner.telegram_user_id, inst.id).ensure()
        plan = await central.plan_for_user_pk(owner.id) or Plan(name=DEFAULT_PLAN_NAME)
        gemini_keys = await central.get_active_gemini_keys(owner.id)

        await central.set_instance_status(instance_id, INST_STARTING, pid=os.getpid())

        client = TelegramClient(paths.session_name, int(api_id), str(api_hash))
        await client.connect()
        if not await client.is_user_authorized():
            logger.error("Instance %s session not authorized; needs re-onboarding", instance_id)
            await central.set_auth_state(instance_id, AUTH_REVOKED)
            await central.set_instance_status(instance_id, INST_CRASHED, error="session not authorized")
            await client.disconnect()
            return 5
        await central.set_auth_state(instance_id, AUTH_ACTIVE)

        me = await client.get_me()
        account_id = int(getattr(me, "id", 0)) or owner.telegram_user_id
        if inst.account_id != account_id:
            await central.set_instance_account(instance_id, account_id, auth_state=AUTH_ACTIVE)

        store = SharedStore(paths.db_path)
        await store.connect()

        ctx = UserContext(
            user_id=account_id,
            client=client,
            db_path=paths.db_path,
            store=store,
            plan=plan,
            paths=paths,
            settings=UserSettings(prefix=owner.prefix or "."),
            gemini_keys=gemini_keys,
            instance_id=inst.id,
            owner_id=owner.telegram_user_id,
        )

        hb_task: Optional[asyncio.Task] = None

        async def on_ready(_c: UserContext) -> None:
            nonlocal hb_task
            await central.set_instance_status(instance_id, INST_RUNNING, pid=os.getpid())
            hb_task = asyncio.create_task(
                _heartbeat_loop(central, instance_id, paths.db_path, _heartbeat_seconds()),
                name=f"heartbeat-{instance_id}",
            )

        async def on_teardown(_c: UserContext) -> None:
            if hb_task is not None:
                hb_task.cancel()
                try:
                    await hb_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await central.set_instance_status(instance_id, INST_STOPPED, pid=os.getpid())

        await serve(ctx, on_ready=on_ready, on_teardown=on_teardown)
        return 0
    except Exception as exc:  # noqa: BLE001 - record crash for the supervisor
        logger.exception("Worker for instance %s crashed", instance_id)
        try:
            await central.set_instance_status(instance_id, INST_CRASHED, error=str(exc)[:500])
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        await central.close()


def main(argv: Optional[list[str]] = None) -> int:
    """Parse ``--instance-id`` and run the worker; returns the exit code."""
    parser = argparse.ArgumentParser(description="Sentinel per-instance worker")
    parser.add_argument("--instance-id", type=int, required=True, help="Central instance id")
    parser.add_argument("--log-level", default=os.environ.get("BANNER_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    _load_dotenv()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args.instance_id))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker interrupted; exiting.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
