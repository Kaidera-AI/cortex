"""Unit tests for the pypdf-backed PDF adapter (doc role).

Every logic test injects an in-memory fake ``pypdf`` module through
``sys.modules``, so the branches (corrupt, encrypted, quotas, OCR-needed,
text layer) run unguarded even in images without the doc-role dependency.
One integration test against a real pypdf-written document is guarded.
"""

from __future__ import annotations

import hashlib
import io
import sys
import types
from uuid import uuid4

import pytest

from cortex_v2.processing.contracts import Outcome, ParserLimits, SourceRef
from cortex_v2.processing.formats import FORMATS
from cortex_v2.processing.parsers import pdf as pdf_adapter

REASON = "doc-role dependency not present in the w1 candidate test image"


class _FakePage:
    def __init__(self, text: str | Exception) -> None:
        self._text = text

    def extract_text(self) -> str:
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class _FakeReader:
    """Marker-driven stand-in exercising every PdfParser branch."""

    def __init__(self, stream: io.BytesIO) -> None:
        marker = stream.read()
        if b"corrupt" in marker:
            raise ValueError("broken xref table")
        self.is_encrypted = b"encrypted" in marker
        self._opens_with_empty_password = b"open" in marker
        self.pages: list[_FakePage]
        if b"nopages" in marker:
            self.pages = []
        elif b"blank" in marker:
            self.pages = [_FakePage(""), _FakePage("   \n  ")]
        elif b"many" in marker:
            self.pages = [_FakePage(f"page {n}") for n in range(1, 6)]
        elif b"page-error" in marker:
            self.pages = [_FakePage(RuntimeError("bad content stream"))]
        elif b"long" in marker:
            self.pages = [_FakePage("A" * 100)]
        elif b"encrypted" in marker:
            self.pages = [_FakePage("Secret text")]
        else:
            self.pages = [
                _FakePage("First paragraph.\n\nSecond paragraph."),
                _FakePage(""),
                _FakePage("Third"),
            ]

    def decrypt(self, password: str) -> int:
        assert password == ""
        return 1 if self._opens_with_empty_password else 0


def _install_fake_pypdf(monkeypatch) -> None:
    module = types.ModuleType("pypdf")
    module.PdfReader = _FakeReader
    monkeypatch.setitem(sys.modules, "pypdf", module)


def _source(data: bytes | None) -> SourceRef:
    return SourceRef(
        scope_id=uuid4(),
        content_id=uuid4(),
        revision=1,
        content_class="artifact",
        media_type="application/pdf",
        body_text="",
        content_hash=hashlib.sha256(data or b"").digest(),
        filename="document.pdf",
        body_bytes=data,
        size_bytes=len(data) if data is not None else None,
    )


def _parse(data: bytes | None, limits: ParserLimits | None = None):
    return pdf_adapter.PARSERS["pdf"].parse(
        _source(data), limits or ParserLimits()
    )


def _assert_stream(result) -> None:
    stream = ""
    for block in result.blocks:
        assert block.span.start == len(stream)
        assert block.span.end == block.span.start + len(block.text)
        stream += block.text + "\n"
        assert stream[block.span.start : block.span.end] == block.text
        assert (
            block.span.line_start
            == stream.count("\n", 0, block.span.start) + 1
        )
        assert block.span.line_end == (
            block.span.line_start + block.text.count("\n")
        )


def test_registry_shape() -> None:
    assert set(pdf_adapter.PARSERS) == {"pdf"}
    parser = pdf_adapter.PARSERS["pdf"]
    assert parser.parser_id == "parser.pdf@1"
    assert parser.parser_id == FORMATS["pdf"].parser_id
    assert parser.format_ids == ("pdf",)
    assert pdf_adapter.DEPENDENCIES == {"pdf": "pypdf"}


def test_missing_pypdf_is_typed(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)
    result = _parse(b"%PDF-1.7 text")
    assert result.outcome is Outcome.EXECUTOR_NOT_ACTIVATED
    assert result.required_role == "doc"
    assert "pypdf" in (result.detail or "")
    assert "unavailable" in (result.detail or "")
    assert result.executor == "parser.pdf@1"
    assert result.blocks == ()
    assert result.format_id is None


def test_source_bytes_unavailable(monkeypatch) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(None)
    assert result.outcome is Outcome.SOURCE_BYTES_UNAVAILABLE
    assert result.required_role == "doc"
    assert result.blocks == ()


def test_compressed_size_over_input_bytes_is_quota_exceeded(
    monkeypatch,
) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 text", ParserLimits(max_input_bytes=8))
    assert result.outcome is Outcome.QUOTA_EXCEEDED
    assert result.detail == "input_bytes_exceeded"


def test_unreadable_document_is_input_corrupt(monkeypatch) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 corrupt")
    assert result.outcome is Outcome.INPUT_CORRUPT
    assert result.blocks == ()
    assert result.executor == "parser.pdf@1"


def test_encrypted_without_empty_password_is_input_encrypted(
    monkeypatch,
) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 encrypted")
    assert result.outcome is Outcome.INPUT_ENCRYPTED
    assert result.blocks == ()


def test_encrypted_opening_with_empty_password_warns(monkeypatch) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 encrypted open")
    assert result.outcome is Outcome.OK
    assert "encrypted_empty_password" in result.warnings
    assert [block.text for block in result.blocks] == ["Secret text"]
    assert result.blocks[0].span.page == 1


def test_page_count_over_max_pages_is_quota_exceeded(monkeypatch) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 many", ParserLimits(max_pages=2))
    assert result.outcome is Outcome.QUOTA_EXCEEDED
    assert result.detail == "page_count_exceeded"


def test_zero_pages_is_input_empty(monkeypatch) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 nopages")
    assert result.outcome is Outcome.INPUT_EMPTY
    assert result.blocks == ()
    assert result.stats["pages"] == 0


def test_every_page_text_free_reports_ocr_needed(monkeypatch) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 blank")
    assert result.outcome is Outcome.OCR_NEEDED
    assert result.stats["pages"] == 2
    assert result.stats["pages_without_text"] == 2
    assert result.required_role == "image"
    assert "image" in (result.detail or "")
    assert result.blocks == ()


def test_text_layer_blocks_carry_page_provenance(monkeypatch) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 text")
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.pdf@1"
    assert result.format_id is None
    _assert_stream(result)
    assert [(b.text, b.span.page) for b in result.blocks] == [
        ("First paragraph.", 1),
        ("Second paragraph.", 1),
        ("Third", 3),
    ]
    assert all(b.block_kind == "paragraph" for b in result.blocks)
    assert result.stats["pages"] == 3
    assert result.stats["pages_without_text"] == 1
    assert result.stats["blocks"] == 3


def test_page_extraction_failure_is_input_corrupt(monkeypatch) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 page-error")
    assert result.outcome is Outcome.INPUT_CORRUPT
    assert result.blocks == ()


def test_block_over_max_chars_is_truncated_explicitly(monkeypatch) -> None:
    _install_fake_pypdf(monkeypatch)
    result = _parse(b"%PDF-1.7 long", ParserLimits(max_block_chars=10))
    assert result.outcome is Outcome.OK
    assert result.truncated is True
    assert "block_text_truncated" in result.warnings
    assert [len(block.text) for block in result.blocks] == [10]


def test_real_pypdf_blank_page_reports_ocr_needed() -> None:
    pypdf = pytest.importorskip("pypdf", reason=REASON)
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    result = _parse(buffer.getvalue())
    assert result.outcome is Outcome.OCR_NEEDED
    assert result.stats["pages"] == 1
    assert result.stats["pages_without_text"] == 1
