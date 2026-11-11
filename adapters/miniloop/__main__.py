"""``python -m miniloop`` entry point.

Works both as ``python -m miniloop`` (when ``adapters/`` is on sys.path) and
``python -m adapters.miniloop`` (repo root on sys.path) — relative imports
resolve under whichever package name was used.
"""

import sys

try:
    from .agent import main
except ImportError:  # pragma: no cover - defensive fallback
    from agent import main  # type: ignore[no-redef]

if __name__ == "__main__":
    sys.exit(main())
