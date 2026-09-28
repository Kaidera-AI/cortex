from __future__ import annotations

import json

import pytest

from cortex_v2.processing.contracts import Outcome, ParseBlock, Span
from cortex_v2.processing.distillation import (
    DEFAULT_DISTILL_RATIO,
    TRANSFORM_KINDS,
    TransformSpec,
    commitments,
    compact,
    distill,
    spec_from_json,
)

S1 = "The service MUST reject expired tokens."
S2 = "Tokens expire after twelve hours."
S3 = "The owner of rotation is the platform team."
S4 = "Logs are retained for thirty days."
SOURCE = "\n".join([S1, S2, S3, S4])
# hand-computed: 39 + 33 + 43 + 34 + 3 separators
assert len(SOURCE) == 152


def distill_spec(**overrides: object) -> TransformSpec:
    values: dict[str, object] = {
        "kind": "distill",
        "name": "test-distill",
        "version": 1,
        "enabled": True,
        "params": {},
    }
    values.update(overrides)
    return TransformSpec(**values)  # type: ignore[arg-type]


def compact_spec(**overrides: object) -> TransformSpec:
    values: dict[str, object] = {
        "kind": "compact",
        "name": "test-compact",
        "version": 1,
        "enabled": True,
        "params": {},
    }
    values.update(overrides)
    return TransformSpec(**values)  # type: ignore[arg-type]


def block(text: str, start: int, heading: tuple[str, ...] = ()) -> ParseBlock:
    return ParseBlock(
        block_kind="paragraph",
        text=text,
        span=Span(start=start, end=start + len(text)),
        heading_path=heading,
    )


# ---------------------------------------------------------------------------
# TransformSpec surface


def test_transform_kinds_exposed() -> None:
    assert set(TRANSFORM_KINDS) == {"distill", "compact"}
    assert DEFAULT_DISTILL_RATIO == 0.5


def test_identity_is_name_at_version() -> None:
    assert distill_spec(name="e2-distill", version=3).identity == "e2-distill@3"


def test_spec_from_json_valid_and_defaults() -> None:
    parsed = spec_from_json(
        {
            "kind": "distill",
            "name": "e2-distill",
            "version": 3,
            "enabled": True,
            "params": {"ratio": 0.5},
        }
    )
    assert parsed == TransformSpec("distill", "e2-distill", 3, True, {"ratio": 0.5})

    minimal = spec_from_json({"kind": "compact", "name": "c", "version": 1})
    assert minimal.enabled is False
    assert minimal.params == {}

    # params values are JSON scalars: a string is accepted at parse time and
    # only rejected where it is consumed (see test_distill_validates_ratio)
    noted = spec_from_json(
        {"kind": "distill", "name": "n", "version": 1, "params": {"ratio": "0.5"}}
    )
    assert noted.params == {"ratio": "0.5"}


def test_spec_from_json_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="bogus"):
        spec_from_json({"kind": "distill", "name": "n", "version": 1, "bogus": 2})


@pytest.mark.parametrize("missing", ["kind", "name", "version"])
def test_spec_from_json_names_missing_key(missing: str) -> None:
    value: dict[str, object] = {"kind": "distill", "name": "n", "version": 1}
    del value[missing]
    with pytest.raises(ValueError, match=missing):
        spec_from_json(value)


@pytest.mark.parametrize(
    ("field", "bad", "named"),
    [
        ("enabled", "yes", "enabled"),
        ("enabled", 1, "enabled"),
        ("kind", "summarize", "kind"),
        ("name", "", "name"),
        ("name", 5, "name"),
        ("version", 0, "version"),
        ("version", "1", "version"),
        ("version", True, "version"),
        ("params", [], "params"),
        ("params", {"flag": None}, "params"),
        ("params", {"x": float("nan")}, "params"),
        ("params", {"a": [1]}, "params"),
        ("params", {f"k{i}": i for i in range(65)}, "params"),
    ],
)
def test_spec_from_json_names_bad_value(field: str, bad: object, named: str) -> None:
    value: dict[str, object] = {"kind": "distill", "name": "n", "version": 1}
    value[field] = bad
    with pytest.raises(ValueError, match=named):
        spec_from_json(value)


