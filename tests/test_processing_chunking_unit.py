from __future__ import annotations

import pytest

from cortex_v2.processing import keys
from cortex_v2.processing.chunking import (
    POLICY_KINDS,
    approx_tokens,
    chunk_blocks,
    policy_from_json,
)
from cortex_v2.processing.contracts import ChunkingPolicy, ParseBlock, Span

SEP = "\n\n"


def policy(
    kind: str = "structural",
    target: int = 200,
    max_chars: int = 260,
    overlap: int = 0,
) -> ChunkingPolicy:
    return ChunkingPolicy("test-policy", 1, kind, target, max_chars, overlap)


def aligned_blocks(
    texts: list[str],
    *,
    kinds: list[str] | None = None,
    headings: list[tuple[str, ...]] | None = None,
) -> tuple[str, tuple[ParseBlock, ...]]:
    """Body text plus blocks whose spans index it verbatim."""
    body = SEP.join(texts)
    blocks: list[ParseBlock] = []
    cursor = 0
    for index, text in enumerate(texts):
        blocks.append(
            ParseBlock(
                block_kind=(kinds[index] if kinds else "paragraph"),
                text=text,
                span=Span(start=cursor, end=cursor + len(text)),
                heading_path=(headings[index] if headings else ()),
            )
        )
        cursor += len(text) + len(SEP)
    return body, tuple(blocks)


# ---------------------------------------------------------------------------
# policy surface


def test_policy_kinds_exposed() -> None:
    assert set(POLICY_KINDS) == {"structural", "token_window"}


def test_empty_input_yields_no_chunks() -> None:
    assert chunk_blocks((), policy()) == ()
    assert chunk_blocks([], policy(kind="token_window")) == ()


@pytest.mark.parametrize(
    ("bad", "named"),
    [
        (ChunkingPolicy("p", 1, "structural", 100, 200, 100), "overlap_chars"),
        (ChunkingPolicy("p", 1, "structural", 100, 200, 250), "overlap_chars"),
        (ChunkingPolicy("p", 1, "structural", 100, 200, -5), "overlap_chars"),
        (ChunkingPolicy("p", 1, "structural", 100, 50, 0), "max_chars"),
        (ChunkingPolicy("p", 1, "structural", 0, 0, 0), "target_chars"),
        (ChunkingPolicy("p", 1, "semantic", 100, 200, 0), "kind"),
        (ChunkingPolicy("", 1, "structural", 100, 200, 0), "policy_id"),
        (ChunkingPolicy("p", 0, "structural", 100, 200, 0), "version"),
    ],
)
def test_invalid_policy_raises_naming_the_conflict(
    bad: ChunkingPolicy, named: str
) -> None:
    with pytest.raises(ValueError, match=named):
        chunk_blocks((), bad)


def test_policy_from_json_valid() -> None:
    value = {
        "policy_id": "structural-default",
        "version": 2,
        "kind": "token_window",
        "target_chars": 1600,
        "max_chars": 2000,
        "overlap_chars": 120,
    }
    parsed = policy_from_json(value)
    assert parsed == ChunkingPolicy(
        "structural-default", 2, "token_window", 1600, 2000, 120
    )
    assert parsed.identity == "structural-default@2"


def test_policy_from_json_rejects_unknown_key() -> None:
    value = {
        "policy_id": "p",
        "version": 1,
        "kind": "structural",
        "target_chars": 100,
        "max_chars": 200,
        "overlap_chars": 0,
        "bogus": 1,
    }
    with pytest.raises(ValueError, match="bogus"):
        policy_from_json(value)


@pytest.mark.parametrize(
    "missing",
    ["policy_id", "version", "kind", "target_chars", "max_chars", "overlap_chars"],
)
def test_policy_from_json_names_missing_key(missing: str) -> None:
    value = {
        "policy_id": "p",
        "version": 1,
        "kind": "structural",
        "target_chars": 100,
        "max_chars": 200,
        "overlap_chars": 0,
    }
    del value[missing]
    with pytest.raises(ValueError, match=missing):
        policy_from_json(value)


