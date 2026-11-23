"""Shared per-user run loop used by both the worker and the dev harness.

``serve(ctx)`` wires the user's feature stack, runs the client until it
disconnects, then tears everything down cleanly. It is deliberately agnostic to
*how* the client was created/authorized (interactively in ``run_local`` or from
a stored session in the worker).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from .context import UserContext
from .features import setup_features

logger = logging.getLogger("sentinel.runtime")


async def serve(
    ctx: UserContext,
    *,
    on_ready: Optional[Callable[[UserContext], Awaitable[None]]] = None,
    on_teardown: Optional[Callable[[UserContext], Awaitable[None]]] = None,
) -> None:
    """Run one tenant to completion.

    Args:
        ctx: The fully-built user context (client + store already connected).
        on_ready: Optional hook awaited once modules are wired (e.g. mark the
            instance ``running`` and start a heartbeat).
        on_teardown: Optional hook awaited during shutdown, before the client
            disconnects (e.g. mark the instance ``stopped``).
    """
    teardown = await setup_features(ctx)
    logger.info("User %s: online", ctx.user_id)
    if on_ready is not None:
        try:
            await on_ready(ctx)
        except Exception:  # noqa: BLE001 - readiness hook is best-effort
            logger.exception("User %s: on_ready hook failed", ctx.user_id)
    try:
        await ctx.client.run_until_disconnected()
    finally:
        logger.info("User %s: shutting down…", ctx.user_id)
        if on_teardown is not None:
            try:
                await on_teardown(ctx)
            except Exception:  # noqa: BLE001 - teardown hook is best-effort
                logger.debug("User %s: on_teardown hook failed", ctx.user_id)
        try:
            await teardown()
        except Exception:  # noqa: BLE001 - feature teardown is best-effort
            logger.exception("User %s: feature teardown failed", ctx.user_id)
        try:
            await ctx.client.disconnect()
        except Exception:  # noqa: BLE001
            logger.debug("User %s: client disconnect failed", ctx.user_id)
