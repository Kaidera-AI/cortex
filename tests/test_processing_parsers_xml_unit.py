"""Unit tests for the XML parser adapter (parser.xml@1)."""

from __future__ import annotations

import sys
from uuid import UUID

import pytest

from cortex_v2.processing.contracts import (
    Outcome,
    Parser,
    ParserLimits,
    SourceRef,
)
from cortex_v2.processing.keys import text_sha256
from cortex_v2.processing.parsers.xml import DEPENDENCIES, PARSERS, XmlParser

SCOPE_ID = UUID("11111111-1111-4111-8111-111111111111")
CONTENT_ID = UUID("22222222-2222-4222-8222-222222222222")

DEPENDENCY_REASON = "doc-role dependency not present in the w1 candidate test image"

CATALOG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<catalog>\n"
    '  <book id="b1">\n'
    "    <title>XML Guide</title>\n"
    "    <author>Ada</author>\n"
    "  </book>\n"
    '  <book id="b2">\n'
    "    <title>Parsing 101</title>\n"
    "  </book>\n"
    "</catalog>\n"
)


def require_defusedxml():
    return pytest.importorskip("defusedxml", reason=DEPENDENCY_REASON)


def make_source(body_text: str) -> SourceRef:
    return SourceRef(
        scope_id=SCOPE_ID,
        content_id=CONTENT_ID,
        revision=1,
        content_class="artifact",
        media_type="application/xml",
        body_text=body_text,
        content_hash=text_sha256(body_text),
    )


def test_module_exposes_parser_with_declared_dependency() -> None:
    assert set(PARSERS) == {"xml"}
    assert isinstance(PARSERS["xml"], XmlParser)
    assert PARSERS["xml"].parser_id == "parser.xml@1"
    assert PARSERS["xml"].format_ids == ("xml",)
    assert isinstance(PARSERS["xml"], Parser)
    assert DEPENDENCIES == {"xml": "defusedxml"}


def test_registry_loads_xml_adapter() -> None:
    from cortex_v2.processing import parsers as registry

    assert registry.parser_for("xml") is PARSERS["xml"]
    assert registry.declared_dependency("xml") == "defusedxml"


def test_missing_defusedxml_is_typed_executor_not_activated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "defusedxml", None)
    result = XmlParser().parse(make_source("<a>x</a>"), ParserLimits())
    assert result.outcome is Outcome.EXECUTOR_NOT_ACTIVATED
    assert result.required_role == "doc"
    assert result.executor == "parser.xml@1"
    assert result.format_id is None
    assert result.detected_by is None
    assert "defusedxml" in (result.detail or "")


def test_element_blocks_point_at_source_elements() -> None:
    require_defusedxml()
    result = XmlParser().parse(make_source(CATALOG), ParserLimits())
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.xml@1"
    assert result.format_id is None
    assert result.detected_by is None
    assert all(block.block_kind == "element" for block in result.blocks)
    by_part = {block.span.part: block for block in result.blocks}
    assert set(by_part) == {
        "/catalog",
        "/catalog/book[1]",
        "/catalog/book[1]/title",
        "/catalog/book[1]/author",
        "/catalog/book[2]",
        "/catalog/book[2]/title",
    }
    title = by_part["/catalog/book[1]/title"]
    assert title.text == "XML Guide"
    assert CATALOG[title.span.start : title.span.end] == "<title>XML Guide</title>"
    assert (title.span.line_start, title.span.line_end) == (4, 4)
    assert title.heading_path == ("catalog", "book")
    second_title = by_part["/catalog/book[2]/title"]
    assert second_title.text == "Parsing 101"
    assert CATALOG[second_title.span.start : second_title.span.end] == (
        "<title>Parsing 101</title>"
    )
    assert (second_title.span.line_start, second_title.span.line_end) == (8, 8)
    book = by_part["/catalog/book[1]"]
    assert book.text == "XML Guide\nAda"
    markup = CATALOG[book.span.start : book.span.end]
    assert markup.startswith('<book id="b1">')
    assert markup.endswith("</book>")
    assert (book.span.line_start, book.span.line_end) == (3, 6)
    catalog_block = by_part["/catalog"]
    assert catalog_block.text == "XML Guide\nAda\nParsing 101"
    catalog_markup = CATALOG[
        CATALOG.index("<catalog>") : CATALOG.index("</catalog>") + len("</catalog>")
    ]
    assert CATALOG[catalog_block.span.start : catalog_block.span.end] == (
        catalog_markup
    )


def test_non_ascii_offsets_are_character_based() -> None:
    require_defusedxml()
    doc = "<r><t>caf\u00e9</t></r>"
    result = XmlParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.OK
    by_part = {block.span.part: block for block in result.blocks}
    assert by_part["/r/t"].text == "caf\u00e9"
    assert doc[by_part["/r/t"].span.start : by_part["/r/t"].span.end] == (
        "<t>caf\u00e9</t>"
    )
    assert doc[by_part["/r"].span.start : by_part["/r"].span.end] == doc


def test_dtd_is_rejected() -> None:
    require_defusedxml()
    doc = '<!DOCTYPE r [<!ENTITY e "x">]><r>&e;</r>'
    result = XmlParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED
    assert (result.detail or "").startswith("forbidden_dtd")


def test_external_entity_is_rejected() -> None:
    require_defusedxml()
    doc = '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
    result = XmlParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED
    assert result.detail is not None


def test_malformed_document_is_corrupt() -> None:
    require_defusedxml()
    result = XmlParser().parse(make_source("<a><b></a>"), ParserLimits())
    assert result.outcome is Outcome.INPUT_CORRUPT
    assert result.detail is not None
    assert len(result.detail) <= 512


def test_depth_beyond_max_depth_is_quota_exceeded() -> None:
    require_defusedxml()
    doc = "<a><b><c>x</c></b></a>"
    result = XmlParser().parse(make_source(doc), ParserLimits(max_depth=2))
    assert result.outcome is Outcome.QUOTA_EXCEEDED
    assert result.executor == "parser.xml@1"


def test_block_limit_truncates_explicitly() -> None:
    require_defusedxml()
    result = XmlParser().parse(make_source(CATALOG), ParserLimits(max_blocks=2))
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 2
    assert result.truncated is True
    assert result.warnings == ("block_limit_reached",)


@pytest.mark.parametrize("doc", ["", "   \n "])
def test_blank_input_is_input_empty(doc: str) -> None:
    require_defusedxml()
    result = XmlParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.INPUT_EMPTY


def test_element_without_text_is_input_empty() -> None:
    require_defusedxml()
    result = XmlParser().parse(make_source("<a><b/></a>"), ParserLimits())
    assert result.outcome is Outcome.INPUT_EMPTY


def test_nul_byte_is_input_rejected() -> None:
    require_defusedxml()
    result = XmlParser().parse(make_source("<a>x\u0000y</a>"), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED


def test_oversize_input_is_quota_exceeded() -> None:
    require_defusedxml()
    result = XmlParser().parse(
        make_source("<a>0123456789</a>"), ParserLimits(max_input_chars=8)
    )
    assert result.outcome is Outcome.QUOTA_EXCEEDED
