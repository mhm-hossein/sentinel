"""Sentinel multi-user platform layer.

This package hosts the control plane (supervisor + control bot), the per-user
worker, and the tenant abstractions (`UserContext`, `UserPaths`) that let the
existing single-user feature modules run once per user in full isolation.

Layering (extends the existing star topology):

    * ``sentinel.paths``   -- per-user filesystem layout (``data/users/<id>/``).
    * ``sentinel.context`` -- ``UserContext``: the single object every feature
      module receives instead of process-global state.
    * ``sentinel.worker``  -- data plane: one OS process per user.
    * ``sentinel.platform`` -- control plane: supervisor + self-serve control bot.

``core`` (migrations/store/stats) and every feature module remain unchanged in
spirit — they are simply fed from a ``UserContext`` rather than the environment.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Platform layer version (independent of the feature ``core.__version__``).
__version__ = "3.0.0"
