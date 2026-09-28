"""Unit tests for the stdlib csv / tsv parsers."""

from __future__ import annotations

import re
from uuid import uuid4

from cortex_v2.processing.contracts import (
    Outcome,
    Parser,
    ParserLimits,
    SourceRef,
)
from cortex_v2.processing.parsers import dispatch
from cortex_v2.processing.parsers.delimited import DEPENDENCIES, PARSERS, CsvParser

_TERM = re.compile(r"\r\n|\r|\n")


def _line_of(body: str, offset: int) -> int:
    """1-based line number containing ``offset`` (terminator stays on its line)."""
    return sum(1 for match in _TERM.finditer(body) if match.end() <= offset) + 1


def _source(
    body: str,
    *,
    filename: str | None = None,
    media_type: str = "text/csv",
) -> SourceRef:
    return SourceRef(
        scope_id=uuid4(),
        content_id=uuid4(),
        revision=1,
        content_class="knowledge",
        media_type=media_type,
        body_text=body,
        content_hash=b"\xab" * 32,
        filename=filename,
    )


def _assert_verbatim(result, body: str) -> None:
    """Every row block is an exact, ordered, non-overlapping slice of ``body``."""
    lines = body.splitlines()
    previous = 0
    for block in result.blocks:
        span = block.span
        assert 0 <= span.start <= span.end <= len(body)
        assert span.start >= previous
        previous = span.end
        assert block.text
        assert body[span.start : span.end] == block.text
        assert span.line_start == _line_of(body, span.start)
        assert span.line_end == _line_of(body, span.end - 1)
        assert span.line_start <= span.line_end <= len(lines)
        expected = lines[span.line_start - 1 : span.line_end]
        got = block.text.splitlines()
        assert len(got) == len(expected)
        assert all(g in e for g, e in zip(got, expected, strict=True))


def test_registry_shape() -> None:
    assert set(PARSERS) == {"csv", "tsv"}
    assert PARSERS["csv"].parser_id == "parser.delimited@1"
    assert PARSERS["tsv"].parser_id == "parser.delimited@1"
    assert PARSERS["csv"].format_ids == ("csv",)
    assert PARSERS["tsv"].format_ids == ("tsv",)
    assert isinstance(PARSERS["csv"], CsvParser)
    assert isinstance(PARSERS["tsv"], CsvParser)
    assert DEPENDENCIES == {}
    for parser in PARSERS.values():
        assert isinstance(parser, Parser)


