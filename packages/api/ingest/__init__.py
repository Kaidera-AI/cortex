"""Cortex ingest write-side transforms (memory-efficiency E2 + E4).

Pure-stdlib packages wired into the ingest endpoints behind config flags:
  * ``symbolic_compact`` (E4) — deterministic Telegraph/Caveman-style text
    compaction; near-lossless, no model, round-trippable for recall benchmarking.
  * ``chat_distill`` (E2) — turn/exchange-level commitment extraction with an
    always-keep floor (file paths, verify criteria, role-lane, tool results).
  * ``recall_gate`` — the dry-run benchmark harness (samples sessions, re-ingests
    into a temp project, measures fact recall + commitment preservation).

Read/search (E1/E3/E5) lives outside this package; this package touches storage
and ingest code only. Flags ``CORTEX_E2_DISTILL`` / ``CORTEX_E4_COMPACT`` are OFF by
default — with both off, ingest output is byte-identical to the pre-change baseline.

No heavy imports at package load (compression is lazy + stdlib-fallback) so the
API process never gains a hard dependency.
"""

from .symbolic_compact import compact_text, is_compactable  # noqa: F401
from .chat_distill import distill_message, is_always_keep  # noqa: F401

__all__ = ["compact_text", "is_compactable", "distill_message", "is_always_keep"]
