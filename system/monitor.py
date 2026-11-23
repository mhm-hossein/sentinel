"""System metrics collection for the VPS monitor.

Uses :mod:`psutil` when available (run off-thread to avoid blocking the event
loop) and an async TCP "ping" for latency. Service detection is best-effort and
only reports services that are actually installed/running.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass

logger = logging.getLogger("system.monitor")

try:  # psutil is optional; the module degrades gracefully without it.
    import psutil  # type: ignore
except Exception:  # noqa: BLE001 - absence is handled at runtime
    psutil = None  # type: ignore

#: Services the monitor knows how to detect, by display name -> process hints.
_KNOWN_SERVICES: dict[str, tuple[str, ...]] = {
    "Docker": ("dockerd", "docker"),
    "Xray": ("xray",),
    "Nginx": ("nginx",),
}


@dataclass(slots=True)
class PingResult:
    """Result of a latency probe.

    Attributes:
        average_ms: Mean round-trip time in milliseconds (``-1`` on failure).
        min_ms: Best round-trip time in milliseconds (``-1`` on failure).
        host: The probed host.
    """

    average_ms: float
    min_ms: float
    host: str


class SystemMonitor:
    """Collects host metrics asynchronously."""

    def __init__(self, *, ping_host: str = "8.8.8.8", ping_port: int = 53) -> None:
        """Initialise the monitor.

        Args:
            ping_host: Host to probe for latency.
            ping_port: TCP port to connect to for the probe.
        """
        self._ping_host = ping_host
        self._ping_port = ping_port

    @classmethod
    def from_env(cls) -> "SystemMonitor":
        """Build a monitor from environment variables (``PING_HOST``)."""
        host = os.environ.get("PING_HOST", "8.8.8.8").strip() or "8.8.8.8"
        return cls(ping_host=host)

    @property
    def available(self) -> bool:
        """Whether psutil-based metrics are available."""
        return psutil is not None

    # ------------------------------------------------------------------ #
    # Individual metrics (run psutil off the event loop)
    # ------------------------------------------------------------------ #
    async def cpu(self) -> str:
        """Return a compact CPU usage string."""
        if psutil is None:
            return "CPU: n/a"
        percent = await asyncio.to_thread(psutil.cpu_percent, 0.3)
        cores = psutil.cpu_count(logical=True) or 0
        return f"CPU: {percent:.0f}% ({cores} cores)"

    async def ram(self) -> str:
        """Return a compact RAM usage string."""
        if psutil is None:
            return "RAM: n/a"
        vm = await asyncio.to_thread(psutil.virtual_memory)
        return f"RAM: {vm.percent:.0f}% ({_gb(vm.used)}/{_gb(vm.total)} GB)"

    async def disk(self) -> str:
        """Return a compact disk usage string for the root/system drive."""
        if psutil is None:
            return "DISK: n/a"
        path = "C:\\" if os.name == "nt" else "/"
        usage = await asyncio.to_thread(psutil.disk_usage, path)
        return f"DISK: {usage.percent:.0f}% ({_gb(usage.used)}/{_gb(usage.total)} GB)"

    async def network(self) -> str:
        """Return a compact network I/O string (cumulative since boot)."""
        if psutil is None:
            return "NET: n/a"
        io = await asyncio.to_thread(psutil.net_io_counters)
        return f"NET: ↑{_gb(io.bytes_sent)} ↓{_gb(io.bytes_recv)} GB"

    async def uptime(self) -> str:
        """Return a compact system uptime string."""
        if psutil is None:
            return "UPTIME: n/a"
        boot = psutil.boot_time()
        return f"UPTIME: {_human_uptime(time.time() - boot)}"

    async def ping(self) -> PingResult:
        """Probe latency to the configured host with three TCP connects."""
        samples: list[float] = []
        for _ in range(3):
            value = await self._tcp_ping_once()
            if value >= 0:
                samples.append(value)
        if not samples:
            return PingResult(average_ms=-1.0, min_ms=-1.0, host=self._ping_host)
        return PingResult(
            average_ms=sum(samples) / len(samples),
            min_ms=min(samples),
            host=self._ping_host,
        )

    async def _tcp_ping_once(self) -> float:
        """Return one TCP-connect latency sample in ms, or ``-1`` on failure."""
        start = time.perf_counter()
        try:
            fut = asyncio.open_connection(self._ping_host, self._ping_port)
            reader, writer = await asyncio.wait_for(fut, timeout=3.0)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - close errors are irrelevant
                pass
            return (time.perf_counter() - start) * 1000.0
        except Exception:  # noqa: BLE001 - unreachable host -> failed sample
            return -1.0

    async def services(self) -> list[tuple[str, bool]]:
        """Detect known services that exist; report ``(name, running)``.

        Only services that are installed (binary present) or currently running
        are returned, per the spec's "only display existing services".

        Returns:
            A list of ``(service_name, is_running)`` tuples.
        """
        running_names = await asyncio.to_thread(self._running_process_names)
        result: list[tuple[str, bool]] = []
        for name, hints in _KNOWN_SERVICES.items():
            installed = any(shutil.which(h) for h in hints)
            running = any(h in running_names for h in hints)
            if installed or running:
                result.append((name, running))
        return result

    @staticmethod
    def _running_process_names() -> set[str]:
        """Return the set of lowercased running process names (psutil)."""
        if psutil is None:
            return set()
        names: set[str] = set()
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name:
                    names.add(name.removesuffix(".exe"))
            except Exception:  # noqa: BLE001 - process may vanish mid-iteration
                continue
        return names


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _gb(num_bytes: float) -> str:
    """Format a byte count as gigabytes with one decimal."""
    return f"{num_bytes / (1024 ** 3):.1f}"


def _human_uptime(seconds: float) -> str:
    """Format a duration as ``Nd Nh`` / ``Nh Nm`` / ``Nm``."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