@pytest.mark.parametrize(
    ("field", "bad", "named"),
    [
        ("kind", "semantic", "kind"),
        ("version", "1", "version"),
        ("version", True, "version"),
        ("version", 0, "version"),
        ("target_chars", "200", "target_chars"),
        ("target_chars", 0, "target_chars"),
        ("max_chars", -5, "max_chars"),
        ("overlap_chars", -1, "overlap_chars"),
        ("policy_id", "", "policy_id"),
        ("policy_id", "x" * 129, "policy_id"),
    ],
)
def test_policy_from_json_names_bad_value(field: str, bad: object, named: str) -> None:
    value = {
        "policy_id": "p",
        "version": 1,
        "kind": "structural",
        "target_chars": 100,
        "max_chars": 200,
        "overlap_chars": 0,
    }
    value[field] = bad
    with pytest.raises(ValueError, match=named):
        policy_from_json(value)


@pytest.mark.parametrize(
    ("target", "max_chars", "overlap", "named"),
    [
        (100, 200, 100, "overlap_chars"),
        (100, 50, 0, "max_chars"),
    ],
)
def test_policy_from_json_cross_field_conflicts(
    target: int, max_chars: int, overlap: int, named: str
) -> None:
    value = {
        "policy_id": "p",
        "version": 1,
        "kind": "structural",
        "target_chars": target,
        "max_chars": max_chars,
        "overlap_chars": overlap,
    }
    with pytest.raises(ValueError, match=named):
        policy_from_json(value)


def test_approx_tokens_is_ceil_chars_over_four() -> None:
    assert approx_tokens("") == 0
    assert approx_tokens("abcd") == 1
    assert approx_tokens("x" * 10) == 3


# ---------------------------------------------------------------------------
# structural kind


def test_structural_merges_greedily_up_to_target() -> None:
    body, blocks = aligned_blocks(["A" * 80, "B" * 90, "C" * 85])
    chunks = chunk_blocks(blocks, policy(target=200, max_chars=260))

    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    assert chunks[0].text == "A" * 80 + SEP + "B" * 90
    assert chunks[1].text == "C" * 85
    assert chunks[0].block_spans == (blocks[0].span, blocks[1].span)
    assert chunks[1].block_spans == (blocks[2].span,)
    # union bounds of the contributing blocks
    assert (chunks[0].span.start, chunks[0].span.end) == (0, 80 + 2 + 90)
    assert (chunks[1].span.start, chunks[1].span.end) == (174, 174 + 85)
    assert body[chunks[1].span.start : chunks[1].span.end] == "C" * 85
    assert chunks[0].text_sha256 == keys.text_sha256(chunks[0].text)
    assert chunks[1].text_sha256 == keys.text_sha256(chunks[1].text)


def test_every_block_span_appears_exactly_once_in_order() -> None:
    texts = [f"block {i} fills its own paragraph nicely." for i in range(6)]
    kinds = ["paragraph", "table"] * 3
    body, blocks = aligned_blocks(texts, kinds=kinds)
    chunks = chunk_blocks(blocks, policy(target=120, max_chars=160))

    flat_spans = [span for chunk in chunks for span in chunk.block_spans]
    flat_kinds = [kind for chunk in chunks for kind in chunk.block_kinds]
    assert flat_spans == [block.span for block in blocks]
    assert flat_kinds == kinds
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.span.start == min(span.start for span in chunk.block_spans)
        assert chunk.span.end == max(span.end for span in chunk.block_spans)
        assert len(chunk.block_kinds) == len(chunk.block_spans)
        assert len(chunk.text) <= 160
    # verbatim provenance against the preserved body
    for span, text in zip(flat_spans, texts, strict=True):
        assert body[span.start : span.end] == text


def test_heading_path_is_deepest_common_prefix() -> None:
    _, blocks = aligned_blocks(
        ["A" * 40, "B" * 40, "C" * 40],
        headings=[("Alpha", "Beta"), ("Alpha", "Gamma"), ("Delta",)],
    )
    chunks = chunk_blocks(blocks, policy(target=100, max_chars=140))

    assert len(chunks) == 2
    assert chunks[0].heading_path == ("Alpha",)
    assert chunks[1].heading_path == ("Delta",)

    _, mixed = aligned_blocks(["x" * 10, "y" * 10], headings=[("Alpha",), ("Beta",)])
    single = chunk_blocks(mixed, policy(target=100, max_chars=140))
    assert single[0].heading_path == ()


