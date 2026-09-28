"""Unit tests for the office container adapters (doc role).

Fixtures are real minimal OOXML / OpenDocument / EPUB packages assembled in
memory with stdlib ``zipfile``. Behavioural tests are guarded on
``defusedxml`` (absent from the w1 candidate test image); the dependency gate
itself is exercised unguarded by blocking the import through ``sys.modules``.
"""

from __future__ import annotations

import hashlib
import io
import sys
import types
import zipfile
from uuid import uuid4

import pytest

from cortex_v2.processing.contracts import Outcome, ParserLimits, SourceRef
from cortex_v2.processing.formats import FORMATS
from cortex_v2.processing.parsers import office

REASON = "doc-role dependency not present in the w1 candidate test image"

DOCX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
PPTX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
ODT_MEDIA = "application/vnd.oasis.opendocument.text"
ODS_MEDIA = "application/vnd.oasis.opendocument.spreadsheet"
ODP_MEDIA = "application/vnd.oasis.opendocument.presentation"

_NS_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_NS_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
_NS_P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
_NS_R = (
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships"'
)
_NS_PKG_REL = (
    'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
)
_NS_CT = (
    'xmlns="http://schemas.openxmlformats.org/package/2006/content-types"'
)
_NS_SHEET = (
    'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
)
_NS_OFFICE = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
)
_NS_TEXT = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
_NS_TABLE = 'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
_NS_DRAW = 'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"'
_NS_PRES = (
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:'
    'presentation:1.0"'
)
_NS_CONTAINER = 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
_NS_OPF = 'xmlns="http://www.idpf.org/2007/opf"'
_NS_XHTML = 'xmlns="http://www.w3.org/1999/xhtml"'

_REL_OFFICE_DOCUMENT = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "officeDocument"
)
_REL_WORKSHEET = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "worksheet"
)
_REL_SHARED_STRINGS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "sharedStrings"
)
_REL_SLIDE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "slide"
)

DOCX_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<w:document {_NS_W}><w:body>"
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
    "<w:r><w:t>Alpha</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>First </w:t></w:r>"
    "<w:r><w:t>paragraph</w:t></w:r></w:p>"
    "<w:p/>"
    '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
    "<w:r><w:t>Beta</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>"
    "</w:body></w:document>"
)

EMPTY_DOCX_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<w:document {_NS_W}><w:body><w:p/></w:body></w:document>"
)

XLSX_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<workbook {_NS_SHEET} {_NS_R}><sheets>"
    '<sheet name="Summary" sheetId="1" r:id="rId1"/>'
    '<sheet name="Detail" sheetId="2" r:id="rId2"/>'
    "</sheets></workbook>"
)

XLSX_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<Relationships {_NS_PKG_REL}>"
    f'<Relationship Id="rId1" Type="{_REL_WORKSHEET}" '
    'Target="worksheets/sheet1.xml"/>'
    f'<Relationship Id="rId2" Type="{_REL_WORKSHEET}" '
    'Target="worksheets/sheet2.xml"/>'
    f'<Relationship Id="rId3" Type="{_REL_SHARED_STRINGS}" '
    'Target="sharedStrings.xml"/>'
    "</Relationships>"
)

XLSX_SHARED_STRINGS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<sst {_NS_SHEET} count="2" uniqueCount="2">'
    "<si><t>Name</t></si><si><t>Score</t></si></sst>"
)

XLSX_SHEET1 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<worksheet {_NS_SHEET}><sheetData>"
    '<row r="1"><c r="A1" t="s"><v>0</v></c>'
    '<c r="B1" t="s"><v>1</v></c></row>'
    '<row r="2"><c r="A2"><v>41</v></c>'
    "<c r=\"B2\"><f>SUM(A2:A2)</f><v>99</v></c></row>"
    "</sheetData></worksheet>"
)

XLSX_SHEET2 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<worksheet {_NS_SHEET}><sheetData>"
    '<row r="1"><c r="A1" t="inlineStr"><is><t>Notes</t></is></c>'
    '<c r="B1" t="b"><v>1</v></c></row>'
    "</sheetData></worksheet>"
)

PPTX_PRESENTATION = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<p:presentation {_NS_P} {_NS_R}><p:sldIdLst>"
    '<p:sldId id="256" r:id="rId2"/>'
    '<p:sldId id="257" r:id="rId1"/>'
    "</p:sldIdLst></p:presentation>"
)

