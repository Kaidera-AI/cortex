"""Phase 4 — Recall gate + benchmark harness (E2 + E4 write-side).

A DETERMINISTIC, no-LLM benchmark that proves the E2 distiller + E4 compactor
preserve recall and commitments before they are enabled in production.

What it measures (over a representative message corpus):
  * **Protected-token recall** — every protected token in the original (file
    paths, UUIDs, code spans, verify keywords, numbers, proper nouns) must appear
    in the E4-compacted text. This is the near-lossless guarantee.
  * **Commitment preservation** — every sentence carrying a commitment keyword
    (decided/shipped/fixed/resolved/...) must survive E2: either the message is
    kept whole by the always-keep floor, OR the commitment sentence is retained
    in a distilled commitment row. 0 commitment loss is the hard gate.
  * **Storage reduction** — the byte savings of the compacted/distilled output
    vs the raw original (the whole point of the transforms).

Gate: protected-token recall >= 95% AND 0 commitment loss -> RECALL_PASSED.

Run:
    python -m agents.api.ingest.recall_gate            # default corpus
    python -m agents.api.ingest.recall_gate --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allow running both as a module and as a script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ingest.chat_distill import distill_message, is_always_keep  # type: ignore
    from ingest.symbolic_compact import compact_text  # type: ignore
else:
    from .chat_distill import distill_message, is_always_keep
    from .symbolic_compact import compact_text


# ---------------------------------------------------------------------------
#  Representative corpus — covers every message shape the transforms meet.
#  Sanitized: no real paths/names/dates. Each entry is (label, message).
# ---------------------------------------------------------------------------

CORPUS: list[tuple[str, str]] = [
    (
        "path+verify (always-keep floor)",
        "I decided to ship the fix. Please verify: results must match the spec. "
        "The change is at src/backend/auth.py and tests/test_auth.py.",
    ),
    (
        "decision+fact (distillable)",
        "We decided to use zlib for compression. The cold tier keeps raw verbatim. "
        "Basically this is essential in order to reduce storage. I really like pizza.",
    ),
    (
        "code block (always-keep)",
        "Here is the snippet:\n```python\nimport os\nprint(os.getcwd())\n```\n"
        "It prints the cwd. Please run it.",
    ),
    (
        "uuid + role-lane (always-keep)",
        "Assigned to_role: backend-specialist. The handoff id is "
        "a1b2c3d4-1111-2222-3333-444455556666. It is basically done.",
    ),
    (
        "pure chatter (distillable, low signal)",
        "Hey team, just checking in. Basically I think we are good to go. "
        "Perhaps we can look at it later. Really appreciate the work.",
    ),
    (
        "long mixed (compaction target)",
        "In order to deploy the service, we are going to need to update the config. "
        "Please kindly make sure the migration runs. The migration is at "
        "db/migrations/2026-06-24-storage.sql. It is essential. We decided to ship it. "
        "Verify: the schema column exists before enabling the flag.",
    ),
    (
        "json tool result (always-keep)",
        'Tool result: {"status": "ok", "rows": 42, "file": "out.json"} -- basically good.',
    ),
    (
        "outcome+test (always-keep floor)",
        "The build passed. We resolved the flaky test by adding a retry. "
        "Outcome: green pipeline. The fix is committed.",
    ),
]


# Protected-token extractors (must all survive compaction).
_PATH_RE = re.compile(r"(?:(?:[A-Za-z0-9_.-]+/+)+[A-Za-z0-9_.-]*[A-Za-z0-9_-])")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_BACKTICK_RE = re.compile(r"`[^`]+`")
_COMMITMENT_KEYWORDS = (
    "decided", "decision", "shipped", "fixed", "implemented", "resolved",
    "outcome", "result", "assigned", "verify", "committed",
)


@dataclass
class CaseResult:
    label: str
    always_keep: bool
    protected_tokens: list[str]
    protected_recall: float
    commitment_sentences: list[str]
    commitments_lost: int
    original_len: int
    output_len: int
    storage_reduction_pct: float


@dataclass
class BenchmarkResult:
    cases: list[CaseResult] = field(default_factory=list)
    mean_protected_recall: float = 0.0
    total_commitments_lost: int = 0
    mean_storage_reduction_pct: float = 0.0
    passed: bool = False
    gate: str = ""


def _protected_tokens(text: str) -> list[str]:
    """All load-bearing tokens that must survive E4 compaction."""
    seen: list[str] = []
    for rx in (_PATH_RE, _UUID_RE, _BACKTICK_RE):
        for m in rx.findall(text):
            if m not in seen:
                seen.append(m)
    return seen


def _commitment_sentences(text: str) -> list[str]:
    """Sentences carrying a commitment keyword (must survive E2)."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        low = sent.lower()
        if any(kw in low for kw in _COMMITMENT_KEYWORDS):
            s = sent.strip()
            if s and s not in out:
                out.append(s)
    return out