def test_oversized_block_splits_at_sentence_boundaries() -> None:
    text = " ".join(
        f"Sentence {i:02d} pads the oversized block text." for i in range(8)
    )
    assert len(text) == 343
    block = ParseBlock(
        block_kind="paragraph", text=text, span=Span(start=0, end=len(text))
    )
    chunks = chunk_blocks((block,), policy(target=100, max_chars=120))

    assert [len(chunk.text) for chunk in chunks] == [86, 86, 86, 85]
    # pieces are exact contiguous slices of the original block
    previous_end = 0
    for chunk in chunks:
        (span,) = chunk.block_spans
        assert span.start == previous_end
        assert text[span.start : span.end] == chunk.text
        assert len(chunk.text) <= 120
        previous_end = span.end
    assert previous_end == len(text)
    # every cut landed on a sentence boundary, never mid-word
    for chunk in chunks[:-1]:
        assert chunk.text.rstrip().endswith(".")


def test_indivisible_run_is_hard_split_at_max_chars() -> None:
    text = "x" * 500
    block = ParseBlock(block_kind="code", text=text, span=Span(start=0, end=500))
    chunks = chunk_blocks((block,), policy(target=100, max_chars=120))

    assert [len(chunk.text) for chunk in chunks] == [120, 120, 120, 120, 20]
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(len(chunk.text) <= 120 for chunk in chunks)
    assert chunks[0].block_kinds == ("code",)
    previous_end = 0
    for chunk in chunks:
        (span,) = chunk.block_spans
        assert (span.start, span.end) == (previous_end, previous_end + len(chunk.text))
        previous_end = span.end


def test_piece_spans_recompute_line_numbers() -> None:
    text = "Alpha one.\nBeta two.\nGamma three.\nDelta four."
    block = ParseBlock(
        block_kind="paragraph",
        text=text,
        span=Span(start=100, end=145, line_start=5, line_end=8),
    )
    chunks = chunk_blocks((block,), policy(target=20, max_chars=25))

    assert [chunk.text for chunk in chunks] == [
        "Alpha one.\nBeta two.\n",
        "Gamma three.\nDelta four.",
    ]
    first, second = chunks
    assert (first.span.start, first.span.end) == (100, 121)
    assert (first.span.line_start, first.span.line_end) == (5, 7)
    assert (second.span.start, second.span.end) == (121, 145)
    assert (second.span.line_start, second.span.line_end) == (7, 8)


def test_structural_overlap_carries_trailing_text_only() -> None:
    texts = [f"b{i} " + "x" * 57 for i in range(4)]
    _, blocks = aligned_blocks(texts)
    chunks = chunk_blocks(blocks, policy(target=100, max_chars=140, overlap=20))

    assert [chunk.ordinal for chunk in chunks] == [0, 1, 2, 3]
    assert len(chunks[0].text) == 60
    assert all(len(chunk.text) == 82 for chunk in chunks[1:])
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.text.startswith(previous.text[-20:])
    # the carried tail is text-only: it contributes no block span
    assert chunks[1].block_spans == (blocks[1].span,)
    assert chunks[1].block_kinds == ("paragraph",)
    assert all(len(chunk.text) <= 140 for chunk in chunks)


# ---------------------------------------------------------------------------
# token_window kind


def test_token_window_honours_target_overlap_and_boundaries() -> None:
    texts = [f"Window sentence {i} covers the stream." for i in range(6)]
    _, blocks = aligned_blocks(texts)
    stream = SEP.join(texts)
    chunks = chunk_blocks(
        blocks, policy(kind="token_window", target=100, max_chars=140, overlap=25)
    )

    assert len(chunks) >= 3
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert len(chunk.text) <= 100
        assert chunk.text in stream
        # windows prefer sentence/line boundaries instead of mid-word cuts
        assert chunk.text.endswith(".")
        assert chunk.text_sha256 == keys.text_sha256(chunk.text)
    assert chunks[0].text == stream[: len(chunks[0].text)]
    assert chunks[-1].text == stream[len(stream) - len(chunks[-1].text) :]
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.text.startswith(previous.text[-25:])
    # block spans stay in document order across chunks
    starts = [chunk.block_spans[0].start for chunk in chunks]
    assert starts == sorted(starts)
    for chunk in chunks:
        assert chunk.span.start == min(span.start for span in chunk.block_spans)
        assert chunk.span.end == max(span.end for span in chunk.block_spans)


# ---------------------------------------------------------------------------
# determinism


def test_chunking_is_deterministic_across_calls() -> None:
    texts = [
        f"deterministic block number {i} with some padding text." for i in range(7)
    ]
    _, blocks = aligned_blocks(texts)
    for kind in ("structural", "token_window"):
        active = policy(kind=kind, target=110, max_chars=150, overlap=18)
        assert chunk_blocks(blocks, active) == chunk_blocks(blocks, active)
