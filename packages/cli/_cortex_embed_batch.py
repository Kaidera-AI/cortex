#!/usr/bin/env python3
"""Retired compatibility shim for Cortex embedding backfill.

Embedding generation now runs through the Cortex API:
  cortex-embed --table all --limit 100

This file intentionally does not call providers or databases directly.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "ERROR: _cortex_embed_batch.py is retired. "
        "Use cortex-embed, which calls /beat/embeddings/backfill.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