def test_transform_spec_guards_construction() -> None:
    with pytest.raises(ValueError, match="kind"):
        TransformSpec("nope", "n", 1, True, {})
    with pytest.raises(ValueError, match="enabled"):
        TransformSpec("distill", "n", 1, "true", {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="version"):
        TransformSpec("distill", "n", 0, True, {})
    with pytest.raises(ValueError, match="name"):
        TransformSpec("distill", "", 1, True, {})
    with pytest.raises(ValueError, match="params"):
        TransformSpec("distill", "n", 1, True, {"a": {}})


def test_transform_spec_copies_params_defensively() -> None:
    params = {"ratio": 0.5}
    spec = TransformSpec("distill", "n", 1, True, params)
    params["ratio"] = 0.9
    assert spec.params == {"ratio": 0.5}


# ---------------------------------------------------------------------------
# commitments extraction


@pytest.mark.parametrize(
    "sentence",
    [
        "The payload MUST NOT exceed the limit.",
        "The worker SHALL retry once.",
        "This field is REQUIRED.",
        "Callers SHOULD pin a revision.",
        "The parser will NEVER guess.",
        "The owner of this service is platform.",
        "The deadline for the migration is Friday.",
        "The decision was recorded yesterday.",
        "We decided to ship on Tuesday.",
        "The team will commit to the date.",
        "They committed to the roadmap.",
        "There will not be another window.",
        "The service won't accept expired keys.",
        "The rollback plan is documented.",
        "Operators must roll back on failure.",
    ],
)
def test_obligation_sentences_are_commitments(sentence: str) -> None:
    assert commitments(sentence) == (" ".join(sentence.split()),)


@pytest.mark.parametrize(
    "sentence",
    [
        "The report covers three systems.",
        "Chunks are embedded in order.",
        "Parsing produced twelve blocks.",
    ],
)
def test_plain_sentences_are_not_commitments(sentence: str) -> None:
    assert commitments(sentence) == ()


def test_commitments_normalise_dedupe_and_keep_order() -> None:
    assert commitments("") == ()
    assert commitments("The   service MUST   go.") == ("The service MUST go.",)
    assert commitments("MUST ship it.\nMUST ship it.") == ("MUST ship it.",)
    text = (
        "The deadline is Friday.\nNothing binding here.\n"
        "The owner is Kai.\nYou MUST sign it."
    )
    assert commitments(text) == (
        "The deadline is Friday.",
        "The owner is Kai.",
        "You MUST sign it.",
    )
    assert commitments(SOURCE) == (S1, S3)


# ---------------------------------------------------------------------------
# distill


def test_disabled_by_default_refuses_with_typed_outcome() -> None:
    spec = spec_from_json({"kind": "distill", "name": "e2-distill", "version": 1})
    assert spec.enabled is False
    result = distill(SOURCE, (), spec)

    assert result.outcome is Outcome.CONFIGURATION_MISSING
    assert result.warnings == ("transform_disabled",)
    assert result.output_text == ""
    assert result.output_payload is None
    assert result.lost_commitments == ()
    assert result.commitments == (S1, S3)
    assert result.coverage == {
        "source_sentences": 4,
        "kept_sentences": 0,
        "source_chars": 152,
        "output_chars": 0,
    }
    assert result.stats == {"commitments": 2, "kept_commitments": 0}

    compact_result = compact(
        SOURCE, (), spec_from_json({"kind": "compact", "name": "c", "version": 1})
    )
    assert compact_result.outcome is Outcome.CONFIGURATION_MISSING
    assert compact_result.warnings == ("transform_disabled",)


def test_blank_input_is_input_empty() -> None:
    for text in ("", "   ", "\n\n  \n"):
        result = distill(text, (), distill_spec())
        assert result.outcome is Outcome.INPUT_EMPTY
        assert result.output_text == ""
        assert result.lost_commitments == ()
        assert compact(text, (), compact_spec()).outcome is Outcome.INPUT_EMPTY


def test_distill_keeps_every_commitment_verbatim_in_order() -> None:
    result = distill(SOURCE, (), distill_spec(), ratio=0.78)

    assert result.outcome is Outcome.OK
    # top-scoring sentences kept verbatim, re-emitted in original order
    assert result.output_text == S1 + "\n" + S2 + "\n" + S3
    assert S1 in result.output_text and S3 in result.output_text
    assert result.output_text.index(S1) < result.output_text.index(S3)
    assert result.commitments == (S1, S3)
    assert result.lost_commitments == ()
    assert set(commitments(result.output_text)) == set(result.commitments)
    # hand-computed coverage and stats
    assert result.coverage == {
        "source_sentences": 4,
        "kept_sentences": 3,
        "source_chars": 152,
        "output_chars": 117,
    }
    assert result.stats == {"commitments": 2, "kept_commitments": 2}
    assert result.output_payload is not None
    assert result.output_payload["spec_identity"] == "test-distill@1"
    assert result.output_payload["kind"] == "distill"
    assert result.output_payload["version"] == 1
    json.dumps(result.output_payload)


def test_distill_ratio_one_is_the_identity() -> None:
    result = distill(SOURCE, (), distill_spec(), ratio=1.0)
    assert result.outcome is Outcome.OK
    assert result.output_text == SOURCE
    assert result.coverage["kept_sentences"] == 4
    assert result.coverage["output_chars"] == 152


def test_distill_reads_ratio_from_spec_params() -> None:
    from_params = distill(SOURCE, (), distill_spec(params={"ratio": 0.78}))
    from_argument = distill(SOURCE, (), distill_spec(), ratio=0.78)
    assert from_params == from_argument


def test_distill_refuses_when_ratio_drops_a_commitment() -> None:
    # budget int(152 * 0.28) = 42 keeps S1 (39) but drops S3 (43)
    result = distill(SOURCE, (), distill_spec(), ratio=0.28)

    assert result.outcome is Outcome.OUTPUT_INVALID
    assert result.output_text == ""
    assert result.output_payload is None
    assert result.lost_commitments == (S3,)
    assert result.commitments == (S1, S3)
    assert result.warnings == ("commitments_lost",)
    assert result.coverage == {
        "source_sentences": 4,
        "kept_sentences": 1,
        "source_chars": 152,
        "output_chars": 0,
    }
    assert result.stats == {"commitments": 2, "kept_commitments": 1}


def test_distill_refuses_when_ratio_keeps_nothing() -> None:
    # budget int(152 * 0.05) = 7 fits no sentence at all
    result = distill(SOURCE, (), distill_spec(), ratio=0.05)

    assert result.outcome is Outcome.OUTPUT_INVALID
    assert result.output_text == ""
    assert result.lost_commitments == (S1, S3)
    assert result.warnings == ("commitments_lost",)
    assert result.coverage["kept_sentences"] == 0
    assert result.coverage["output_chars"] == 0
    assert result.stats == {"commitments": 2, "kept_commitments": 0}


def test_distill_prefers_deeper_headings() -> None:
    deep = "Deep section text explains the design."
    shallow = "Shallow trailing note about nothing here."
    source = deep + "\n" + shallow
    blocks = (
        block(deep, 0, heading=("Design", "Deep", "Deeper")),
        block(shallow, len(deep) + 1),
    )
    result = distill(source, blocks, distill_spec(), ratio=0.5)

    assert result.outcome is Outcome.OK
    assert result.output_text == deep
    assert result.coverage == {
        "source_sentences": 2,
        "kept_sentences": 1,
        "source_chars": 80,
        "output_chars": 38,
    }


def test_distill_validates_ratio() -> None:
    with pytest.raises(ValueError, match="ratio"):
        distill(SOURCE, (), distill_spec(), ratio=0)
    with pytest.raises(ValueError, match="ratio"):
        distill(SOURCE, (), distill_spec(), ratio=1.5)
    with pytest.raises(ValueError, match="ratio"):
        distill(SOURCE, (), distill_spec(params={"ratio": "fast"}))


def test_kind_mismatch_is_a_configuration_bug() -> None:
    with pytest.raises(ValueError, match="kind"):
        distill(SOURCE, (), compact_spec())
    with pytest.raises(ValueError, match="kind"):
        compact(SOURCE, (), distill_spec())


def test_inputs_are_never_mutated() -> None:
    blocks = (block(S1, 0), block(S2, len(S1) + 1))
    snapshot = tuple(blocks)
    spec = distill_spec(params={"ratio": 0.78})
    params_snapshot = dict(spec.params)

    result = distill(SOURCE, blocks, spec)

    assert blocks == snapshot
    assert spec.params == params_snapshot
    assert SOURCE == "\n".join([S1, S2, S3, S4])
    assert result.output_text is not SOURCE


def test_distill_is_deterministic() -> None:
    first = distill(SOURCE, (), distill_spec(), ratio=0.78)
    second = distill(SOURCE, (), distill_spec(), ratio=0.78)
    assert first == second


# ---------------------------------------------------------------------------
# compact


def test_compact_merges_near_duplicates_keeping_first_occurrence() -> None:
    c0 = "Release checklist for the alpha launch."
    c1 = "release   CHECKLIST for the alpha launch."
    c2 = "The owner MUST rotate signing keys before deploy."
    c3 = "Release checklist for the alpha launch."
    source = "\n\n".join([c0, c1, c2, c3])
    assert len(source) == 174
    blocks = []
    cursor = 0
    for text in (c0, c1, c2, c3):
        blocks.append(block(text, cursor))
        cursor += len(text) + 2

    result = compact(source, tuple(blocks), compact_spec())

    assert result.outcome is Outcome.OK
    assert result.output_text == c0 + "\n\n" + c2
    assert result.lost_commitments == ()
    assert result.commitments == ("The owner MUST rotate signing keys before deploy.",)
    assert result.output_payload == {
        "spec_identity": "test-compact@1",
        "kind": "compact",
        "version": 1,
        "blocks_in": 4,
        "blocks_out": 2,
        "duplicates": 2,
    }
    assert result.coverage == {
        "source_sentences": 4,
        "kept_sentences": 2,
        "source_chars": 174,
        "output_chars": 90,
    }
    assert result.stats == {"commitments": 1, "kept_commitments": 1}
    json.dumps(result.output_payload)


def test_compact_refuses_when_dropped_text_holds_a_commitment() -> None:
    obligation = "The reviewer MUST sign the release note."
    filler = "Notes from the release meeting."
    source = obligation + "\n\n" + filler
    result = compact(source, (block(filler, len(obligation) + 2),), compact_spec())

    assert result.outcome is Outcome.OUTPUT_INVALID
    assert result.output_text == ""
    assert result.lost_commitments == (obligation,)
    assert result.warnings == ("commitments_lost",)


def test_compact_without_blocks_is_invalid_output() -> None:
    result = compact("Plain body text without markers.", (), compact_spec())
    assert result.outcome is Outcome.OUTPUT_INVALID
    assert result.warnings == ("no_blocks",)
    assert result.output_text == ""


def test_compact_is_deterministic() -> None:
    c0 = "Release checklist for the alpha launch."
    source = c0 + "\n\n" + c0
    blocks = (block(c0, 0), block(c0, len(c0) + 2))
    first = compact(source, blocks, compact_spec())
    second = compact(source, blocks, compact_spec())
    assert first == second
