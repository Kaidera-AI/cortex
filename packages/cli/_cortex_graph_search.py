#!/usr/bin/env python3
"""Retired host-side graph search worker.

Use ``cortex-graph-search`` so Layer 4 retrieval stays behind cortex-api.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "ERROR: _cortex_graph_search.py is retired; use cortex-graph-search "
        "or GET /cortex-graph-search through cortex-api.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