PPTX_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<Relationships {_NS_PKG_REL}>"
    f'<Relationship Id="rId1" Type="{_REL_SLIDE}" '
    'Target="slides/slide1.xml"/>'
    f'<Relationship Id="rId2" Type="{_REL_SLIDE}" '
    'Target="slides/slide2.xml"/>'
    "</Relationships>"
)

PPTX_SLIDE1 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<p:sld {_NS_P} {_NS_A}><p:cSld><p:spTree><p:sp><p:txBody>"
    "<a:p><a:r><a:t>Opening</a:t></a:r></a:p>"
    "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
)

PPTX_SLIDE2 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<p:sld {_NS_P} {_NS_A}><p:cSld><p:spTree><p:sp><p:txBody>"
    "<a:p><a:r><a:t>Second slide title</a:t></a:r></a:p>"
    "<a:p><a:r><a:t>Bullet</a:t></a:r></a:p>"
    "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
)

ODT_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<office:document {_NS_OFFICE} {_NS_TEXT} "
    f'office:mimetype="{ODT_MEDIA}">'
    "<office:body><office:text>"
    '<text:h text:outline-level="1">Title</text:h>'
    "<text:p>Body text</text:p>"
    "<text:p>Second body</text:p>"
    "</office:text></office:body></office:document>"
)

ODS_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<office:document {_NS_OFFICE} {_NS_TEXT} {_NS_TABLE} "
    f'office:mimetype="{ODS_MEDIA}">'
    "<office:body><office:spreadsheet>"
    '<table:table table:name="Data">'
    "<table:table-row>"
    '<table:table-cell office:value-type="string"><text:p>Label</text:p>'
    "</table:table-cell>"
    '<table:table-cell office:value-type="string"><text:p>Value</text:p>'
    "</table:table-cell>"
    "</table:table-row>"
    "<table:table-row>"
    '<table:table-cell office:value-type="string"><text:p>Total</text:p>'
    "</table:table-cell>"
    '<table:table-cell table:formula="of:=SUM([.A1])" '
    'office:value-type="float" office:value="7"><text:p>7</text:p>'
    "</table:table-cell>"
    "</table:table-row>"
    "</table:table>"
    "</office:spreadsheet></office:body></office:document>"
)

ODP_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<office:document {_NS_OFFICE} {_NS_TEXT} {_NS_DRAW} {_NS_PRES} "
    f'office:mimetype="{ODP_MEDIA}">'
    "<office:body><office:presentation>"
    '<draw:page draw:name="Intro"><presentation:shapes>'
    "<text:p>Welcome</text:p>"
    "</presentation:shapes></draw:page>"
    '<draw:page draw:name="Body"><presentation:shapes>'
    "<text:h>Points</text:h><text:p>Detail</text:p>"
    "</presentation:shapes></draw:page>"
    "</office:presentation></office:body></office:document>"
)

EPUB_CONTAINER = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<container {_NS_CONTAINER} version=\"1.0\"><rootfiles>"
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)

EPUB_OPF = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<package {_NS_OPF} version=\"3.0\"><manifest>"
    '<item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"/>'
    "</manifest><spine>"
    '<itemref idref="ch1"/><itemref idref="ch2"/>'
    "</spine></package>"
)

EPUB_CH1 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<html {_NS_XHTML}><head><title>One</title>"
    '<script>var evil = "ignore me";</script></head>'
    "<body><h1>Chapter One</h1><p>Hello <em>world</em></p></body></html>"
)

EPUB_CH2 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<html {_NS_XHTML}><body><p>Second chapter</p></body></html>"
)

EPUB_NAV = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f"<html {_NS_XHTML}><body><nav>"
    '<a href="ch1.xhtml">Chapter One</a>'
    "</nav></body></html>"
)


def _content_types(override_part: str, override_type: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Types {_NS_CT}>"
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="{override_part}" '
        f'ContentType="{override_type}"/>'
        "</Types>"
    )


def _root_rels(target: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Relationships {_NS_PKG_REL}>"
        f'<Relationship Id="rId1" Type="{_REL_OFFICE_DOCUMENT}" '
        f'Target="{target}"/>'
        "</Relationships>"
    )


def _docx_entries() -> dict[str, str]:
    return {
        "[Content_Types].xml": _content_types(
            "/word/document.xml",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document.main+xml",
        ),
        "_rels/.rels": _root_rels("word/document.xml"),
        "word/document.xml": DOCX_DOCUMENT,
    }


