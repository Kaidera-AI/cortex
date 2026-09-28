"""Worker entrypoint: ``python -m cortex_v2.worker --role {doc,embed,graph}``.

The worker loop, role/executor capability mapping, queue claim/lease/fenced
publish semantics and profile-pinned database-URL validation live in
``cortex_v2.processing.runtime`` (Processing module ownership). This thin
entrypoint exists so every role shares one package invocation form across
compose files and never diverges from the runtime contract.
"""

from __future__ import annotations

from .processing.runtime import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
