"""``python -m sentinel.platform`` — boot the control plane.

This module is a thin alias for the official production entry point in
``start.py``. Both run the exact same startup sequence (`start.run_platform`),
so there is a single source of truth for platform boot logic — running

    python -m sentinel.platform

is equivalent to

    python start.py
"""

from __future__ import annotations

from start import main

if __name__ == "__main__":
    raise SystemExit(main())
