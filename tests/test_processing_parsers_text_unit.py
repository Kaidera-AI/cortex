"""Unit tests for the stdlib text / source_code / unknown_text parsers."""

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
from cortex_v2.processing.parsers.text import (
    DEPENDENCIES,
    PARSERS,
    SourceCodeParser,
    TextParser,
)


def _source(
    body: str,
    *,
    filename: str | None = None,
    media_type: str = "text/plain",
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


_TERM = re.compile(r"\r\n|\r|\n")


def _line_of(body: str, offset: int) -> int:
    """1-based line number containing ``offset`` (terminator stays on its line)."""
    return sum(1 for match in _TERM.finditer(body) if match.end() <= offset) + 1


def _assert_verbatim(result, body: str) -> None:
    """Every block is an exact, ordered, non-overlapping slice of ``body``."""
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
    assert set(PARSERS) == {"text", "source_code", "unknown_text"}
    assert PARSERS["text"].parser_id == "parser.text@1"
    assert PARSERS["unknown_text"].parser_id == "parser.text@1"
    assert PARSERS["source_code"].parser_id == "parser.source_code@1"
    assert isinstance(PARSERS["text"], TextParser)
    assert isinstance(PARSERS["unknown_text"], TextParser)
    assert isinstance(PARSERS["source_code"], SourceCodeParser)
    assert DEPENDENCIES == {}
    for parser in PARSERS.values():
        assert isinstance(parser, Parser)


def test_paragraphs_are_verbatim_ordered_slices() -> None:
    body = "alpha beta\nsecond line\n\n\ngamma\n"
    result = PARSERS["text"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    texts = [block.text for block in result.blocks]
    assert texts == ["alpha beta\nsecond line", "gamma"]
    assert [block.block_kind for block in result.blocks] == ["paragraph", "paragraph"]
    _assert_verbatim(result, body)


def test_crlf_paragraphs_keep_verbatim_spans() -> None:
    body = "a\r\nb\r\n\r\nc\r\n"
    result = PARSERS["text"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [block.text for block in result.blocks] == ["a\r\nb", "c"]
    _assert_verbatim(result, body)


def test_oversize_block_splits_at_line_boundaries() -> None:
    paragraph = "x" * 20 + "\n" + "y" * 20 + "\n" + "z" * 16
    result = PARSERS["text"].parse(
        _source(paragraph), ParserLimits(max_block_chars=25)
    )
    assert result.outcome is Outcome.OK
    pieces = [block.text for block in result.blocks]
    assert "".join(pieces) == paragraph
    assert all(len(piece) <= 25 for piece in pieces)
    assert pieces == ["x" * 20 + "\n", "y" * 20 + "\n", "z" * 16]
    assert result.truncated is False
    _assert_verbatim(result, paragraph)


def test_oversize_single_line_hard_splits() -> None:
    body = "w" * 100
    result = PARSERS["text"].parse(_source(body), ParserLimits(max_block_chars=30))
    assert result.outcome is Outcome.OK
    pieces = [block.text for block in result.blocks]
    assert "".join(pieces) == body
    assert [len(piece) for piece in pieces] == [30, 30, 30, 10]
    _assert_verbatim(result, body)


def test_input_truncation_warns_and_bounds_spans() -> None:
    body = "a" * 10 + "\n\n" + "b" * 10
    result = PARSERS["text"].parse(_source(body), ParserLimits(max_input_chars=12))
    assert result.outcome is Outcome.OK
    assert result.truncated is True
    assert "input_truncated" in result.warnings
    assert [block.text for block in result.blocks] == ["a" * 10]
    _assert_verbatim(result, body)


def test_block_cap_warns() -> None:
    body = "\n\n".join(f"para {index}" for index in range(5))
    result = PARSERS["text"].parse(_source(body), ParserLimits(max_blocks=2))
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 2
    assert result.truncated is True
    assert "blocks_truncated" in result.warnings


def test_empty_and_whitespace_inputs_are_input_empty() -> None:
    for body in ("", "   \n \n\t\n"):
        result = PARSERS["text"].parse(_source(body), ParserLimits())
        assert result.outcome is Outcome.INPUT_EMPTY
        assert result.blocks == ()
        assert result.executor == "parser.text@1"


def test_nul_byte_is_input_rejected() -> None:
    result = PARSERS["text"].parse(_source("a\x00b"), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED
    assert result.blocks == ()


def test_result_metadata() -> None:
    result = PARSERS["text"].parse(_source("hello\n"), ParserLimits())
    assert result.executor == "parser.text@1"
    assert result.format_id is None
    assert result.detected_by is None
    assert result.stats["blocks"] == 1


def test_source_code_blocks_carry_language_and_symbols() -> None:
    body = "import os\n\ndef main():\n    return 1\n\nclass Widget:\n    pass\n"
    result = PARSERS["source_code"].parse(
        _source(body, filename="main.py"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert [block.block_kind for block in result.blocks] == ["code", "code", "code"]
    assert [block.language for block in result.blocks] == ["py", "py", "py"]
    assert [block.span.symbol for block in result.blocks] == [None, "main", "Widget"]
    _assert_verbatim(result, body)


def test_source_code_multi_symbol_block_records_detail() -> None:
    body = "def a():\n    pass\ndef b():\n    pass\n"
    result = PARSERS["source_code"].parse(
        _source(body, filename="mod.py"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 1
    span = result.blocks[0].span
    assert span.symbol == "a"
    assert span.detail["symbols"] == ["a", "b"]


def test_source_code_config_assignment_symbols() -> None:
    body = "key = value\nother: 2\n"
    result = PARSERS["source_code"].parse(
        _source(body, filename="app.ini"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    span = result.blocks[0].span
    assert span.symbol == "key"
    assert span.detail["symbols"] == ["key", "other"]


def test_source_code_assignment_in_code_is_not_a_symbol() -> None:
    body = "x = 5\n"
    result = PARSERS["source_code"].parse(
        _source(body, filename="mod.py"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert result.blocks[0].span.symbol is None


def test_source_code_without_filename_has_no_language() -> None:
    result = PARSERS["source_code"].parse(
        _source("def a():\n    pass\n"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert result.blocks[0].language is None
    assert result.blocks[0].span.symbol == "a"


def test_source_code_never_executes_imports() -> None:
    body = "import os\nos.system('echo pwned')\n"
    result = PARSERS["source_code"].parse(
        _source(body, filename="evil.py"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert result.blocks[0].text == "import os\nos.system('echo pwned')"
    assert result.blocks[0].block_kind == "code"
    _assert_verbatim(result, body)


def test_dispatch_serves_text_format() -> None:
    result = dispatch(_source("hello\n\nworld\n", filename="notes.txt"))
    assert result.outcome is Outcome.OK
    assert result.format_id == "text"
    assert result.executor == "parser.text@1"
    assert result.detected_by is not None
