"""Live monitoring collectors for the admin dashboard + instance panels.

Reads aggregate metrics from :class:`~core.central.CentralDatabase` and live
per-process metrics (memory/CPU) via ``psutil``, plus filesystem sizes and
per-instance target counts (from each instance's ``sentinel.db``, read-only).
Nothing here mutates state; all readers degrade gracefully (missing psutil,
dead pid, absent file) so the panels never crash.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from core.central import CentralDatabase, INST_RUNNING, SUB_ACTIVE, SUB_EXPIRED
from sentinel.usage import dir_size, file_size, read_targets

try:  # psutil is optional; metrics degrade to None without it.
    import psutil  # type: ignore
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore


@dataclass(slots=True)
class Overview:
    """Platform-wide dashboard metrics."""

    total_users: int = 0
    active_subscriptions: int = 0
    expired_subscriptions: int = 0
    active_workers: int = 0
    stopped_workers: int = 0
    total_instances: int = 0
    running_instances: int = 0
    total_gemini_keys: int = 0
    enabled_gemini_keys: int = 0
    total_databases: int = 0
    total_storage_bytes: int = 0
    avg_worker_memory_mb: Optional[float] = None
    avg_worker_uptime_seconds: Optional[float] = None


@dataclass(slots=True)
class InstanceMetrics:
    """Live per-instance metrics for the monitoring panel."""

    instance_id: int
    account_id: Optional[int]
    status: str
    uptime_seconds: Optional[float]
    memory_mb: Optional[float]
    cpu_percent: Optional[float]
    last_heartbeat: Optional[float]
    db_size: int
    session_size: int
    active_banners: int
    archive_targets: int
    ghost_targets: int
    hunter_targets: int


def process_memory_mb(pid: Optional[int]) -> Optional[float]:
    """Return a process's RSS in MB (``None`` if unavailable)."""
    if not pid or psutil is None:
        return None
    try:
        return psutil.Process(pid).memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 - process gone / no perms
        return None


def process_cpu_percent(pid: Optional[int]) -> Optional[float]:
    """Return a process's instantaneous CPU% (``None`` if unavailable)."""
    if not pid or psutil is None:
        return None
    try:
        return psutil.Process(pid).cpu_percent(interval=0.0)
    except Exception:  # noqa: BLE001
        return None


async def gather_overview(central: CentralDatabase) -> Overview:
    """Collect the platform overview (Phase 1 dashboard), live from the DB."""
    ov = Overview()
    ov.total_users = await central.count_all_users()
    ov.active_subscriptions = await central.count_subscriptions_by_status(SUB_ACTIVE)
    ov.expired_subscriptions = await central.count_subscriptions_by_status(SUB_EXPIRED)
    ov.total_instances = await central.count_all_instances()
    ov.running_instances = await central.count_instances_by_status(INST_RUNNING)
    ov.total_gemini_keys = await central.count_all_gemini_keys()
    ov.enabled_gemini_keys = await central.count_all_gemini_keys(enabled=True)

    instances = await central.list_instances()
    ov.active_workers = sum(1 for i in instances if i.status == INST_RUNNING)
    ov.stopped_workers = sum(1 for i in instances if i.status != INST_RUNNING)

    now = time.time()
    storage = 0
    dbs = 0
    mem_samples: list[float] = []
    uptime_samples: list[float] = []
    for inst in instances:
        if inst.db_path and os.path.exists(inst.db_path):
            dbs += 1
        # Sum the whole per-instance directory (db + session + exports/backups/cache).
        root = os.path.dirname(inst.db_path) if inst.db_path else ""
        storage += dir_size(root) if root else 0
        if inst.status == INST_RUNNING:
            mem = process_memory_mb(inst.pid)
            if mem is not None:
                mem_samples.append(mem)
            if inst.started_at:
                uptime_samples.append(max(0.0, now - inst.started_at))
    # Count the central DB file too.
    if os.path.exists(central._path):  # noqa: SLF001 - path is a plain attribute
        dbs += 1
        storage += file_size(central._path)  # noqa: SLF001
    ov.total_databases = dbs
    ov.total_storage_bytes = storage
    ov.avg_worker_memory_mb = (sum(mem_samples) / len(mem_samples)) if mem_samples else None
    ov.avg_worker_uptime_seconds = (
        sum(uptime_samples) / len(uptime_samples) if uptime_samples else None
    )
    return ov


def instance_metrics(inst) -> InstanceMetrics:
    """Collect live metrics for one :class:`~core.central.InstanceRecord`."""
    now = time.time()
    running = inst.status == INST_RUNNING
    targets = read_targets(inst.db_path)
    return InstanceMetrics(
        instance_id=inst.id,
        account_id=inst.account_id,
        status=inst.status,
        uptime_seconds=(now - inst.started_at) if running and inst.started_at else None,
        memory_mb=process_memory_mb(inst.pid) if running else None,
        cpu_percent=process_cpu_percent(inst.pid) if running else None,
        last_heartbeat=inst.last_heartbeat,
        db_size=file_size(inst.db_path),
        session_size=file_size(inst.session_path),
        active_banners=targets["banners_active"],
        archive_targets=targets["archive_targets"],
        ghost_targets=targets["ghost_targets"],
        hunter_targets=targets["hunter_targets"],
    )
