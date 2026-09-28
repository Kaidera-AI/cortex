"""Deterministic chunking of parse blocks into store-ready chunks (F05, R08).

Chunking is a pure projection: no clock, no randomness, no IO. The same
blocks under the same pinned policy always produce byte-identical chunks, so
a re-executed attempt re-derives the same ordinals and digests instead of
duplicating projection rows. Chunk identity (``chunk_key``) is derived by the
caller from the policy identity and ordinal; the text digest is derived here
through :mod:`cortex_v2.processing.keys`.

Two policy kinds are served:

``structural``
    Respects block boundaries. Consecutive blocks are merged greedily until
    the next block would push the joined text past ``target_chars``. A single
    block longer than ``max_chars`` is split at paragraph, then sentence,
    then line boundaries; only an indivisible run is hard-split at exactly
    ``max_chars``, so no chunk ever exceeds the hard cap.

``token_window``
    Approximates tokens as ``ceil(chars / 4)`` and windows the joined block
    stream by ``target_chars`` (the character width that corresponds to the
    policy's token budget under that approximation). Cuts prefer sentence or
    line boundaries in the second half of a window instead of mid-word cuts.

``overlap_chars`` carries trailing text of each chunk into the next one. The
carry is text-only: it never contributes block spans, so in ``structural``
mode every block span still appears in exactly one chunk (split blocks emit
one span per contiguous piece). A ``token_window`` cut inside a block makes
that block contribute to both neighbouring chunks, which is the documented
"chunk spans may overlap" case, never a silent gap.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from . import keys
from .contracts import Chunk, ChunkingPolicy, ParseBlock, Span

#: Chunking strategies a pinned policy may name.
POLICY_KINDS = ("structural", "token_window")

#: Storage-side bound for the pinned policy id.
POLICY_ID_MAX_LENGTH = 128

_POLICY_KEYS = (
    "policy_id",
    "version",
    "kind",
    "target_chars",
    "max_chars",
    "overlap_chars",
)
_COUNT_KEYS = ("target_chars", "max_chars", "overlap_chars")

_SEPARATOR = "\n\n"
_SEPARATOR_LENGTH = len(_SEPARATOR)

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
_LINE_BREAK = re.compile(r"\n")
_SPLIT_LEVELS = (_PARAGRAPH_BREAK, _SENTENCE_BREAK, _LINE_BREAK)


def approx_tokens(text: str) -> int:
    """Canonical token estimate for policy arithmetic: ``ceil(chars / 4)``."""
    return (len(text) + 3) // 4


def sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Sentence-like ``(start, end)`` segments of *text*, in order.

    Breaks fall after ``.``/``!``/``?`` runs and at line boundaries; the
    separating whitespace belongs to neither segment, so every segment is a
    clean verbatim slice. This is a deterministic heuristic, not a grammar —
    an abbreviation like ``e.g.`` splits — which is acceptable because every
    consumer only needs stable, reproducible boundaries.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_BREAK.finditer(text):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return tuple(spans)


def _validate_policy(policy: ChunkingPolicy) -> None:
    """Raise ``ValueError`` naming the offending key of an unusable policy.

    An invalid policy is a configuration bug; chunking with it would silently
    re-key or truncate projections, so it is never a no-op.
    """
    if not isinstance(policy.policy_id, str) or not policy.policy_id:
        raise ValueError("chunking policy key 'policy_id' must be a non-empty string")
    if len(policy.policy_id) > POLICY_ID_MAX_LENGTH:
        raise ValueError(
            "chunking policy key 'policy_id' must be at most "
            f"{POLICY_ID_MAX_LENGTH} characters"
        )
    if (
        isinstance(policy.version, bool)
        or not isinstance(policy.version, int)
        or policy.version < 1
    ):
        raise ValueError(
            "chunking policy key 'version' must be an int >= 1: "
            f"{policy.version!r}"
        )
    if policy.kind not in POLICY_KINDS:
        raise ValueError(
            "chunking policy key 'kind' must be one of "
            f"{', '.join(POLICY_KINDS)}: {policy.kind!r}"
        )
    for name in _COUNT_KEYS:
        value = getattr(policy, name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"chunking policy key '{name}' must be an int: {value!r}")
    if policy.target_chars <= 0:
        raise ValueError(
            "chunking policy key 'target_chars' must be positive: "
            f"{policy.target_chars}"
        )
    if policy.max_chars < policy.target_chars:
        raise ValueError(
            "chunking policy key 'max_chars' must be >= target_chars: "
            f"{policy.max_chars} < {policy.target_chars}"
        )
    if policy.overlap_chars < 0:
        raise ValueError(
            "chunking policy key 'overlap_chars' must not be negative: "
            f"{policy.overlap_chars}"
        )
    if policy.overlap_chars >= policy.target_chars:
        raise ValueError(
            "chunking policy key 'overlap_chars' must be less than target_chars: "
            f"{policy.overlap_chars} >= {policy.target_chars}"
        )


def policy_from_json(value: dict[str, Any]) -> ChunkingPolicy:
    """Parse and validate a chunking policy from its JSON representation.

    Every rejection is a ``ValueError`` naming the offending key, including
    the cross-field rules ``overlap_chars < target_chars`` and
    ``max_chars >= target_chars``.
    """
    if not isinstance(value, dict):
        raise ValueError("chunking policy must be a JSON object")
    unknown = sorted(set(value) - set(_POLICY_KEYS))
    if unknown:
        raise ValueError(f"unknown chunking policy key: {unknown[0]}")
    for key in _POLICY_KEYS:
        if key not in value:
            raise ValueError(f"missing chunking policy key: {key}")
    policy_id = value["policy_id"]
    if not isinstance(policy_id, str):
        raise ValueError(
            f"chunking policy key 'policy_id' must be a string: {policy_id!r}"
        )
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"chunking policy key 'version' must be an int: {version!r}")
    kind = value["kind"]
    if not isinstance(kind, str):
        raise ValueError(f"chunking policy key 'kind' must be a string: {kind!r}")
    for name in _COUNT_KEYS:
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"chunking policy key '{name}' must be an int: {item!r}")
    policy = ChunkingPolicy(
        policy_id=policy_id,
        version=version,
        kind=kind,
        target_chars=value["target_chars"],
        max_chars=value["max_chars"],
        overlap_chars=value["overlap_chars"],
    )
    _validate_policy(policy)
    return policy


@dataclass(frozen=True, slots=True)
class _Unit:
    """One indivisible piece of block text entering chunk assembly."""

    text: str
    span: Span
    block_kind: str
    heading_path: tuple[str, ...]

    @classmethod
    def of(cls, block: ParseBlock) -> _Unit:
        return cls(block.text, block.span, block.block_kind, block.heading_path)


def _bounded_segments(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    """Contiguous segments between *pattern* matches.

    The separator stays with the preceding segment, so segments tile *text*
    exactly and every slice offset remains a verbatim provenance offset.
    """
    segments: list[tuple[int, int]] = []
    cursor = 0
    for match in pattern.finditer(text):
        segments.append((cursor, match.end()))
        cursor = match.end()
    if cursor < len(text):
        segments.append((cursor, len(text)))
    return segments


def _cascade(text: str, budget: int, level: int, base: int) -> list[tuple[int, str]]:
    """``(offset, piece)`` slices of *text*, each at most *budget* characters.

    Boundary levels are tried in order — paragraph, sentence, line — and only
    an indivisible run longer than *budget* is hard-split at exactly the cap.
    Pieces tile *text*; offsets are relative to *base* in the caller's text.
    """
    if len(text) <= budget:
        return [(base, text)]
    if level >= len(_SPLIT_LEVELS):
        # documented last resort: a single unsplittable run is cut at the cap
        return [
            (base + offset, text[offset : offset + budget])
            for offset in range(0, len(text), budget)
        ]
    segments = _bounded_segments(text, _SPLIT_LEVELS[level])
    if len(segments) <= 1:
        return _cascade(text, budget, level + 1, base)
    pieces: list[tuple[int, str]] = []
    pack_start: int | None = None
    pack_end = 0
    for start, end in segments:
        if end - start > budget:
            if pack_start is not None:
                pieces.append((base + pack_start, text[pack_start:pack_end]))
                pack_start = None
            pieces.extend(_cascade(text[start:end], budget, level + 1, base + start))
            continue
        if pack_start is None:
            pack_start, pack_end = start, end
        elif end - pack_start <= budget:
            pack_end = end
        else:
            pieces.append((base + pack_start, text[pack_start:pack_end]))
            pack_start, pack_end = start, end
    if pack_start is not None:
        pieces.append((base + pack_start, text[pack_start:pack_end]))
    return pieces


def _piece_span(block: ParseBlock, offset: int, piece: str, lines_before: int) -> Span:
    """Exact sub-span of one piece of a split block.

    Offsets stay verbatim because pieces are contiguous slices of the block
    text; line numbers are recomputed from the block's own ``line_start`` and
    the newlines preceding the piece; binary locators are carried over with a
    detached ``detail`` copy so pieces never alias one dict.
    """
    span = block.span
    start = span.start + offset
    line_start = None if span.line_start is None else span.line_start + lines_before
    line_end = None if line_start is None else line_start + piece.count("\n")
    return replace(
        span,
        start=start,
        end=start + len(piece),
        line_start=line_start,
        line_end=line_end,
        detail=dict(span.detail),
    )


def _units(blocks: Sequence[ParseBlock], max_chars: int) -> list[_Unit]:
    """Assembly units: whole blocks, or contiguous pieces of oversized ones."""
    units: list[_Unit] = []
    for block in blocks:
        if not block.text:
            # a parser that found nothing reports INPUT_EMPTY, not empty
            # blocks; dropping them keeps chunk text free of bare separators
            continue
        if len(block.text) <= max_chars:
            units.append(_Unit.of(block))
            continue
        lines_before = 0
        for offset, piece in _cascade(block.text, max_chars, 0, 0):
            units.append(
                _Unit(
                    piece,
                    _piece_span(block, offset, piece, lines_before),
                    block.block_kind,
                    block.heading_path,
                )
            )
            lines_before += piece.count("\n")
    return units


def _common_heading(paths: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
    """Deepest heading path shared by every contributing block."""
    common = list(paths[0])
    for path in paths[1:]:
        limit = min(len(common), len(path))
        index = 0
        while index < limit and common[index] == path[index]:
            index += 1
        del common[index:]
        if not common:
            break
    return tuple(common)


def _make_chunk(ordinal: int, text: str, units: Sequence[_Unit]) -> Chunk:
    spans = tuple(unit.span for unit in units)
    if len(spans) == 1:
        span = spans[0]
    else:
        line_start = spans[0].line_start
        line_end = spans[-1].line_end
        if any(item.line_start is None for item in spans):
            line_start = None
        if any(item.line_end is None for item in spans):
            line_end = None
        span = Span(
            start=min(item.start for item in spans),
            end=max(item.end for item in spans),
            line_start=line_start,
            line_end=line_end,
        )
    return Chunk(
        ordinal=ordinal,
        text=text,
        span=span,
        block_spans=spans,
        block_kinds=tuple(unit.block_kind for unit in units),
        text_sha256=keys.text_sha256(text),
        heading_path=_common_heading([unit.heading_path for unit in units]),
    )


def _structural(units: Sequence[_Unit], policy: ChunkingPolicy) -> tuple[Chunk, ...]:
    """Merge whole units up to ``target_chars``, carrying overlap text."""
    chunks: list[Chunk] = []
    carry = ""
    index = 0
    while index < len(units):
        first = units[index]
        index += 1
        parts = [first.text]
        selected = [first]
        if carry:
            # keep the carried tail inside the target even when the first new
            # unit is large; a unit at max_chars simply travels without overlap
            room = policy.target_chars - len(first.text) - _SEPARATOR_LENGTH
            if room > 0:
                parts.insert(0, carry[-room:] if len(carry) > room else carry)
        length = sum(len(part) for part in parts)
        length += _SEPARATOR_LENGTH * (len(parts) - 1)
        while index < len(units):
            extra = _SEPARATOR_LENGTH + len(units[index].text)
            if length + extra > policy.target_chars:
                break
            parts.append(units[index].text)
            selected.append(units[index])
            length += extra
            index += 1
        text = _SEPARATOR.join(parts)
        chunks.append(_make_chunk(len(chunks), text, selected))
        carry = text[-policy.overlap_chars :] if policy.overlap_chars else ""
    return tuple(chunks)


def _preferred_cut(text: str, start: int, end: int) -> int:
    """Largest sentence/line boundary in the second half of the window.

    Cutting on a boundary keeps windows readable; the halfway floor stops a
    window from collapsing to a stub. Without a candidate the window ends at
    *end*, which ``max_chars`` still bounds.
    """
    floor = start + (end - start) // 2
    cut = end
    for match in _SENTENCE_BREAK.finditer(text, start + 1, end):
        if match.start() >= floor:
            cut = match.start()
    return cut


def _preceding(extents: Sequence[tuple[int, int]], offset: int) -> int:
    best = 0
    for position, (_, end) in enumerate(extents):
        if end <= offset:
            best = position
    return best


def _token_window(units: Sequence[_Unit], policy: ChunkingPolicy) -> tuple[Chunk, ...]:
    """Window the joined block stream by ``target_chars`` with overlap."""
    pieces: list[str] = []
    extents: list[tuple[int, int]] = []
    cursor = 0
    for position, unit in enumerate(units):
        if position:
            pieces.append(_SEPARATOR)
            cursor += _SEPARATOR_LENGTH
        pieces.append(unit.text)
        extents.append((cursor, cursor + len(unit.text)))
        cursor += len(unit.text)
    stream = "".join(pieces)

    chunks: list[Chunk] = []
    start = 0
    total = len(stream)
    while start < total:
        end = min(start + policy.target_chars, total)
        cut = _preferred_cut(stream, start, end) if end < total else end
        chosen = [
            position
            for position, (block_start, block_end) in enumerate(extents)
            if block_start < cut and block_end > start
        ]
        if not chosen:
            # a degenerate window inside a separator still needs provenance:
            # attribute it to the nearest preceding block
            chosen = [_preceding(extents, start)]
        selected = [units[position] for position in chosen]
        chunks.append(_make_chunk(len(chunks), stream[start:cut], selected))
        if cut >= total:
            break
        start = max(cut - policy.overlap_chars, start + 1)
    return tuple(chunks)


def chunk_blocks(
    blocks: Sequence[ParseBlock], policy: ChunkingPolicy
) -> tuple[Chunk, ...]:
    """Project parse blocks into ordered chunks under a pinned policy.

    Pure and deterministic: no clock, no randomness, no IO. An invalid policy
    raises ``ValueError`` naming the offending key instead of silently
    no-op'ing; empty input honestly yields an empty tuple. Chunk ordinals are
    0-based and contiguous, every chunk's ``span`` covers the union bounds of
    its ``block_spans``, and ``text_sha256`` is the digest of ``text``.
    """
    _validate_policy(policy)
    units = _units(blocks, policy.max_chars)
    if not units:
        return ()
    if policy.kind == "structural":
        return _structural(units, policy)
    return _token_window(units, policy)


__all__ = [
    "POLICY_ID_MAX_LENGTH",
    "POLICY_KINDS",
    "approx_tokens",
    "chunk_blocks",
    "policy_from_json",
    "sentence_spans",
]
