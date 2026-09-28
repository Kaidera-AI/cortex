"""RTF adapter for the Document Processor role.

``striprtf`` is a pre-approved doc-role dependency and is imported inside
``parse`` so an image without it still serves every stdlib format and reports
RTF as a typed ``executor_not_activated`` result. RTF control words are text,
never instructions: embedded objects and pictures are inventoried as warnings
and never executed or fetched.
"""

from __future__ import annotations

from ..contracts import (
    Outcome,
    ParseBlock,
    ParseResult,
    ParserLimits,
    SourceRef,
    Span,
)
from ..formats import FORMATS

DEPENDENCIES = {"rtf": "striprtf"}

_SIGNATURE = "{\\rtf"
_EMBEDDED_MARKERS = ("\\object", "\\pict", "\\ole", "\\shppict")


class RtfParser:
    """Paragraph blocks with offsets into the extracted text stream."""

    parser_id = FORMATS["rtf"].parser_id
    format_ids = ("rtf",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        raw = source.body_bytes.decode("utf-8", "replace") if source.body_bytes else (
            source.body_text or ""
        )
        if not raw.lstrip().startswith(_SIGNATURE):
            return ParseResult(
                outcome=Outcome.INPUT_REJECTED,
                executor=self.parser_id,
                detail="content is not an RTF document",
            )
        try:
            from striprtf.striprtf import rtf_to_text
        except ImportError:
            return ParseResult(
                outcome=Outcome.EXECUTOR_NOT_ACTIVATED,
                executor=self.parser_id,
                required_role="doc",
                detail="striprtf is not installed in this worker image",
            )
        try:
            text = rtf_to_text(raw)
        except (ValueError, IndexError, UnicodeDecodeError) as exc:
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                detail=f"{type(exc).__name__} while reading the RTF control words",
            )
        warnings: list[str] = []
        for marker in _EMBEDDED_MARKERS:
            if marker in raw:
                warnings.append("embedded_object_ignored")
                break
        if not text.strip():
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                warnings=tuple(warnings),
                detail="the RTF document contains no extractable text",
            )

        truncated = False
        if len(text) > limits.max_input_chars:
            text = text[: limits.max_input_chars]
            truncated = True
            warnings.append("input_truncated")

        blocks: list[ParseBlock] = []
        offset = 0
        for paragraph in text.split("\n\n"):
            length = len(paragraph)
            start, end = offset, offset + length
            offset = end + 2
            stripped = paragraph.strip()
            if not stripped:
                continue
            if len(stripped) > limits.max_block_chars:
                stripped = stripped[: limits.max_block_chars]
                end = min(end, start + limits.max_block_chars)
                truncated = True
                if "block_truncated" not in warnings:
                    warnings.append("block_truncated")
            blocks.append(
                ParseBlock(
                    block_kind="paragraph",
                    text=stripped,
                    span=Span(start=start, end=end),
                )
            )
            if len(blocks) >= limits.max_blocks:
                truncated = True
                warnings.append("blocks_truncated")
                break
        if not blocks:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                warnings=tuple(warnings),
                truncated=truncated,
                detail="the RTF document contains no extractable paragraphs",
            )
        return ParseResult(
            outcome=Outcome.OK,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
            truncated=truncated,
            executor=self.parser_id,
            stats={"paragraphs": len(blocks), "chars": len(text)},
        )


PARSERS = {"rtf": RtfParser()}
