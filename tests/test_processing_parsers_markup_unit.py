"""Unit tests for the stdlib markdown and html parsers."""

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
from cortex_v2.processing.parsers.markup import (
    DEPENDENCIES,
    HtmlParser,
    MarkdownParser,
    PARSERS,
)


def _source(
    body: str,
    *,
    filename: str | None = None,
    media_type: str = "text/markdown",
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


def _assert_spans(result, body: str, *, verbatim: bool, contain: bool) -> None:
    lines = body.splitlines()
    previous = 0
    for block in result.blocks:
        span = block.span
        assert 0 <= span.start <= span.end <= len(body)
        assert span.start >= previous
        previous = span.end
        assert block.text
        if verbatim:
            assert body[span.start : span.end] == block.text
        assert span.line_start == _line_of(body, span.start)
        assert span.line_end == _line_of(body, span.end - 1)
        assert span.line_start <= span.line_end <= len(lines)
        if contain:
            expected = lines[span.line_start - 1 : span.line_end]
            got = block.text.splitlines()
            assert len(got) == len(expected)
            assert all(g in e for g, e in zip(got, expected, strict=True))


def test_registry_shape() -> None:
    assert set(PARSERS) == {"markdown", "html"}
    assert PARSERS["markdown"].parser_id == "parser.markdown@1"
    assert PARSERS["html"].parser_id == "parser.html@1"
    assert isinstance(PARSERS["markdown"], MarkdownParser)
    assert isinstance(PARSERS["html"], HtmlParser)
    assert DEPENDENCIES == {}
    for parser in PARSERS.values():
        assert isinstance(parser, Parser)


def test_markdown_heading_paths_accumulate() -> None:
    body = "# Title\n\nintro\n\n## Section\n\nbody text\n\n### Deep\n\nx\n"
    result = PARSERS["markdown"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [(block.block_kind, block.text) for block in result.blocks] == [
        ("heading", "Title"),
        ("paragraph", "intro"),
        ("heading", "Section"),
        ("paragraph", "body text"),
        ("heading", "Deep"),
        ("paragraph", "x"),
    ]
    assert [block.heading_path for block in result.blocks] == [
        ("Title",),
        ("Title",),
        ("Title", "Section"),
        ("Title", "Section"),
        ("Title", "Section", "Deep"),
        ("Title", "Section", "Deep"),
    ]
    _assert_spans(result, body, verbatim=False, contain=True)


def test_markdown_setext_headings() -> None:
    body = "Title\n=====\n\ntext\n\nSub\n---\n"
    result = PARSERS["markdown"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [(block.block_kind, block.text) for block in result.blocks] == [
        ("heading", "Title"),
        ("paragraph", "text"),
        ("heading", "Sub"),
    ]
    assert result.blocks[0].heading_path == ("Title",)
    assert result.blocks[2].heading_path == ("Title", "Sub")


def test_markdown_fenced_code_language() -> None:
    body = "```python\nx = 1\n```\n"
    result = PARSERS["markdown"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.block_kind == "code"
    assert block.language == "python"
    assert block.text == "x = 1"
    assert body[block.span.start : block.span.end] == "x = 1"
    assert (block.span.line_start, block.span.line_end) == (2, 2)


def test_markdown_indented_code() -> None:
    body = "    let y = 2\n"
    result = PARSERS["markdown"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    block = result.blocks[0]
    assert block.block_kind == "code"
    assert block.language is None
    assert block.text == "let y = 2"


def test_markdown_lists_quotes_and_table_rows() -> None:
    body = "- one\n- two\n\n> quoted\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    result = PARSERS["markdown"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [(block.block_kind, block.text) for block in result.blocks] == [
        ("list_item", "one"),
        ("list_item", "two"),
        ("quote", "quoted"),
        ("row", "a | b"),
        ("row", "1 | 2"),
    ]
    _assert_spans(result, body, verbatim=False, contain=True)


def test_markdown_mdx_components_are_inert_text_with_warning() -> None:
    body = "text <Widget id={1} /> more\n"
    result = PARSERS["markdown"].parse(_source(body), ParserLimits())
    assert result.outcome is Outcome.OK
    assert result.blocks[0].block_kind == "paragraph"
    assert "<Widget id={1} />" in result.blocks[0].text
    assert "mdx_component_inert" in result.warnings


def test_markdown_limits_and_typed_outcomes() -> None:
    long_body = "word " * 500
    result = PARSERS["markdown"].parse(
        _source(long_body), ParserLimits(max_input_chars=50)
    )
    assert result.outcome is Outcome.OK
    assert result.truncated is True
    assert "input_truncated" in result.warnings

    many = "\n\n".join(f"para {index}" for index in range(6))
    capped = PARSERS["markdown"].parse(_source(many), ParserLimits(max_blocks=3))
    assert len(capped.blocks) == 3
    assert capped.truncated is True
    assert "blocks_truncated" in capped.warnings

    empty = PARSERS["markdown"].parse(_source("   \n\n"), ParserLimits())
    assert empty.outcome is Outcome.INPUT_EMPTY
    nul = PARSERS["markdown"].parse(_source("a\x00b"), ParserLimits())
    assert nul.outcome is Outcome.INPUT_REJECTED


def test_html_blocks_and_script_skipping() -> None:
    body = (
        "<html><head><title>T &amp; Co</title></head><body>\n"
        "<h1>Top</h1>\n"
        "<p>para one</p>\n"
        "<ul><li>item</li></ul>\n"
        "<table><tr><th>H</th><td>C</td></tr></table>\n"
        "<pre><code>x = 1</code></pre>\n"
        "<blockquote>quoted</blockquote>\n"
        "<figure><figcaption>cap</figcaption></figure>\n"
        "<script>evil()</script>\n"
        "<style>a{}</style>\n"
        "<!-- note -->\n"
        "<div>orphan</div>\n"
        "</body></html>\n"
    )
    result = PARSERS["html"].parse(
        _source(body, media_type="text/html"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    kinds = [block.block_kind for block in result.blocks]
    texts = [block.text for block in result.blocks]
    assert "script_content_skipped" in result.warnings
    assert "title" in kinds and "heading" in kinds and "cell" in kinds
    assert "code" in kinds and "quote" in kinds and "caption" in kinds
    assert texts[0] == "T & Co"
    assert "Top" in texts and "para one" in texts and "item" in texts
    assert "H" in texts and "C" in texts and "x = 1" in texts
    assert "quoted" in texts and "cap" in texts and "orphan" in texts
    for text in texts:
        assert "<" not in text and ">" not in text
        assert "evil" not in text and "a{}" not in text and "note" not in text
    _assert_spans(result, body, verbatim=False, contain=False)


def test_html_offsets_are_exact_character_offsets() -> None:
    body = "<p>hello</p>"
    result = PARSERS["html"].parse(
        _source(body, media_type="text/html"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    span = result.blocks[0].span
    assert span.start == body.index("hello")
    assert span.end == span.start + len("hello")

    entity = "<p>a&amp;b</p>"
    result = PARSERS["html"].parse(
        _source(entity, media_type="text/html"), ParserLimits()
    )
    span = result.blocks[0].span
    assert result.blocks[0].text == "a&b"
    assert span.start == entity.index("a&amp;b")
    assert span.end == span.start + len("a&amp;b")


def test_html_heading_paths_and_multiline_lines() -> None:
    body = "<h1>One</h1>\n<h2>Two</h2>\n<p>line one\nline two</p>\n"
    result = PARSERS["html"].parse(
        _source(body, media_type="text/html"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    heading = [block for block in result.blocks if block.block_kind == "heading"]
    paragraph = [block for block in result.blocks if block.block_kind == "paragraph"]
    assert [block.heading_path for block in heading] == [("One",), ("One", "Two")]
    assert paragraph[0].heading_path == ("One", "Two")
    assert paragraph[0].text == "line one\nline two"
    assert (paragraph[0].span.line_start, paragraph[0].span.line_end) == (3, 4)


def test_html_skip_tags_suppress_content() -> None:
    body = (
        "<p>a</p><svg><circle/></svg><template>t</template>"
        "<noscript>n</noscript><iframe src='x'></iframe>"
        "<object>o</object><embed><p>b</p>"
    )
    result = PARSERS["html"].parse(
        _source(body, media_type="text/html"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    texts = [block.text for block in result.blocks]
    assert texts == ["a", "b"]
    assert "script_content_skipped" in result.warnings


def test_html_comments_are_ignored() -> None:
    body = "<p>a<!-- c -->b</p>"
    result = PARSERS["html"].parse(
        _source(body, media_type="text/html"), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert result.blocks[0].text == "ab"
    assert result.warnings == ()


def test_html_limits_and_typed_outcomes() -> None:
    body = "<p>" + "word " * 400 + "</p>"
    result = PARSERS["html"].parse(
        _source(body, media_type="text/html"), ParserLimits(max_input_chars=60)
    )
    assert result.outcome is Outcome.OK
    assert result.truncated is True
    assert "input_truncated" in result.warnings

    many = "".join(f"<p>p{index}</p>" for index in range(6))
    capped = PARSERS["html"].parse(
        _source(many, media_type="text/html"), ParserLimits(max_blocks=2)
    )
    assert len(capped.blocks) == 2
    assert "blocks_truncated" in capped.warnings

    empty = PARSERS["html"].parse(
        _source("<div>  </div>", media_type="text/html"), ParserLimits()
    )
    assert empty.outcome is Outcome.INPUT_EMPTY
    nul = PARSERS["html"].parse(
        _source("<p>a\x00</p>", media_type="text/html"), ParserLimits()
    )
    assert nul.outcome is Outcome.INPUT_REJECTED


def test_dispatch_serves_markdown_format() -> None:
    result = dispatch(_source("# Hi\n\ntext\n", filename="doc.md"))
    assert result.outcome is Outcome.OK
    assert result.format_id == "markdown"
    assert result.executor == "parser.markdown@1"