def test_csv_rows_are_verbatim_with_cells_and_ordinals() -> None:
    body = "a,b\n1,2\n"
    result = PARSERS["csv"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [block.block_kind for block in result.blocks] == ["row", "row"]
    assert [block.text for block in result.blocks] == ["a,b", "1,2"]
    assert [block.span.row for block in result.blocks] == [1, 2]
    assert [block.span.cell for block in result.blocks] == [None, None]
    assert [block.span.detail["cells"] for block in result.blocks] == [
        ["a", "b"],
        ["1", "2"],
    ]
    _assert_verbatim(result, body)


def test_csv_quoted_comma_and_escaped_quotes() -> None:
    body = '"x,y",z\n"he said ""hi""",ok\n'
    result = PARSERS["csv"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [block.span.detail["cells"] for block in result.blocks] == [
        ["x,y", "z"],
        ['he said "hi"', "ok"],
    ]
    _assert_verbatim(result, body)


def test_csv_multiline_quoted_field_is_one_block() -> None:
    body = 'a,"line1\nline2",c\n2,3,4\n'
    result = PARSERS["csv"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 2
    first, second = result.blocks
    assert first.text == 'a,"line1\nline2",c'
    assert (first.span.line_start, first.span.line_end) == (1, 2)
    assert first.span.row == 1
    assert first.span.detail["cells"] == ["a", "line1\nline2", "c"]
    assert second.span.row == 2
    assert (second.span.line_start, second.span.line_end) == (3, 3)
    _assert_verbatim(result, body)


def test_tsv_splits_on_tabs_only() -> None:
    body = "x,y\tz\n1\t2\n"
    result = PARSERS["tsv"].parse(
        _source(body, media_type="text/tab-separated-values", filename="d.tsv"),
        ParserLimits(),
    )
    assert result.outcome is Outcome.OK
    assert [block.span.detail["cells"] for block in result.blocks] == [
        ["x,y", "z"],
        ["1", "2"],
    ]
    _assert_verbatim(result, body)


def test_crlf_rows_drop_the_terminator_from_text() -> None:
    body = "a,b\r\n1,2\r\n"
    result = PARSERS["csv"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [block.text for block in result.blocks] == ["a,b", "1,2"]
    _assert_verbatim(result, body)


def test_single_record_is_header_only_and_input_empty() -> None:
    result = PARSERS["csv"].parse(_source("a,b,c\n"), ParserLimits())
    assert result.outcome is Outcome.INPUT_EMPTY
    assert result.blocks == ()
    assert result.executor == "parser.delimited@1"


def test_ragged_rows_keep_verbatim_text_and_warn() -> None:
    body = "a,b\n1\n"
    result = PARSERS["csv"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert "ragged_rows" in result.warnings
    assert [block.text for block in result.blocks] == ["a,b", "1"]
    _assert_verbatim(result, body)


def test_max_rows_truncates_with_warning() -> None:
    body = "\n".join(f"{index},{index}" for index in range(5)) + "\n"
    result = PARSERS["csv"].parse(_source(body), ParserLimits(max_rows=2))
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 2
    assert [block.span.row for block in result.blocks] == [1, 2]
    assert result.truncated is True
    assert "rows_truncated" in result.warnings


def test_input_truncation_warns_and_bounds_spans() -> None:
    body = "a,b\n" + "c,d\n" * 10
    result = PARSERS["csv"].parse(_source(body), ParserLimits(max_input_chars=8))
    assert result.outcome is Outcome.OK
    assert result.truncated is True
    assert "input_truncated" in result.warnings
    _assert_verbatim(result, body)


def test_block_cap_warns() -> None:
    body = "a,b\n1,2\n3,4\n"
    result = PARSERS["csv"].parse(_source(body), ParserLimits(max_blocks=1))
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 1
    assert result.truncated is True
    assert "blocks_truncated" in result.warnings


def test_oversize_row_splits_verbatim_within_one_row() -> None:
    row = "x" * 100 + ",y"
    body = row + "\n1,2\n"
    result = PARSERS["csv"].parse(_source(body), ParserLimits(max_block_chars=40))
    assert result.outcome is Outcome.OK
    pieces = [block.text for block in result.blocks if block.span.row == 1]
    assert "".join(pieces) == row
    assert all(len(piece) <= 40 for piece in pieces)
    assert all(block.span.row == 1 for block in result.blocks if block.span.row == 1)
    _assert_verbatim(result, body)


def test_empty_and_whitespace_inputs_are_input_empty() -> None:
    for body in ("", "  \n \n"):
        result = PARSERS["csv"].parse(_source(body), ParserLimits())
        assert result.outcome is Outcome.INPUT_EMPTY
        assert result.blocks == ()


def test_nul_byte_is_input_rejected() -> None:
    result = PARSERS["csv"].parse(_source("a,b\x00c\n1,2\n"), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED
    assert result.blocks == ()


def test_result_metadata() -> None:
    result = PARSERS["csv"].parse(_source("a,b\n1,2\n"), ParserLimits())
    assert result.executor == "parser.delimited@1"
    assert result.format_id is None
    assert result.detected_by is None
    assert result.stats["blocks"] == 2


def test_dispatch_serves_csv_format() -> None:
    result = dispatch(_source("a,b\n1,2\n", filename="data.csv"))
    assert result.outcome is Outcome.OK
    assert result.format_id == "csv"
    assert result.executor == "parser.delimited@1"
    assert result.detected_by is not None
