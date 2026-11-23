"""Instance supervisor — spawns and reconciles one process per selfbot instance.

The supervisor owns no tenant state. Each reconcile tick reads the central
database and drives the process table toward the desired state:

    * an *enabled* instance of an *active* user with no live process → spawn a
      worker (``python -m sentinel.worker --instance-id <id>``);
    * a live process whose instance is no longer desired → terminate it;
    * an instance with ``restart_requested`` set (user tapped 🔄) → terminate it
      so the reap loop respawns it;
    * a worker that exits while still desired → restart with exponential backoff.

Coordination with the control bot is entirely through the DB (desired vs. actual
state) — the two components never call each other directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

from core.central import AUTH_ACTIVE, INST_CRASHED, INST_STOPPED, CentralDatabase
from sentinel.paths import central_db_path

logger = logging.getLogger("sentinel.supervisor")


@dataclass(slots=True)
class _Child:
    """Bookkeeping for a running worker subprocess (keyed by instance id)."""

    process: asyncio.subprocess.Process
    started_at: float
    restarts: int = 0
    cooldown_until: float = 0.0


@dataclass(slots=True)
class SupervisorConfig:
    """Supervisor tuning."""

    reconcile_interval: float = 10.0
    max_workers: int = 0
    base_backoff: float = 5.0
    max_backoff: float = 300.0

    @classmethod
    def from_env(cls) -> "SupervisorConfig":
        """Build configuration from environment variables."""
        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        return cls(
            reconcile_interval=float(_int("SENTINEL_RECONCILE_SECONDS", 10)),
            max_workers=_int("SENTINEL_MAX_WORKERS", 0),
        )


class Supervisor:
    """Spawns, monitors and restarts per-instance worker processes."""

    def __init__(
        self, central: CentralDatabase, config: Optional[SupervisorConfig] = None,
        *, alerts: object = None,
    ) -> None:
        """Initialise the supervisor.

        Args:
            central: A connected central database.
            config: Optional tuning (defaults from env).
            alerts: Optional ``AlertManager`` (owner alerts on crash / restart loop).
        """
        self._central = central
        self._config = config or SupervisorConfig.from_env()
        self._alerts = alerts
        #: instance_id -> child bookkeeping.
        self._children: dict[int, _Child] = {}
        self._stopping = False
        #: Instance ids terminated on purpose (user restart / desired-state stop).
        #: Popped by _reap so deliberate SIGTERMs are never counted as crashes.
        self._expected_exits: set[int] = set()

    #: Restarts within a supervised instance before a "restart loop" alert fires.
    _RESTART_LOOP_THRESHOLD = 5

    async def _alert(self, kind: str, message: str) -> None:
        """Emit an owner alert if an AlertManager is wired (best-effort)."""
        if self._alerts is None:
            return
        try:
            await self._alerts.notify(kind, message)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Run the reconcile loop until :meth:`stop` is called."""
        logger.info(
            "Supervisor started (interval=%.0fs, max_workers=%s)",
            self._config.reconcile_interval, self._config.max_workers or "∞",
        )
        try:
            while not self._stopping:
                try:
                    await self._reconcile()
                    await self._reap()
                except Exception:  # noqa: BLE001 - never let the loop die
                    logger.exception("Reconcile tick failed")
                await asyncio.sleep(self._config.reconcile_interval)
        finally:
            await self._shutdown_all()

    def stop(self) -> None:
        """Signal the reconcile loop to stop and terminate all workers."""
        self._stopping = True

    # ------------------------------------------------------------------ #
    # Reconciliation
    # ------------------------------------------------------------------ #
    async def _reconcile(self) -> None:
        """Drive the process table toward the desired instance set."""
        desired = await self._central.list_runnable_instances()
        desired_by_id = {i.id: i for i in desired}

        # Stop workers whose instance is no longer desired.
        for iid in list(self._children):
            if iid not in desired_by_id:
                await self._stop_child(iid)

        # Honor explicit restart requests for live, desired instances.
        for iid, inst in desired_by_id.items():
            child = self._children.get(iid)
            if child is not None and child.process.returncode is None:
                if await self._central.consume_restart(iid):
                    logger.info("Restart requested for instance %s", iid)
                    self._expected_exits.add(iid)  # deliberate SIGTERM, not a crash
                    await self._terminate(child.process, iid)  # reap respawns it

        # Start workers for desired instances with no live process.
        for iid, inst in desired_by_id.items():
            child = self._children.get(iid)
            if child is not None and child.process.returncode is None:
                continue
            if not self._capacity_available():
                logger.warning("Worker cap reached; deferring start for instance %s", iid)
                continue
            if child is not None and time.monotonic() < child.cooldown_until:
                continue
            await self._spawn(iid, inst=inst, prior=child)

    def _capacity_available(self) -> bool:
        """Return ``True`` if another worker may be started."""
        if self._config.max_workers <= 0:
            return True
        live = sum(1 for c in self._children.values() if c.process.returncode is None)
        return live < self._config.max_workers

    @staticmethod
    def _log_path(inst) -> Optional[str]:
        """Return the per-instance ``worker.log`` path (alongside its db)."""
        if inst is None or not getattr(inst, "db_path", ""):
            return None
        return os.path.join(os.path.dirname(inst.db_path), "worker.log")

    async def _spawn(self, instance_id: int, *, inst=None, prior: Optional[_Child]) -> None:
        """Spawn a worker subprocess, redirecting output to a per-instance log."""
        cmd = [sys.executable, "-m", "sentinel.worker", "--instance-id", str(instance_id)]
        log_handle = None
        log_path = self._log_path(inst)
        if log_path:
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                log_handle = open(log_path, "ab", buffering=0)  # noqa: SIM115
            except OSError:
                log_handle = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_handle if log_handle else None,
                stderr=log_handle if log_handle else None,
                start_new_session=(os.name != "nt"),
            )
        except Exception:  # noqa: BLE001 - spawn failure is recorded, not fatal
            logger.exception("Failed to spawn worker for instance %s", instance_id)
            if log_handle:
                log_handle.close()
            return
        finally:
            # The child inherits the fd; the parent can close its handle.
            if log_handle:
                try:
                    log_handle.close()
                except OSError:
                    pass
        restarts = prior.restarts if prior else 0
        self._children[instance_id] = _Child(
            process=process, started_at=time.monotonic(), restarts=restarts
        )
        logger.info("Spawned worker for instance %s (pid=%s, restarts=%d)",
                    instance_id, process.pid, restarts)

    async def _reap(self) -> None:
        """Detect exited workers; restart (with backoff) if still desired."""
        for iid, child in list(self._children.items()):
            if child.process.returncode is None:
                continue
            code = child.process.returncode
            expected = iid in self._expected_exits
            self._expected_exits.discard(iid)
            if expected:
                logger.info("Worker for instance %s exited on purpose (code=%s)", iid, code)
            else:
                logger.warning("Worker for instance %s exited (code=%s)", iid, code)
            inst = await self._central.get_instance(iid)
            owner = await self._central.get_user_by_pk(inst.user_id) if inst else None
            still_desired = bool(
                inst and inst.enabled and owner and owner.status == "active"
                and inst.auth_state == AUTH_ACTIVE
            )
            if not still_desired:
                if inst and inst.auth_state != AUTH_ACTIVE and expected is False:
                    logger.info(
                        "Worker for instance %s exited while auth_state=%s; "
                        "dropping without crash accounting",
                        iid, inst.auth_state if inst else "?",
                    )
                self._children.pop(iid, None)
                if inst:
                    await self._central.set_instance_status(iid, INST_STOPPED)
                continue
            child.restarts += 1
            await self._central.increment_restart(iid)
            if code not in (0, None) and not expected:
                await self._central.set_instance_status(iid, INST_CRASHED, error=f"exit {code}")
                await self._alert("worker_crash", f"Instance {iid} crashed (exit {code}).")
            if child.restarts == self._RESTART_LOOP_THRESHOLD:
                await self._alert(
                    "restart_loop",
                    f"Instance {iid} has restarted {child.restarts} times — possible crash loop.",
                )
            backoff = min(
                self._config.max_backoff,
                self._config.base_backoff * (2 ** min(child.restarts, 6)),
            )
            child.cooldown_until = time.monotonic() + backoff
            logger.info("Restarting instance %s in %.0fs (attempt %d)", iid, backoff, child.restarts)

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #
    async def _stop_child(self, instance_id: int) -> None:
        """Terminate a worker and mark its instance stopped."""
        child = self._children.pop(instance_id, None)
        if child is None:
            return
        await self._terminate(child.process, instance_id)
        await self._central.set_instance_status(instance_id, INST_STOPPED)
        logger.info("Stopped worker for instance %s", instance_id)

    async def _terminate(self, process: asyncio.subprocess.Process, instance_id: int) -> None:
        """Gracefully terminate, then kill, a worker process."""
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001
            logger.debug("terminate() failed for instance %s", instance_id)
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass

    async def _shutdown_all(self) -> None:
        """Terminate every worker on supervisor shutdown."""
        logger.info("Supervisor shutting down %d worker(s)…", len(self._children))
        for iid in list(self._children):
            await self._stop_child(iid)


async def run_supervisor() -> None:
    """Standalone runner: connect central DB, install signals, run supervisor."""
    central = CentralDatabase(central_db_path())
    await central.connect()
    supervisor = Supervisor(central)

    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, supervisor.stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await supervisor.run()
    finally:
        await central.close()
