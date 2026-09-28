"""Unit tests for the JSON/JSON Lines parser adapter (parser.json@1)."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from cortex_v2.processing.contracts import (
    Outcome,
    Parser,
    ParserLimits,
    SourceRef,
)
from cortex_v2.processing.keys import text_sha256
from cortex_v2.processing.parsers.json import (
    DEPENDENCIES,
    PARSERS,
    JsonLinesParser,
    JsonParser,
)

SCOPE_ID = UUID("11111111-1111-4111-8111-111111111111")
CONTENT_ID = UUID("22222222-2222-4222-8222-222222222222")

DOC = '{"name": "alpha", "tags": ["x", "y"], "meta": {"n": 3, "ok": true, "z": null}}'
JSONL = '{"a": 1}\n{malformed\n{"b": 2}\n'


def make_source(
    body_text: str,
    *,
    media_type: str = "application/json",
    body_bytes: bytes | None = None,
) -> SourceRef:
    return SourceRef(
        scope_id=SCOPE_ID,
        content_id=CONTENT_ID,
        revision=1,
        content_class="artifact",
        media_type=media_type,
        body_text=body_text,
        content_hash=text_sha256(body_text),
        body_bytes=body_bytes,
    )


def test_module_exposes_parsers_without_dependencies() -> None:
    assert set(PARSERS) == {"json", "jsonl"}
    assert isinstance(PARSERS["json"], JsonParser)
    assert isinstance(PARSERS["jsonl"], JsonLinesParser)
    assert PARSERS["json"].parser_id == "parser.json@1"
    assert PARSERS["jsonl"].parser_id == "parser.json@1"
    assert PARSERS["json"].format_ids == ("json",)
    assert PARSERS["jsonl"].format_ids == ("jsonl",)
    assert isinstance(PARSERS["json"], Parser)
    assert DEPENDENCIES == {}


def test_registry_loads_json_adapter() -> None:
    from cortex_v2.processing import parsers as registry

    assert registry.parser_for("json") is PARSERS["json"]
    assert registry.parser_for("jsonl") is PARSERS["jsonl"]
    assert registry.declared_dependency("json") is None


def test_leaf_blocks_are_verbatim_slices_with_rfc6901_pointers() -> None:
    result = JsonParser().parse(make_source(DOC), ParserLimits())
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.json@1"
    assert result.format_id is None
    assert result.detected_by is None
    values = [block for block in result.blocks if block.block_kind == "json_value"]
    expected = [
        ("/name", '"alpha"'),
        ("/tags/0", '"x"'),
        ("/tags/1", '"y"'),
        ("/meta/n", "3"),
        ("/meta/ok", "true"),
        ("/meta/z", "null"),
    ]
    assert [(block.span.json_pointer, block.text) for block in values] == expected
    for block in values:
        assert DOC[block.span.start : block.span.end] == block.text
        assert block.span.end > block.span.start


def test_document_summary_block_is_first() -> None:
    result = JsonParser().parse(make_source(DOC), ParserLimits())
    summary = result.blocks[0]
    assert summary.block_kind == "json_document"
    assert summary.span.json_pointer == ""
    assert summary.span.detail == {
        "top_level_type": "object",
        "key_count": 6,
        "value_count": 6,
    }
    assert json.loads(summary.text) == summary.span.detail
    assert DOC[summary.span.start : summary.span.end] == DOC


def test_top_level_array_pointers_and_types() -> None:
    doc = '[1, "two", false]'
    result = JsonParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.OK
    values = [block for block in result.blocks if block.block_kind == "json_value"]
    assert [(block.span.json_pointer, block.text) for block in values] == [
        ("/0", "1"),
        ("/1", '"two"'),
        ("/2", "false"),
    ]
    for block in values:
        assert doc[block.span.start : block.span.end] == block.text
    assert result.blocks[0].span.detail == {
        "top_level_type": "array",
        "key_count": 0,
        "value_count": 3,
    }


def test_pointer_escapes_tilde_and_slash() -> None:
    doc = '{"a~b": 1, "m/n": 2, "c\\"d": 3}'
    result = JsonParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.OK
    values = [block for block in result.blocks if block.block_kind == "json_value"]
    assert [(block.span.json_pointer, block.text) for block in values] == [
        ("/a~0b", "1"),
        ("/m~1n", "2"),
        ('/c"d', "3"),
    ]
    for block in values:
        assert doc[block.span.start : block.span.end] == block.text


def test_depth_beyond_max_depth_is_quota_exceeded() -> None:
    result = JsonParser().parse(
        make_source('{"a": {"b": {"c": 1}}}'), ParserLimits(max_depth=2)
    )
    assert result.outcome is Outcome.QUOTA_EXCEEDED
    assert result.executor == "parser.json@1"


def test_extreme_nesting_is_quota_exceeded_not_a_crash() -> None:
    doc = "[" * 2000 + "]" * 2000
    result = JsonParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.QUOTA_EXCEEDED


def test_value_index_truncates_explicitly() -> None:
    doc = json.dumps({"a": [1, 2, 3, 4, 5]})
    result = JsonParser().parse(make_source(doc), ParserLimits(max_values_indexed=2))
    assert result.outcome is Outcome.OK
    assert result.truncated is True
    assert result.warnings == ("value_index_truncated",)
    values = [block for block in result.blocks if block.block_kind == "json_value"]
    assert [(block.span.json_pointer, block.text) for block in values] == [
        ("/a/0", "1"),
        ("/a/1", "2"),
    ]
    assert result.blocks[0].span.detail["value_count"] == 5


def test_block_limit_reserves_summary_slot() -> None:
    result = JsonParser().parse(make_source(DOC), ParserLimits(max_blocks=3))
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 3
    assert result.blocks[0].block_kind == "json_document"
    assert result.truncated is True
    assert "block_limit_reached" in result.warnings


def test_oversize_value_slice_stays_verbatim_when_clipped() -> None:
    doc = json.dumps({"a": "x" * 50})
    result = JsonParser().parse(make_source(doc), ParserLimits(max_block_chars=10))
    assert result.outcome is Outcome.OK
    assert result.truncated is True
    assert "block_chars_truncated" in result.warnings
    value = next(b for b in result.blocks if b.block_kind == "json_value")
    assert len(value.text) == 10
    assert doc[value.span.start : value.span.end] == value.text


def test_corrupt_document_is_typed_corrupt() -> None:
    result = JsonParser().parse(make_source('{"a": }'), ParserLimits())
    assert result.outcome is Outcome.INPUT_CORRUPT
    assert result.detail is not None
    assert len(result.detail) <= 512


@pytest.mark.parametrize("doc", ["", "   \n\t "])
def test_empty_document_is_input_empty(doc: str) -> None:
    result = JsonParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.INPUT_EMPTY


def test_nul_byte_is_input_rejected() -> None:
    result = JsonParser().parse(make_source('{"a": "x\u0000y"}'), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED


def test_oversize_input_is_quota_exceeded() -> None:
    result = JsonParser().parse(
        make_source('{"a": "0123456789"}'), ParserLimits(max_input_chars=10)
    )
    assert result.outcome is Outcome.QUOTA_EXCEEDED


def test_jsonl_keeps_good_lines_when_one_is_malformed() -> None:
    result = JsonLinesParser().parse(
        make_source(JSONL, media_type="application/x-ndjson"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.json@1"
    assert result.warnings == ("malformed_line_1",)
    assert [block.text for block in result.blocks] == ['{"a": 1}', '{"b": 2}']
    first, second = result.blocks
    assert first.block_kind == "json_line"
    assert JSONL[first.span.start : first.span.end] == first.text
    assert (first.span.line_start, first.span.line_end) == (1, 1)
    assert first.span.json_pointer == "/0"
    assert JSONL[second.span.start : second.span.end] == second.text
    assert second.span.start == JSONL.index('{"b": 2}')
    assert (second.span.line_start, second.span.line_end) == (3, 3)
    assert second.span.json_pointer == "/2"


def test_jsonl_crlf_lines_stay_verbatim() -> None:
    doc = '{"a": 1}\r\n{"b": 2}\r\n'
    result = JsonLinesParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [block.text for block in result.blocks] == ['{"a": 1}', '{"b": 2}']
    for block in result.blocks:
        assert doc[block.span.start : block.span.end] == block.text
    assert [block.span.json_pointer for block in result.blocks] == ["/0", "/1"]


def test_jsonl_skips_blank_lines_and_counts_pointers_physically() -> None:
    doc = '{"a": 1}\n\n{"b": 2}\n'
    result = JsonLinesParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [block.span.json_pointer for block in result.blocks] == ["/0", "/2"]
    assert [(b.span.line_start, b.span.line_end) for b in result.blocks] == [
        (1, 1),
        (3, 3),
    ]


def test_jsonl_all_malformed_is_corrupt() -> None:
    result = JsonLinesParser().parse(make_source("nope\n{bad\n"), ParserLimits())
    assert result.outcome is Outcome.INPUT_CORRUPT
    assert result.warnings == ("malformed_line_0", "malformed_line_1")
    assert result.blocks == ()


@pytest.mark.parametrize("doc", ["", "\n  \n"])
def test_jsonl_without_content_is_input_empty(doc: str) -> None:
    result = JsonLinesParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.INPUT_EMPTY


def test_jsonl_row_limit_truncates() -> None:
    doc = '{"i": 1}\n{"i": 2}\n{"i": 3}\n'
    result = JsonLinesParser().parse(make_source(doc), ParserLimits(max_rows=2))
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 2
    assert result.truncated is True
    assert result.warnings == ("row_limit_reached",)


def test_jsonl_nul_byte_is_input_rejected() -> None:
    result = JsonLinesParser().parse(make_source('{"a": 1}\n\u0000\n'), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED
