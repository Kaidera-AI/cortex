"""E2 — chat distillation (commitment extraction + always-keep floor).

Extracts load-bearing commitments (decisions, facts, outcomes, paths) from a
message turn, allowing conversational chatter to be moved to a cold tier.

Always-keep floor:
  If a message contains specific technical tokens, it is kept whole (no distillation)
  to ensure 100% preservation of critical signals:
  * File paths (/path/to/file)
  * Verify criteria / tests (pytest, vitest, verify:)
  * Role/lane assignments (to_role, to_agent)
  * Tool result JSON/blobs
"""

from __future__ import annotations

import re
from typing import Any, Final

# ---------------------------------------------------------------------------
#  Always-keep patterns (The Recall Floor)
# ---------------------------------------------------------------------------

_PATH_RE = re.compile(r"(?:(?:[A-Za-z0-9_.-]+/+)+[A-Za-z0-9_.-]*[A-Za-z0-9_-])")
_VERIFY_RE = re.compile(r"\b(?:verify|acceptance|test|tests|pytest|vitest|cargo|npm run|git)\b", re.IGNORECASE)
_ROLE_RE = re.compile(r"\b(?:to_role|to_agent|from_role|from_agent)\b", re.IGNORECASE)
_JSON_RE = re.compile(r"\{.*\"[a-z_]+\":.*\}", re.DOTALL) # Basic JSON-like structure detection

def is_always_keep(text: str) -> bool:
    """True if the text contains critical signals that must never be distilled."""
    if not text:
        return False
    if _PATH_RE.search(text):
        return True
    if _VERIFY_RE.search(text):
        return True
    if _ROLE_RE.search(text):
        return True
    if "```" in text: # Code blocks are always kept
        return True
    if _JSON_RE.search(text):
        return True
    return False

# ---------------------------------------------------------------------------
#  Commitment Extraction (Simple symbolic version)
# ---------------------------------------------------------------------------

# Keywords that signal a commitment turn.
_COMMITMENT_KEYWORDS: Final[set[str]] = {
    "decided", "decision", "committed", "shipped", "fixed", "implemented",
    "resolved", "outcome", "result", "path", "assigned", "handoff",
}

def distill_message(text: str) -> list[dict[str, Any]]:
    """Distill a message into one or more commitment/fact rows.

    This is a simplified symbolic version for Phase 3. In a production
    environment, this would be an LLM-backed transform. It splits by
    sentences and keeps those containing commitment keywords or facts.
    """
    if not text:
        return []

    # Split by sentences (naive)
    sentences = re.split(r'(?<=[.!?])\s+', text)
    commitments = []

    for sent in sentences:
        sent_clean = sent.strip()
        if not sent_clean:
            continue

        lower_sent = sent_clean.lower()
        # If it looks like a commitment, extract it.
        if any(kw in lower_sent for kw in _COMMITMENT_KEYWORDS):
            commitments.append({
                "content": sent_clean,
                "metadata": {"commitment_type": "decision"}
            })
        # If it looks like a fact (contains "is", "are", "has" + nouns) - very naive
        elif any(kw in lower_sent for kw in [" is ", " are ", " has ", " was ", " were "]):
            commitments.append({
                "content": sent_clean,
                "metadata": {"commitment_type": "fact"}
            })

    return commitments
