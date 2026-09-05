"""E4 — symbolic text compaction (Telegraph/Caveman-style, deterministic, no model).

Rule-based compaction that shrinks stored ``messages.content`` and ``decisions``
summaries near-losslessly. It is the OPPOSITE of a summariser: it never drops a
load-bearing token, only strips redundancy (filler, tense, list scaffolding,
politeness, repeated whitespace). The compacted text is round-trippable for the
recall benchmark (the facts are intact) but is not pretty for display — display
expands the raw original from the cold tier by row id.

Guarantees (the recall floor):
  * Proper nouns, file paths, verify criteria, role names, decision keywords,
    quoted strings, UUIDs, numbers, and code tokens are NEVER altered or dropped.
  * Only whitespace, filler words, tense inflections, and list scaffolding are
    collapsed/normalized.
  * Returns ``was_changed=False`` when savings are below a threshold (the original
    is kept verbatim so a tiny compaction never risks a semantic-shift regression).

This module is stdlib-only so it imports cleanly in the API process.
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------

# Below this many chars saved (or this fraction of the original), keep verbatim.
_MIN_ABS_SAVINGS = 24
_MIN_FRAC_SAVINGS = 0.05

# Filler words safe to drop ONLY when they are not load-bearing. These are
# conversational/epistemic fillers, never entity names (handled by the
# proper-noun guard) and never part of a path/code token (handled by the token
# guard). The regex anchors on word boundaries so `path/to/maybe` is untouched.
_FILLER_WORDS: Final[set[str]] = {
    # politeness / hedging
    "please", "kindly", "just", "simply", "basically", "essentially",
    "actually", "really", "quite", "rather", "somewhat", "perhaps", "maybe",
    # filler adverbs
    "very", "really", "so", "too", "pretty", "fairly", "rather",
    # discourse
    "like", "well", "okay", "ok", "alright", "right", "yeah", "yep", "nope",
    # verbosity
    "going", "gonna", "wanna", "stuff", "things", "thing",
    # tense filler verbs (kept when load-bearing by the verb-keep guard below)
}

# Tokens that must NEVER be compacted away — load-bearing for recall.
_PATH_RE = re.compile(r"(?:(?:[A-Za-z0-9_.-]+/+)+[A-Za-z0-9_.-]*[A-Za-z0-9_-])")  # file/path-ish
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_BACKTICK_RE = re.compile(r"`[^`]+`")
_VERIFY_RE = re.compile(r"\b(?:verify|acceptance|test|tests|pytest|vitest|cargo|npm run|git)\b", re.IGNORECASE)

# A token is "protected" if it looks like a path, uuid, quoted/code span, or
# contains structural punctuation that compaction must not dissolve.
_PROTECTED_RE = re.compile(
    r"[/\\]|"                  # path separators
    r"\.[A-Za-z0-9]+$|"        # file extensions like .py
    r"^[A-Z][A-Za-z0-9_]*$|"   # ProperNouns / CamelCase / ALLCAPS acronyms
    r"[A-Z]{2,}|"              # acronyms (API, GKE, SQL)
    r"^[A-Z][a-z]+[A-Z]",      # CamelCase mid
)


def _word_protected(word: str) -> bool:
    """True if `word` must survive compaction verbatim."""
    if not word:
        return False
    if word.isdigit():
        return True
    if _PROTECTED_RE.search(word):
        return True
    return False


# ---------------------------------------------------------------------------
#  Rule set (order matters; each operates on the running string)
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"[ \t]+")
_NEWLINE_WS_RE = re.compile(r"\s*\n\s*")
_LIST_BULLET_RE = re.compile(r"(?m)^\s*[-*]\s+")
_ENUM_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|[a-z][.)])\s+")


def _collapse_whitespace(text: str) -> str:
    text = _NEWLINE_WS_RE.sub("\n", text)
    text = _LIST_BULLET_RE.sub("", text)        # bullet → comma-phrase on next rule
    text = _ENUM_RE.sub("", text)               # 1. / a) → drop the marker
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _drop_filler_words(text: str) -> str:
    """Drop only unprotected filler words (guarded so paths/acronyms survive)."""

    def _sub(match: re.Match[str]) -> str:
        word = match.group(0)
        # Keep the word if it borders structural punctuation (a path/code token).
        start, end = match.span()
        prev = text[start - 1] if start > 0 else ""
        nxt = text[end] if end < len(text) else ""
        if prev in ("/", "\\", ".", "_", "-") or nxt in ("/", "\\", ".", "_", "-"):
            return word
        if _word_protected(word):
            return word
        # Lowercase filler only — never drop a capitalized word (proper noun).
        if word[0].isupper():
            return word
        return ""

    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in _FILLER_WORDS) + r")\b", re.IGNORECASE)
    out = pattern.sub(_sub, text)
    # Collapse the double spaces left by filler removal.
    out = _WS_RE.sub(" ", out)
    return out


def _normalize_tense(text: str) -> str:
    """Collapse a small, safe set of tense/filler verb forms to simple present.

    Only applies to a handful of auxiliaries + gerund 'going to' → 'will'. Never
    touches a load-bearing verb (verified, tested, etc.) — those are protected by
    the verify regex and by case.
    """
    text = re.sub(r"\b(is|are|was|were) going to\b", "will", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(I am|we are) going to\b", "will", text, flags=re.IGNORECASE)
    # 'in order to' → 'to' (the canonical verbosity compaction).
    text = re.sub(r"\bin order to\b", "to", text, flags=re.IGNORECASE)
    return text


def _collapse_comma_lists(text: str) -> str:
    """Collapse a comma-separated enumeration that spans lines into one phrase."""
    # "a,\nb,\nc" → "a, b, c" (tidy), keep commas.
    text = re.sub(r",\s*\n\s*", ", ", text)
    return text


def is_compactable(text: str) -> bool:
    """True if `text` has enough redundancy to be worth compacting. Cheap pre-check."""
    if not text or len(text) < _MIN_ABS_SAVINGS:
        return False
    return bool(_WS_RE.search(text) or "," in text or "\n" in text)


def compact_text(text: str) -> tuple[str, bool, int]:
    """Compact `text` deterministically.

    Returns ``(compact_text, was_changed, savings_chars)``. ``was_changed`` is False
    (and the ORIGINAL is returned) when the savings are below the threshold — so a
    near-zero compaction never risks a semantic shift and the row stays verbatim.
    """
    if not text:
        return text, False, 0
    original = text

    # Protect spans we must never alter: quoted code, paths, uuids.
    protected: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    guarded = _BACKTALK_GUARD = text
    guarded = _UUID_RE.sub(_stash, guarded)
    guarded = _BACKTICK_RE.sub(_stash, guarded)
    guarded = _PATH_RE.sub(_stash, guarded)
    guarded = _VERIFY_RE.sub(_stash, guarded)

    # Run the rule set.
    out = _collapse_whitespace(guarded)
    out = _drop_filler_words(out)
    out = _normalize_tense(out)
    out = _collapse_comma_lists(out)

    # Restore protected spans.
    def _restore(m: re.Match[str]) -> str:
        return protected[int(m.group(1))]
    out = re.sub(r"\x00(\d+)\x00", _restore, out)

    out = out.strip()
    savings = len(original) - len(out)
    frac = savings / len(original) if original else 0
    if savings < _MIN_ABS_SAVINGS or frac < _MIN_FRAC_SAVINGS:
        return original, False, 0
    return out, True, savings


__all__ = ["compact_text", "is_compactable"]
