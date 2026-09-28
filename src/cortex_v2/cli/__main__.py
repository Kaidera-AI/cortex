"""``python -m cortex_v2.cli`` entrypoint for the cortex2 CLI."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