def _xlsx_entries() -> dict[str, str]:
    return {
        "[Content_Types].xml": _content_types(
            "/xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet.main+xml",
        ),
        "_rels/.rels": _root_rels("xl/workbook.xml"),
        "xl/workbook.xml": XLSX_WORKBOOK,
        "xl/_rels/workbook.xml.rels": XLSX_WORKBOOK_RELS,
        "xl/sharedStrings.xml": XLSX_SHARED_STRINGS,
        "xl/worksheets/sheet1.xml": XLSX_SHEET1,
        "xl/worksheets/sheet2.xml": XLSX_SHEET2,
    }


def _pptx_entries() -> dict[str, str]:
    return {
        "[Content_Types].xml": _content_types(
            "/ppt/presentation.xml",
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation.main+xml",
        ),
        "_rels/.rels": _root_rels("ppt/presentation.xml"),
        "ppt/presentation.xml": PPTX_PRESENTATION,
        "ppt/_rels/presentation.xml.rels": PPTX_RELS,
        "ppt/slides/slide1.xml": PPTX_SLIDE1,
        "ppt/slides/slide2.xml": PPTX_SLIDE2,
    }


def _odt_entries() -> dict[str, str | bytes]:
    return {
        "mimetype": ODT_MEDIA.encode("utf-8"),
        "content.xml": ODT_CONTENT,
    }


def _ods_entries() -> dict[str, str | bytes]:
    return {
        "mimetype": ODS_MEDIA.encode("utf-8"),
        "content.xml": ODS_CONTENT,
    }


def _odp_entries() -> dict[str, str | bytes]:
    return {
        "mimetype": ODP_MEDIA.encode("utf-8"),
        "content.xml": ODP_CONTENT,
    }


def _epub_entries() -> dict[str, str | bytes]:
    return {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": EPUB_CONTAINER,
        "OEBPS/content.opf": EPUB_OPF,
        "OEBPS/ch1.xhtml": EPUB_CH1,
        "OEBPS/ch2.xhtml": EPUB_CH2,
        "OEBPS/nav.xhtml": EPUB_NAV,
    }


