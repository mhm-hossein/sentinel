"""Deprecated legacy entry point — kept as a thin redirect.

Historically ``python -m banner.main`` launched the single-user self-bot. That
role is gone: Sentinel is now a multi-user platform. This module remains only so
the old command path keeps working, delegating to the current entry points:

    * ``python start.py``            — production control plane (recommended)
    * ``python -m sentinel.run_local`` — single-user development harness

The banner *feature* package (``banner.banner_manager``, ``banner.commands``,
``banner.database``, ``banner.scheduler``) is still very much alive — it is
wired per instance by ``sentinel.features.setup_features``. Only this legacy
launcher was retired.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("banner.main")


def main() -> None:
    """Delegate to the production platform entry point (with a deprecation note)."""
    logger.warning(
        "`banner.main` is deprecated; use `python start.py` (platform) or "
        "`python -m sentinel.run_local` (single-user dev). Delegating to start.main()."
    )
    from start import main as platform_main

    raise SystemExit(platform_main())


if __name__ == "__main__":
    main()
