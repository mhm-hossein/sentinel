"""Sentinel control plane: supervisor + self-serve control bot.

``python -m sentinel.platform`` boots both:

    * :class:`~sentinel.platform.supervisor.Supervisor` — spawns one worker
      subprocess per active user and reconciles desired vs. actual state.
    * :class:`~sentinel.platform.control_bot.ControlBot` — a Bot API bot that
      onboards new users (phone → code → 2FA) and exposes admin commands.

They coordinate purely through the central database (``users.status`` desired
vs. ``instances.status`` actual) — no extra IPC channel.
"""

from __future__ import annotations

__all__ = ["Supervisor", "ControlBot"]


def __getattr__(name: str):  # pragma: no cover - lazy to avoid heavy imports
    if name == "Supervisor":
        from .supervisor import Supervisor

        return Supervisor
    if name == "ControlBot":
        from .control_bot import ControlBot

        return ControlBot
    raise AttributeError(name)