def _zip(entries: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _source(
    data: bytes | None,
    *,
    filename: str = "package.docx",
    media_type: str = DOCX_MEDIA,
) -> SourceRef:
    return SourceRef(
        scope_id=uuid4(),
        content_id=uuid4(),
        revision=1,
        content_class="artifact",
        media_type=media_type,
        body_text="",
        content_hash=hashlib.sha256(data or b"").digest(),
        filename=filename,
        body_bytes=data,
        size_bytes=len(data) if data is not None else None,
    )


def _assert_stream(result) -> None:
    """Spans index the parser's extracted stream: ordered, exact, in range."""
    stream = ""
    for block in result.blocks:
        assert block.span.start == len(stream)
        assert block.span.end == block.span.start + len(block.text)
        assert block.span.start < block.span.end or block.text == ""
        stream += block.text + "\n"
        assert stream[block.span.start : block.span.end] == block.text
        assert (
            block.span.line_start == stream.count("\n", 0, block.span.start) + 1
        )
        assert block.span.line_end == (
            block.span.line_start + block.text.count("\n")
        )


@pytest.fixture(params=["real", "stdlib-elementtree"])
def xml_backend(request, monkeypatch) -> str:
    """Run every contract leg against real defusedxml when it is present.

    The w1 candidate test image ships no defusedxml: there the ``real`` leg
    skips via ``importorskip`` and the stdlib leg injects an
    ``xml.etree.ElementTree``-backed ``defusedxml`` through ``sys.modules``,
    so the identical parse path is exercised in the throwaway container.
    """
    if request.param == "real":
        pytest.importorskip("defusedxml", reason=REASON)
        return request.param
    import xml.etree.ElementTree as element_tree

    module = types.ModuleType("defusedxml")
    module.ElementTree = element_tree
    monkeypatch.setitem(sys.modules, "defusedxml", module)
    monkeypatch.setitem(sys.modules, "defusedxml.ElementTree", element_tree)
    return request.param


# ---------------------------------------------------------------------------
# Registry shape and the dependency gate (unguarded).
# ---------------------------------------------------------------------------


def test_registry_shape() -> None:
    assert set(office.PARSERS) == {
        "docx",
        "pptx",
        "xlsx",
        "odt",
        "odp",
        "ods",
        "epub",
        "zip_container",
    }
    for format_id, parser in office.PARSERS.items():
        assert parser.parser_id == FORMATS[format_id].parser_id
        assert format_id in parser.format_ids
    assert office.PARSERS["docx"].parser_id == "parser.ooxml@1"
    assert office.PARSERS["odt"].parser_id == "parser.opendocument@1"
    assert office.PARSERS["epub"].parser_id == "parser.epub@1"
    assert office.PARSERS["zip_container"].parser_id == "parser.zip@1"
    assert office.DEPENDENCIES == {
        format_id: "defusedxml" for format_id in office.PARSERS
    }


@pytest.mark.parametrize("format_id", sorted(office.PARSERS))
def test_missing_defusedxml_is_typed(format_id, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "defusedxml", None)
    monkeypatch.setitem(sys.modules, "defusedxml.ElementTree", None)
    data = _zip(_docx_entries())
    result = office.PARSERS[format_id].parse(
        _source(data),
        ParserLimits(),
    )
    assert result.outcome is Outcome.EXECUTOR_NOT_ACTIVATED
    assert result.required_role == "doc"
    assert "defusedxml" in (result.detail or "")
    assert "unavailable" in (result.detail or "")
    assert result.executor == FORMATS[format_id].parser_id
    assert result.blocks == ()
    assert result.format_id is None


# ---------------------------------------------------------------------------
# Concrete parsers (guarded on defusedxml).
# ---------------------------------------------------------------------------


def test_docx_paragraphs_headings_and_provenance(xml_backend) -> None:
    result = office.PARSERS["docx"].parse(
        _source(_zip(_docx_entries())), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.ooxml@1"
    assert result.format_id is None
    assert result.warnings == ()
    assert not result.truncated
    _assert_stream(result)
    assert [(b.block_kind, b.text) for b in result.blocks] == [
        ("heading", "Alpha"),
        ("paragraph", "First paragraph"),
        ("heading", "Beta"),
        ("paragraph", "Second paragraph"),
    ]
    assert [b.heading_path for b in result.blocks] == [
        ("Alpha",),
        ("Alpha",),
        ("Alpha", "Beta"),
        ("Alpha", "Beta"),
    ]
    # The empty <w:p/> keeps its document position: indices are 0, 1, 3, 4.
    assert [b.span.detail["paragraph_index"] for b in result.blocks] == [
        0,
        1,
        3,
        4,
    ]
    assert all(b.span.part == "word/document.xml" for b in result.blocks)


def test_docx_macros_are_ignored_with_warning(xml_backend) -> None:
    entries = _docx_entries()
    entries["word/vbaProject.bin"] = "\x00\x01macro payload"
    result = office.PARSERS["docx"].parse(
        _source(_zip(entries)), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert "macros_ignored" in result.warnings
    assert all("macro" not in block.text for block in result.blocks)


def test_docx_without_paragraphs_is_input_empty(xml_backend) -> None:
    entries = _docx_entries()
    entries["word/document.xml"] = EMPTY_DOCX_DOCUMENT
    result = office.PARSERS["docx"].parse(
        _source(_zip(entries)), ParserLimits()
    )
    assert result.outcome is Outcome.INPUT_EMPTY
    assert result.blocks == ()
    assert result.executor == "parser.ooxml@1"


def test_docx_malformed_part_is_input_corrupt(xml_backend) -> None:
    entries = _docx_entries()
    entries["word/document.xml"] = "<w:document>"
    result = office.PARSERS["docx"].parse(
        _source(_zip(entries)), ParserLimits()
    )
    assert result.outcome is Outcome.INPUT_CORRUPT
    assert result.detail == "malformed_xml_part"


def test_source_bytes_unavailable(xml_backend) -> None:
    result = office.PARSERS["docx"].parse(
        _source(None), ParserLimits()
    )
    assert result.outcome is Outcome.SOURCE_BYTES_UNAVAILABLE
    assert result.required_role == "doc"
    assert result.blocks == ()


def test_xlsx_rows_shared_strings_and_formula_reporting(xml_backend) -> None:
    result = office.PARSERS["xlsx"].parse(
        _source(_zip(_xlsx_entries()), media_type=XLSX_MEDIA),
        ParserLimits(),
    )
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.ooxml@1"
    _assert_stream(result)
    assert [b.block_kind for b in result.blocks] == ["row", "row", "row"]
    assert [b.text for b in result.blocks] == [
        "Name\tScore",
        "41",
        "Notes\tTRUE",
    ]
    assert [b.span.sheet for b in result.blocks] == [
        "Summary",
        "Summary",
        "Detail",
    ]
    assert [b.span.row for b in result.blocks] == [1, 2, 1]
    assert [b.span.cell for b in result.blocks] == ["A1:B1", "A2:B2", "A1:B1"]
    assert [b.span.part for b in result.blocks] == [
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/sheet2.xml",
    ]
    formula_block = result.blocks[1]
    assert formula_block.span.detail == {"formula": {"B2": "SUM(A2:A2)"}}
    # The cached result of the formula is never presented as a value.
    assert "99" not in formula_block.text
    assert "SUM" not in formula_block.text


def test_pptx_slides_follow_presentation_order(xml_backend) -> None:
    result = office.PARSERS["pptx"].parse(
        _source(_zip(_pptx_entries()), media_type=PPTX_MEDIA),
        ParserLimits(),
    )
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.ooxml@1"
    _assert_stream(result)
    assert [b.block_kind for b in result.blocks] == [
        "slide_text",
        "slide_text",
    ]
    # sldIdLst names rId2 first: slide2.xml is presented first.
    assert [b.text for b in result.blocks] == [
        "Second slide title\nBullet",
        "Opening",
    ]
    assert [b.span.slide for b in result.blocks] == [1, 2]
    assert [b.span.part for b in result.blocks] == [
        "ppt/slides/slide2.xml",
        "ppt/slides/slide1.xml",
    ]


def test_odt_paragraphs_and_headings(xml_backend) -> None:
    result = office.PARSERS["odt"].parse(
        _source(_zip(_odt_entries()), media_type=ODT_MEDIA),
        ParserLimits(),
    )
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.opendocument@1"
    _assert_stream(result)
    assert [(b.block_kind, b.text) for b in result.blocks] == [
        ("heading", "Title"),
        ("paragraph", "Body text"),
        ("paragraph", "Second body"),
    ]
    assert [b.heading_path for b in result.blocks] == [
        ("Title",),
        ("Title",),
        ("Title",),
    ]
    assert [b.span.detail["paragraph_index"] for b in result.blocks] == [
        0,
        1,
        2,
    ]
    assert all(b.span.part == "content.xml" for b in result.blocks)


def test_ods_rows_and_formula_reporting(xml_backend) -> None:
    result = office.PARSERS["ods"].parse(
        _source(_zip(_ods_entries()), media_type=ODS_MEDIA),
        ParserLimits(),
    )
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.opendocument@1"
    _assert_stream(result)
    assert [(b.block_kind, b.text) for b in result.blocks] == [
        ("row", "Label\tValue"),
        ("row", "Total"),
    ]
    assert [b.span.sheet for b in result.blocks] == ["Data", "Data"]
    assert [b.span.row for b in result.blocks] == [1, 2]
    assert [b.span.cell for b in result.blocks] == ["A1:B1", "A2:B2"]
    assert result.blocks[1].span.detail == {
        "formula": {"B2": "of:=SUM([.A1])"}
    }
    # The cached value of the formula cell never appears as content.
    assert "7" not in result.blocks[1].text


def test_odp_slides_in_page_order(xml_backend) -> None:
    result = office.PARSERS["odp"].parse(
        _source(_zip(_odp_entries()), media_type=ODP_MEDIA),
        ParserLimits(),
    )
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.opendocument@1"
    _assert_stream(result)
    assert [(b.block_kind, b.text) for b in result.blocks] == [
        ("slide_text", "Welcome"),
        ("slide_text", "Points\nDetail"),
    ]
    assert [b.span.slide for b in result.blocks] == [1, 2]
    assert [b.span.detail["page_name"] for b in result.blocks] == [
        "Intro",
        "Body",
    ]
    assert all(b.span.part == "content.xml" for b in result.blocks)


def test_epub_spine_order_and_inert_scripts(xml_backend) -> None:
    result = office.PARSERS["epub"].parse(
        _source(
            _zip(_epub_entries()),
            filename="book.epub",
            media_type="application/epub+zip",
        ),
        ParserLimits(),
    )
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.epub@1"
    assert "script_content_skipped" in result.warnings
    _assert_stream(result)
    assert [(b.block_kind, b.text) for b in result.blocks] == [
        ("heading", "Chapter One"),
        ("paragraph", "Hello world"),
        ("paragraph", "Second chapter"),
    ]
    assert [b.heading_path for b in result.blocks] == [
        ("Chapter One",),
        ("Chapter One",),
        (),
    ]
    assert [b.span.part for b in result.blocks] == [
        "OEBPS/ch1.xhtml",
        "OEBPS/ch1.xhtml",
        "OEBPS/ch2.xhtml",
    ]
    assert [b.span.detail["paragraph_index"] for b in result.blocks] == [
        0,
        1,
        0,
    ]
    joined = " ".join(block.text for block in result.blocks)
    assert "evil" not in joined
    # <title> lives in <head>: head content is never extracted as body text.
    assert all(block.text != "One" for block in result.blocks)


def test_zip_container_refines_to_every_concrete_format(xml_backend) -> None:
    cases = (
        (_docx_entries(), "docx"),
        (_pptx_entries(), "pptx"),
        (_xlsx_entries(), "xlsx"),
        (_odt_entries(), "odt"),
        (_odp_entries(), "odp"),
        (_ods_entries(), "ods"),
        (_epub_entries(), "epub"),
    )
    for entries, expected in cases:
        result = office.PARSERS["zip_container"].parse(
            _source(
                _zip(entries),
                filename="package.zip",
                media_type="application/zip",
            ),
            ParserLimits(),
        )
        assert result.outcome is Outcome.OK, expected
        assert result.format_id == expected
        assert result.executor == FORMATS[expected].parser_id
        assert result.blocks, expected


def test_zip_container_refines_docx_identically(xml_backend) -> None:
    data = _zip(_docx_entries())
    direct = office.PARSERS["docx"].parse(_source(data), ParserLimits())
    refined = office.PARSERS["zip_container"].parse(
        _source(data, filename="package.zip", media_type="application/zip"),
        ParserLimits(),
    )
    assert refined.outcome is Outcome.OK
    assert refined.format_id == "docx"
    assert refined.executor == "parser.ooxml@1"
    assert [
        (b.block_kind, b.text, b.span.as_dict()) for b in refined.blocks
    ] == [(b.block_kind, b.text, b.span.as_dict()) for b in direct.blocks]


def test_zip_container_without_markers_is_unsupported(xml_backend) -> None:
    data = _zip({"notes.txt": "hello", "data/x.bin": b"\x00\x01"})
    result = office.PARSERS["zip_container"].parse(
        _source(data, filename="package.zip", media_type="application/zip"),
        ParserLimits(),
    )
    assert result.outcome is Outcome.UNSUPPORTED_FORMAT
    assert result.detail == "unrecognized_zip_container"
    assert result.executor == "parser.zip@1"
    assert result.format_id is None
    assert result.blocks == ()


# ---------------------------------------------------------------------------
# Bounded-zip security contract (guarded on defusedxml).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["../evil.xml", "/etc/evil.xml", "word/../../evil.xml"],
)
def test_zip_slip_members_are_rejected(bad_name, xml_backend) -> None:
    entries = _docx_entries()
    entries[bad_name] = "<evil/>"
    result = office.PARSERS["docx"].parse(
        _source(_zip(entries)), ParserLimits()
    )
    assert result.outcome is Outcome.INPUT_REJECTED
    assert result.detail == "zip_slip"
    assert result.blocks == ()


def test_declared_size_over_ratio_is_quota_exceeded(xml_backend) -> None:
    entries = _docx_entries()
    entries["payload.bin"] = "A" * 200_000
    result = office.PARSERS["docx"].parse(
        _source(_zip(entries)), ParserLimits()
    )
    assert result.outcome is Outcome.QUOTA_EXCEEDED
    assert result.detail == "decompression_ratio_exceeded"


def test_truncated_zip_is_input_corrupt(xml_backend) -> None:
    data = _zip(_docx_entries())
    result = office.PARSERS["docx"].parse(
        _source(data[: len(data) // 2]), ParserLimits()
    )
    assert result.outcome is Outcome.INPUT_CORRUPT
    assert result.detail == "bad_zip_container"


def test_compressed_size_over_input_bytes_is_quota_exceeded(
    xml_backend,
) -> None:
    data = _zip(_docx_entries())
    assert len(data) > 8
    result = office.PARSERS["docx"].parse(
        _source(data), ParserLimits(max_input_bytes=8)
    )
    assert result.outcome is Outcome.QUOTA_EXCEEDED
    assert result.detail == "input_bytes_exceeded"
