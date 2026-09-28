"""PDF adapter for the doc role: text-layer extraction with pypdf.

``pypdf`` is imported inside the parse call, so a worker image without the
doc-role dependency still boots and answers with a typed
``executor_not_activated``. Spans index the parser's own extracted text
stream and ``span.page`` carries the 1-based page of the original (R12).
Encrypted files are only opened when the empty password works; a document
whose pages carry no text layer is reported as ``ocr_needed`` for the
optional ``image`` executor role instead of inventing content.
"""

from __future__ import annotations

import io
import re

from ..contracts import (
    EXECUTOR_DOC,
    EXECUTOR_IMAGE,
    Outcome,
    ParseBlock,
    ParseResult,
    ParserLimits,
    SourceRef,
    Span,
)
from ..formats import FORMATS

PARSER_ID = FORMATS["pdf"].parser_id

WARNING_ENCRYPTED_EMPTY_PASSWORD = "encrypted_empty_password"
WARNING_BLOCK_TRUNCATED = "block_text_truncated"
WARNING_BLOCKS_TRUNCATED = "max_blocks_exceeded"

_DEPENDENCY_DETAIL = "pypdf unavailable in this worker image"
_BYTES_DETAIL = "the referenced original bytes could not be resolved"
_OCR_DETAIL = (
    "no text layer on any page; the optional image executor role is "
    "required for optical extraction"
)

#: Paragraphs are separated by at least one blank line in the text layer.
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")


class _TextStream:
    """The parser's own extracted text stream that spans index."""

    __slots__ = ("_chunks", "_length", "_newlines")

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._length = 0
        self._newlines = 0

    def add(self, text: str) -> tuple[int, int, int, int]:
        start = self._length
        line_start = self._newlines + 1
        line_end = line_start + text.count("\n")
        self._chunks.append(text)
        self._chunks.append("\n")
        self._length = start + len(text) + 1
        self._newlines += text.count("\n") + 1
        return start, start + len(text), line_start, line_end


class PdfParser:
    """One block per non-empty paragraph of each page's text layer."""

    parser_id = PARSER_ID
    format_ids = ("pdf",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        try:
            from pypdf import PdfReader
        except ImportError:
            return ParseResult(
                outcome=Outcome.EXECUTOR_NOT_ACTIVATED,
                executor=self.parser_id,
                required_role=EXECUTOR_DOC,
                detail=_DEPENDENCY_DETAIL,
            )
        data = source.body_bytes
        if data is None:
            return ParseResult(
                outcome=Outcome.SOURCE_BYTES_UNAVAILABLE,
                executor=self.parser_id,
                required_role=EXECUTOR_DOC,
                detail=_BYTES_DETAIL,
            )
        if len(data) > limits.max_input_bytes:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail="input_bytes_exceeded",
            )
        try:
            reader = PdfReader(io.BytesIO(data))
        except Exception:  # pypdf raises many types on damaged documents
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                detail="unreadable_pdf_document",
            )
        warnings: list[str] = []
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                return ParseResult(
                    outcome=Outcome.INPUT_ENCRYPTED,
                    executor=self.parser_id,
                    detail="password_protected_pdf",
                )
            warnings.append(WARNING_ENCRYPTED_EMPTY_PASSWORD)
        try:
            page_count = len(reader.pages)
        except Exception:
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                detail="unreadable_pdf_document",
            )
        if page_count == 0:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                warnings=tuple(warnings),
                executor=self.parser_id,
                stats={"pages": 0, "pages_without_text": 0, "blocks": 0},
            )
        if page_count > limits.max_pages:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail="page_count_exceeded",
            )
        stream = _TextStream()
        blocks: list[ParseBlock] = []
        pages_without_text = 0
        truncated = False
        stopped = False
        for number, page in enumerate(reader.pages, 1):
            if stopped:
                break
            try:
                text = page.extract_text() or ""
            except Exception:
                return ParseResult(
                    outcome=Outcome.INPUT_CORRUPT,
                    executor=self.parser_id,
                    detail="unreadable_pdf_page",
                )
            if not text.strip():
                pages_without_text += 1
                continue
            for paragraph in _PARAGRAPH_BREAK.split(text):
                cleaned = paragraph.strip()
                if not cleaned:
                    continue
                if len(cleaned) > limits.max_block_chars:
                    cleaned = cleaned[: limits.max_block_chars]
                    truncated = True
                    if WARNING_BLOCK_TRUNCATED not in warnings:
                        warnings.append(WARNING_BLOCK_TRUNCATED)
                if len(blocks) >= limits.max_blocks:
                    truncated = True
                    stopped = True
                    if WARNING_BLOCKS_TRUNCATED not in warnings:
                        warnings.append(WARNING_BLOCKS_TRUNCATED)
                    break
                start, end, line_start, line_end = stream.add(cleaned)
                blocks.append(
                    ParseBlock(
                        block_kind="paragraph",
                        text=cleaned,
                        span=Span(
                            start=start,
                            end=end,
                            line_start=line_start,
                            line_end=line_end,
                            page=number,
                        ),
                    )
                )
        stats = {
            "pages": page_count,
            "pages_without_text": pages_without_text,
            "blocks": len(blocks),
        }
        if not blocks:
            if pages_without_text == page_count:
                return ParseResult(
                    outcome=Outcome.OCR_NEEDED,
                    warnings=tuple(warnings),
                    executor=self.parser_id,
                    required_role=EXECUTOR_IMAGE,
                    detail=_OCR_DETAIL,
                    stats=stats,
                )
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                warnings=tuple(warnings),
                executor=self.parser_id,
                stats=stats,
            )
        return ParseResult(
            outcome=Outcome.OK,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
            truncated=truncated,
            executor=self.parser_id,
            stats=stats,
        )


PARSERS = {"pdf": PdfParser()}

DEPENDENCIES = {"pdf": "pypdf"}