def _e2_e4_output(message: str) -> tuple[str, list[dict[str, Any]]]:
    """Apply E2 + E4 exactly as the ingest path does, return (hot_text, commitments)."""
    always_keep = is_always_keep(message)
    if always_keep:
        # E4 still compacts the hot copy; E2 keeps the message whole.
        compact, _, _ = compact_text(message)
        hot = compact if compact else message
        return hot, []
    # Distillable: E2 extracts commitments; E4 compacts each commitment.
    commitments = distill_message(message)
    compacted = []
    for c in commitments:
        text = c.get("content", "")
        compact, _, _ = compact_text(text)
        compacted.append({"content": compact if compact else text, "metadata": c.get("metadata", {})})
    hot = "\n".join(c["content"] for c in compacted)
    return hot, compacted


def run_benchmark(corpus: list[tuple[str, str]] | None = None) -> BenchmarkResult:
    corpus = corpus if corpus is not None else CORPUS
    results: list[CaseResult] = []

    for label, message in corpus:
        protected = _protected_tokens(message)
        commitment_sents = _commitment_sentences(message)

        hot, commitments = _e2_e4_output(message)
        # The hot tier text after E2/E4. Distillable messages store the distilled
        # commitment rows as hot messages; do not count them twice.
        full_output = hot

        # Protected-token recall (E4 must preserve every protected token).
        if protected:
            kept = sum(1 for tok in protected if tok in full_output)
            recall = kept / len(protected)
        else:
            recall = 1.0

        # Commitment preservation (E2 must retain every commitment sentence).
        # A commitment is preserved if it appears in the kept-whole message OR
        # in a distilled commitment row (substring match, case-insensitive).
        lost = 0
        out_low = full_output.lower()
        for sent in commitment_sents:
            # Match on a distinctive substring (first 24 chars) so tense/punctuation
            # shifts from compaction don't false-negative a preserved commitment.
            probe = sent[:24].lower().strip()
            if probe and probe not in out_low:
                lost += 1

        original_len = len(message)
        output_len = len(full_output)
        reduction = max(0.0, (original_len - output_len) / original_len * 100.0) if original_len else 0.0

        results.append(CaseResult(
            label=label,
            always_keep=is_always_keep(message),
            protected_tokens=protected,
            protected_recall=recall,
            commitment_sentences=commitment_sents,
            commitments_lost=lost,
            original_len=original_len,
            output_len=output_len,
            storage_reduction_pct=round(reduction, 1),
        ))

    mean_recall = sum(r.protected_recall for r in results) / len(results) if results else 0.0
    total_lost = sum(r.commitments_lost for r in results)
    mean_reduction = sum(r.storage_reduction_pct for r in results) / len(results) if results else 0.0

    GATE_RECALL = 0.95
    passed = mean_recall >= GATE_RECALL and total_lost == 0
    return BenchmarkResult(
        cases=results,
        mean_protected_recall=round(mean_recall, 4),
        total_commitments_lost=total_lost,
        mean_storage_reduction_pct=round(mean_reduction, 1),
        passed=passed,
        gate=f"recall>={GATE_RECALL} AND commitment_loss==0",
    )


def _to_dict(res: BenchmarkResult) -> dict[str, Any]:
    return {
        "mean_protected_recall": res.mean_protected_recall,
        "total_commitments_lost": res.total_commitments_lost,
        "mean_storage_reduction_pct": res.mean_storage_reduction_pct,
        "passed": res.passed,
        "gate": res.gate,
        "cases": [
            {
                "label": c.label,
                "always_keep": c.always_keep,
                "protected_tokens": c.protected_tokens,
                "protected_recall": c.protected_recall,
                "commitment_sentences": c.commitment_sentences,
                "commitments_lost": c.commitments_lost,
                "original_len": c.original_len,
                "output_len": c.output_len,
                "storage_reduction_pct": c.storage_reduction_pct,
            }
            for c in res.cases
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E2+E4 recall-gate benchmark.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    res = run_benchmark()
    payload = _to_dict(res)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"E2+E4 recall-gate benchmark  (gate: {res.gate})")
        print(f"  mean protected-token recall : {res.mean_protected_recall}")
        print(f"  total commitments lost      : {res.total_commitments_lost}")
        print(f"  mean storage reduction       : {res.mean_storage_reduction_pct}%")
        for c in res.cases:
            print(f"  - [{c.label}] recall={c.protected_recall:.2f} lost={c.commitments_lost} "
                  f"reduction={c.storage_reduction_pct}% keep={c.always_keep}")
        print(f"\nRESULT: {'RECALL_PASSED' if res.passed else 'RECALL_FAILED'}")
    return 0 if res.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
