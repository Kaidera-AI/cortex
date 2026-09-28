"""Optional, versioned derived-text transforms (R18).

Distillation and compaction are *derived* outputs pinned to a transform
identity (``name@version``); the originals they are built from are never
mutated and stay retrievable byte-for-byte. Both transforms are disabled by
default: a spec without an explicit ``enabled: true`` is a typed
``CONFIGURATION_MISSING`` refusal, never a silent summary.

A lossy summary is never published. Every candidate output passes a
commitment-preservation gate: obligation-bearing sentences (RFC 2119
MUST/SHALL/REQUIRED/SHOULD/NEVER plus owner, deadline, decision, commit,
will-not and rollback markers) are extracted from the source, normalised and
compared against the candidate. When anything would be lost, the transform
refuses with ``OUTPUT_INVALID`` and the exact ``lost_commitments`` instead of
publishing.

Selection is deterministic and extractive: sentences are scored by heading
depth, obligation markers, document position and length; the top-scoring ones
are kept verbatim within the ratio budget and re-emitted in their original
order. Nothing is paraphrased and no new wording is ever generated.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .chunking import sentence_spans
from .contracts import Outcome, ParseBlock

#: Transform kinds a pinned spec may name; they match the durable job kinds
#: ``transform.distill`` / ``transform.compact``.
TRANSFORM_KINDS = ("distill", "compact")

#: Fail-closed default ratio when a spec pins none.
DEFAULT_DISTILL_RATIO = 0.5

#: Bounds for spec params so a derived row stays reviewable.
PARAMS_MAX_ITEMS = 64
PARAM_KEY_MAX_LENGTH = 128

_SPEC_KEYS = ("kind", "name", "version", "enabled", "params")
_REQUIRED_SPEC_KEYS = ("kind", "name", "version")

#: Obligation markers: RFC 2119 keywords plus the owner/deadline/decision/
#: commit/will-not/rollback markers an obligation sentence typically carries.
#: Matched case-insensitively at word boundaries.
_OBLIGATION = re.compile(
    r"\b(?:"
    r"must|shall|should|required|never"
    r"|owner|deadline|decision|decid(?:e|ed|es|ing)"
    r"|commit(?:s|ted|ting)?|rollback|roll\s+back|will\s+not|won't"
    r")\b",
    re.IGNORECASE,
)

#: Extractive scoring weights. Obligation dominates every other signal by an
#: order of magnitude, so an enabled transform never trades a commitment away
#: for filler; ties break on original position, keeping selection total.
_OBLIGATION_SCORE = 100.0
_HEADING_SCORE = 8.0
_LEAD_SCORE = 6.0
_POSITION_SCORE = 4.0
_SHORT_PENALTY = -6.0
_LONG_PENALTY = -2.0
_MIN_USEFUL_CHARS = 24
_MAX_USEFUL_CHARS = 480


@dataclass(frozen=True, slots=True)
class TransformSpec:
    """Pinned identity of one optional derived transform.

    ``enabled`` defaults to False: a transform that nobody explicitly turned
    on stays off, and ``params`` is normalised through JSON at construction so
    a pinned identity can never carry NaN infinities or caller-side aliasing.
    """

    kind: str
    name: str
    version: int
    enabled: bool = False
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def __post_init__(self) -> None:
        if self.kind not in TRANSFORM_KINDS:
            raise ValueError(
                "transform kind must be one of "
                f"{', '.join(TRANSFORM_KINDS)}: {self.kind!r}"
            )
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("transform spec key 'name' must be a non-empty string")
        if "\x00" in self.name:
            raise ValueError(
                "transform spec key 'name' must not contain a NUL character"
            )
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError(
                f"transform spec key 'version' must be an int >= 1: {self.version!r}"
            )
        if not isinstance(self.enabled, bool):
            raise ValueError(
                f"transform spec key 'enabled' must be a bool: {self.enabled!r}"
            )
        object.__setattr__(self, "params", _validate_params(self.params))


def _validate_params(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError(f"transform spec key 'params' must be a dict: {params!r}")
    if len(params) > PARAMS_MAX_ITEMS:
        raise ValueError(
            f"transform spec key 'params' must hold at most {PARAMS_MAX_ITEMS} items"
        )
    for key, value in params.items():
        if not isinstance(key, str) or not key or len(key) > PARAM_KEY_MAX_LENGTH:
            raise ValueError(
                "transform spec key 'params' keys must be non-empty strings of at "
                f"most {PARAM_KEY_MAX_LENGTH} characters"
            )
        if isinstance(value, bool) or isinstance(value, str):
            continue
        if isinstance(value, int | float):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(
                    f"transform spec key 'params' values must be finite: {key}"
                )
            continue
        raise ValueError(
            f"transform spec key 'params' values must be JSON scalars: {key}"
        )
    normalised: dict[str, Any] = json.loads(json.dumps(params, sort_keys=True))
    return normalised


def spec_from_json(value: dict[str, Any]) -> TransformSpec:
    """Parse and validate one transform spec from its JSON representation.

    ``enabled`` defaults to False (fail-closed) and ``params`` to an empty
    mapping; every other rejection is a ``ValueError`` naming the offending
    key.
    """
    if not isinstance(value, dict):
        raise ValueError("transform spec must be a JSON object")
    unknown = sorted(set(value) - set(_SPEC_KEYS))
    if unknown:
        raise ValueError(f"unknown transform spec key: {unknown[0]}")
    for key in _REQUIRED_SPEC_KEYS:
        if key not in value:
            raise ValueError(f"missing transform spec key: {key}")
    kind = value["kind"]
    if not isinstance(kind, str) or kind not in TRANSFORM_KINDS:
        raise ValueError(
            "transform spec key 'kind' must be one of "
            f"{', '.join(TRANSFORM_KINDS)}: {kind!r}"
        )
    name = value["name"]
    if not isinstance(name, str) or not name:
        raise ValueError(
            f"transform spec key 'name' must be a non-empty string: {name!r}"
        )
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(
            f"transform spec key 'version' must be an int >= 1: {version!r}"
        )
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"transform spec key 'enabled' must be a bool: {enabled!r}"
        )
    return TransformSpec(
        kind=kind,
        name=name,
        version=version,
        enabled=enabled,
        params=_validate_params(value.get("params", {})),
    )


@dataclass(frozen=True, slots=True)
class DistillationResult:
    """Typed outcome of one derived transform.

    ``commitments`` is the source's obligation baseline; ``lost_commitments``
    is the part of it the candidate output failed to preserve (non-empty only
    when the gate refused). ``coverage`` and ``stats`` are always populated so
    a refusal is as observable as a success. On refusal ``output_text`` is
    empty — the rejected candidate is discarded, never handed back — while
    ``coverage["kept_sentences"]`` still reports what the selection held
    before the gate rejected it.
    """

    outcome: Outcome
    output_text: str = ""
    output_payload: dict[str, Any] | None = None
    commitments: tuple[str, ...] = ()
    lost_commitments: tuple[str, ...] = ()
    coverage: dict[str, int] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


def commitments(text: str) -> tuple[str, ...]:
    """Obligation-bearing sentences of *text*: normalised, deduped, in order.

    Normalisation collapses whitespace so punctuation and line-wrapping
    differences cannot smuggle a lost obligation past the gate; order follows
    the source, which makes ``lost_commitments`` a precise, stable citation.
    """
    found: dict[str, None] = {}
    for start, end in sentence_spans(text):
        normalised = " ".join(text[start:end].split())
        if not normalised or normalised in found:
            continue
        if _OBLIGATION.search(normalised):
            found[normalised] = None
    return tuple(found)


def _coverage(
    source_text: str, kept_sentences: int, output_chars: int
) -> dict[str, int]:
    return {
        "source_sentences": len(sentence_spans(source_text)),
        "kept_sentences": kept_sentences,
        "source_chars": len(source_text),
        "output_chars": output_chars,
    }


def _disabled(source_text: str) -> DistillationResult:
    baseline = commitments(source_text)
    return DistillationResult(
        outcome=Outcome.CONFIGURATION_MISSING,
        commitments=baseline,
        coverage=_coverage(source_text, 0, 0),
        stats={"commitments": len(baseline), "kept_commitments": 0},
        warnings=("transform_disabled",),
    )


def _blank(source_text: str) -> DistillationResult:
    return DistillationResult(
        outcome=Outcome.INPUT_EMPTY,
        coverage=_coverage(source_text, 0, 0),
        stats={"commitments": 0, "kept_commitments": 0},
    )


def _finish(
    source_text: str,
    output_text: str,
    *,
    payload: dict[str, Any],
    kept_sentences: int,
) -> DistillationResult:
    """Run the commitment-preservation gate over one candidate output."""
    baseline = commitments(source_text)
    preserved = set(commitments(output_text))
    lost = tuple(item for item in baseline if item not in preserved)
    stats = {
        "commitments": len(baseline),
        "kept_commitments": len(baseline) - len(lost),
    }
    if lost:
        # refuse: a lossy summary is never published
        return DistillationResult(
            outcome=Outcome.OUTPUT_INVALID,
            commitments=baseline,
            lost_commitments=lost,
            coverage=_coverage(source_text, kept_sentences, 0),
            stats=stats,
            warnings=("commitments_lost",),
        )
    if not output_text.strip():
        # an empty "summary" of a non-empty source is never a success either
        return DistillationResult(
            outcome=Outcome.OUTPUT_INVALID,
            commitments=baseline,
            coverage=_coverage(source_text, kept_sentences, 0),
            stats=stats,
            warnings=("empty_output",),
        )
    return DistillationResult(
        outcome=Outcome.OK,
        output_text=output_text,
        output_payload=payload,
        commitments=baseline,
        coverage=_coverage(source_text, kept_sentences, len(output_text)),
        stats=stats,
    )


def _effective_ratio(spec: TransformSpec, ratio: float | None) -> float:
    value: Any = (
        spec.params.get("ratio", DEFAULT_DISTILL_RATIO) if ratio is None else ratio
    )
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0.0 < float(value) <= 1.0
    ):
        raise ValueError(f"ratio must be a number in (0, 1]: {value!r}")
    return float(value)


def _heading_depth(offset: int, blocks: Sequence[ParseBlock]) -> int:
    """Deepest heading path among the blocks whose span contains *offset*."""
    depth = 0
    for block in blocks:
        if block.span.start <= offset < block.span.end:
            depth = max(depth, len(block.heading_path))
    return depth


def _score(
    index: int,
    sentence: str,
    total: int,
    spans: Sequence[tuple[int, int]],
    blocks: Sequence[ParseBlock],
) -> float:
    score = 0.0
    if _OBLIGATION.search(sentence):
        score += _OBLIGATION_SCORE
    score += _HEADING_SCORE * _heading_depth(spans[index][0], blocks)
    if index == 0:
        score += _LEAD_SCORE
    score += (
        _POSITION_SCORE * (1.0 - index / (total - 1)) if total > 1 else _POSITION_SCORE
    )
    length = len(sentence.strip())
    if length < _MIN_USEFUL_CHARS:
        score += _SHORT_PENALTY
    elif length > _MAX_USEFUL_CHARS:
        score += _LONG_PENALTY
    return score


def distill(
    source_text: str,
    blocks: Sequence[ParseBlock],
    spec: TransformSpec,
    *,
    ratio: float | None = None,
) -> DistillationResult:
    """Deterministic extractive distillation of *source_text*.

    Sentences are scored by heading depth (from the *blocks* whose spans index
    *source_text*), obligation markers, document position and length; the
    top-scoring ones are kept verbatim while their total length fits inside
    ``ratio`` of the source size, and the output re-emits them in original
    order. Blocks whose spans do not index *source_text* (binary formats carry
    their own extracted stream) simply contribute no heading depth — scoring
    degrades, it never guesses. The ratio comes from the *ratio* argument, or
    from ``spec.params["ratio"]``, or defaults to
    :data:`DEFAULT_DISTILL_RATIO`.
    """
    if spec.kind != "distill":
        raise ValueError(
            f"distill requires a transform spec of kind 'distill': {spec.kind!r}"
        )
    if not spec.enabled:
        return _disabled(source_text)
    if not source_text.strip():
        return _blank(source_text)
    effective = _effective_ratio(spec, ratio)
    spans = sentence_spans(source_text)
    sentences = [source_text[start:end] for start, end in spans]
    total = len(sentences)
    budget = int(len(source_text) * effective)
    ranked = sorted(
        range(total),
        key=lambda index: (
            -_score(index, sentences[index], total, spans, blocks),
            index,
        ),
    )
    kept: list[int] = []
    used = 0
    for index in ranked:
        length = len(sentences[index])
        if used + length <= budget:
            kept.append(index)
            used += length
    kept.sort()
    output_text = "\n".join(sentences[index] for index in kept)
    payload = {
        "spec_identity": spec.identity,
        "kind": spec.kind,
        "version": spec.version,
        "ratio": effective,
        "sentences_in": total,
        "sentences_out": len(kept),
    }
    return _finish(source_text, output_text, payload=payload, kept_sentences=len(kept))


def compact(
    source_text: str,
    blocks: Sequence[ParseBlock],
    spec: TransformSpec,
) -> DistillationResult:
    """Merge near-duplicate blocks, preserving first-occurrence order.

    A block whose normalised text (whitespace-collapsed, case-folded) repeats
    an earlier block is dropped; kept block texts are emitted verbatim. The
    commitment gate still applies: source content that lives outside the
    blocks (for example a heading no block covers) and carries an obligation
    refuses the transform instead of publishing a lossy merge.
    """
    if spec.kind != "compact":
        raise ValueError(
            f"compact requires a transform spec of kind 'compact': {spec.kind!r}"
        )
    if not spec.enabled:
        return _disabled(source_text)
    if not source_text.strip():
        return _blank(source_text)
    if not blocks:
        baseline = commitments(source_text)
        return DistillationResult(
            outcome=Outcome.OUTPUT_INVALID,
            commitments=baseline,
            coverage=_coverage(source_text, 0, 0),
            stats={"commitments": len(baseline), "kept_commitments": 0},
            warnings=("no_blocks",),
        )
    seen: set[str] = set()
    kept_blocks: list[ParseBlock] = []
    duplicates = 0
    for block in blocks:
        key = " ".join(block.text.split()).casefold()
        if not key:
            continue
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        kept_blocks.append(block)
    output_text = "\n\n".join(block.text for block in kept_blocks)
    payload = {
        "spec_identity": spec.identity,
        "kind": spec.kind,
        "version": spec.version,
        "blocks_in": len(blocks),
        "blocks_out": len(kept_blocks),
        "duplicates": duplicates,
    }
    return _finish(
        source_text,
        output_text,
        payload=payload,
        kept_sentences=len(sentence_spans(output_text)),
    )


__all__ = [
    "DEFAULT_DISTILL_RATIO",
    "PARAMS_MAX_ITEMS",
    "PARAM_KEY_MAX_LENGTH",
    "TRANSFORM_KINDS",
    "DistillationResult",
    "TransformSpec",
    "commitments",
    "compact",
    "distill",
    "spec_from_json",
]
