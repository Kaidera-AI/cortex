"""EML and mbox parser adapters (``parser.mail@1``), stdlib only.

Provenance model: the ``headers`` (EML) / ``message`` (mbox) block is a
verbatim slice of the raw RFC 822 source — ``body_text`` for EML, the
decoded entry stream for mbox — spanning the first through the last of the
From/To/Subject/Date/Message-ID lines. ``part`` blocks index the *decoded*
part text: base64 or quoted-printable transfer encodings mean the decoded
content is not a slice of the raw source, so the exact location is carried
by ``span.part`` (MIME path, e.g. ``1.2``) plus ``span.row`` for mbox
entries. Attachments are inventoried (filename, content type, size, sha256)
in the message-level block's ``span.detail["attachments"]``, bounded by
``max_attachments`` and never decoded into text (``attachment_not_extracted``).
Nested ``message/rfc822`` recurses up to ``max_depth``. ``mailbox.mbox``
insists on a filesystem path, so the mbox reader subclasses it over an
in-memory buffer: no temp file, no filesystem traversal, read-only iteration.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import io
import mailbox
from email.message import Message
from typing import Any

from ..contracts import (
    Outcome,
    ParseBlock,
    ParseResult,
    ParserLimits,
    SourceRef,
    Span,
)

PARSER_ID = "parser.mail@1"

#: stdlib only: nothing to declare to the doc role.
DEPENDENCIES: dict[str, str] = {}

_POLICY = email.policy.default
_HEADER_NAMES = frozenset({"from", "to", "subject", "date", "message-id"})
_TEXT_TYPES = frozenset({"text/plain", "text/html"})
_DETAIL_LIMIT = 512
_DECODE_ERRORS = (LookupError, ValueError, KeyError, TypeError, AttributeError)


class _MemoryMbox(mailbox.mbox):
    """``mailbox.mbox`` over an in-memory byte buffer, never over a path.

    The stock constructor resolves ``path`` through ``os.path.abspath`` and
    opens a real file; parsers must not touch the filesystem, so the state
    the read path needs (buffer, table of contents, message factory) is
    assembled directly and the box is only ever iterated — never locked,
    flushed, closed or written.
    """

    def __init__(self, data: bytes) -> None:
        self._path = "<memory>"
        self._factory = None
        self._message_factory = mailbox.mboxMessage
        self._file = io.BytesIO(data)
        self._toc = None
        self._next_key = 0
        self._pending = False
        self._pending_sync = False
        self._locked = False
        self._file_length = None


def _logical_headers(raw: str) -> list[tuple[str, int, int]]:
    """(lowercased name, start offset, end offset) per logical header.

    Folded continuation lines extend the preceding header; the scan stops at
    the blank line that ends the header section. Lines without a colon get an
    empty name so message bodies never masquerade as headers.
    """
    headers: list[tuple[str, int, int]] = []
    position = 0
    while position < len(raw):
        newline = raw.find("\n", position)
        stop = len(raw) if newline == -1 else newline
        line = raw[position:stop]
        if not line.strip():
            break
        if line[:1] in (" ", "\t") and headers:
            name, start, _ = headers[-1]
            headers[-1] = (name, start, stop)
        elif ":" in line:
            name = line.partition(":")[0].strip().lower()
            headers.append((name, position, stop))
        else:
            headers.append(("", position, stop))
        if newline == -1:
            break
        position = stop + 1
    return headers


def _header_span(
    headers: list[tuple[str, int, int]],
) -> tuple[int, int] | None:
    """(start, end) covering the wanted header lines, else any real header."""
    chosen = [(start, end) for name, start, end in headers if name in _HEADER_NAMES]
    if not chosen:
        chosen = [(start, end) for name, start, end in headers if name]
    if not chosen:
        return None
    return min(start for start, _ in chosen), max(end for _, end in chosen)


def _headers_block(
    raw: str, kind: str, row: int | None, detail: dict[str, Any]
) -> ParseBlock | None:
    span_range = _header_span(_logical_headers(raw))
    if span_range is None:
        return None
    start, end = span_range
    text = raw[start:end].rstrip()
    if not text:
        return None
    line_start = raw.count("\n", 0, start) + 1
    return ParseBlock(
        block_kind=kind,
        text=text,
        span=Span(
            start=start,
            end=start + len(text),
            line_start=line_start,
            line_end=line_start + text.count("\n"),
            row=row,
            detail=detail,
        ),
    )


class _MessageExtractor:
    """One message's MIME tree -> part blocks plus an attachment inventory."""

    def __init__(
        self,
        limits: ParserLimits,
        blocks: list[ParseBlock],
        warnings: list[str],
    ) -> None:
        self._limits = limits
        self._blocks = blocks
        self._warnings = warnings
        self._warned: set[str] = set()
        self.truncated = False
        self.attachments: list[dict[str, Any]] = []

    def extract(
        self, message: Message, raw_source: str, kind: str, row: int | None
    ) -> None:
        if self._full():
            return
        self.attachments = []
        parts: list[ParseBlock] = []
        self._walk(message, "", 0, 0, row, parts)
        detail: dict[str, Any] = {}
        if self.attachments:
            detail["attachments"] = list(self.attachments)
        headers = _headers_block(raw_source, kind, row, detail)
        ordered = ([headers] if headers is not None else []) + parts
        budget = self._limits.max_blocks - len(self._blocks)
        if len(ordered) > budget:
            ordered = ordered[: max(budget, 0)]
            self.truncated = True
            self._warn("block_limit_reached")
        self._blocks.extend(ordered)

    def _full(self) -> bool:
        if len(self._blocks) >= self._limits.max_blocks:
            self.truncated = True
            self._warn("block_limit_reached")
            return True
        return False

    def _warn(self, name: str) -> None:
        if name not in self._warned:
            self._warned.add(name)
            self._warnings.append(name)

    def _walk(
        self,
        message: Message,
        prefix: str,
        depth: int,
        structure: int,
        row: int | None,
        parts: list[ParseBlock],
    ) -> None:
        # ``structure`` bounds pathological multipart nesting so the walk
        # itself stays inside the recursion budget.
        if structure > self._limits.max_depth:
            self._warn("depth_limit_reached")
            return
        if (
            message.get_content_maintype() != "message"
            and message.is_multipart()
        ):
            for index, part in enumerate(message.iter_parts(), start=1):
                path = str(index) if not prefix else f"{prefix}.{index}"
                self._walk(part, path, depth, structure + 1, row, parts)
            return
        self._leaf(message, prefix or "1", depth, structure, row, parts)

    def _leaf(
        self,
        part: Message,
        path: str,
        depth: int,
        structure: int,
        row: int | None,
        parts: list[ParseBlock],
    ) -> None:
        content_type = part.get_content_type()
        if content_type == "message/rfc822":
            if depth + 1 > self._limits.max_depth:
                self._warn("depth_limit_reached")
                return
            nested = self._nested_message(part)
            if nested is not None:
                self._walk(nested, path, depth + 1, structure + 1, row, parts)
            return
        filename = part.get_filename()
        if (
            content_type in _TEXT_TYPES
            and filename is None
            and part.get_content_disposition() != "attachment"
        ):
            block = self._text_block(part, path, content_type, row)
            if block is not None:
                parts.append(block)
            return
        self._inventory(part, content_type, filename)

    def _nested_message(self, part: Message) -> Message | None:
        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, Message):
                return first
        return None

    def _text_block(
        self, part: Message, path: str, content_type: str, row: int | None
    ) -> ParseBlock | None:
        try:
            content = part.get_content()
        except _DECODE_ERRORS:
            self._warnings.append(f"undecodable_part_{path}")
            return None
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        if not isinstance(content, str) or not content:
            return None
        text = content
        if len(text) > self._limits.max_block_chars:
            text = text[: self._limits.max_block_chars]
            self.truncated = True
            self._warn("block_chars_truncated")
        return ParseBlock(
            block_kind="part",
            text=text,
            span=Span(
                start=0,
                end=len(text),
                line_start=1,
                line_end=text.count("\n") + 1,
                row=row,
                part=path,
                detail={
                    "content_type": content_type,
                    "charset": part.get_content_charset(),
                },
            ),
        )

    def _inventory(
        self, part: Message, content_type: str, filename: str | None
    ) -> None:
        if len(self.attachments) >= self._limits.max_attachments:
            self.truncated = True
            self._warn("attachment_limit_reached")
            return
        try:
            payload = part.get_payload(decode=True)
        except _DECODE_ERRORS:
            payload = None
        if payload is None:
            raw = part.get_payload()
            payload = raw.encode("utf-8", "replace") if isinstance(raw, str) else b""
        self.attachments.append(
            {
                "filename": filename,
                "content_type": content_type,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
        self._warn("attachment_not_extracted")


class EmlParser:
    """RFC 822 message: headers, text parts, bounded attachment inventory."""

    parser_id = PARSER_ID
    format_ids = ("eml",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        text = source.body_text
        if "\x00" in text:
            return ParseResult(
                outcome=Outcome.INPUT_REJECTED,
                executor=self.parser_id,
                detail="text input contains a NUL byte",
            )
        if source.body_bytes is None:
            if len(text) > limits.max_input_chars:
                return ParseResult(
                    outcome=Outcome.QUOTA_EXCEEDED,
                    executor=self.parser_id,
                    detail=f"input exceeds max_input_chars={limits.max_input_chars}",
                )
            data = text.encode("utf-8")
        else:
            data = source.body_bytes
        if not data.strip():
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                detail="message contains no bytes",
            )
        if len(data) > limits.max_input_bytes:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail=f"input exceeds max_input_bytes={limits.max_input_bytes}",
            )
        try:
            if source.body_bytes is None:
                message = email.message_from_string(text, policy=_POLICY)
                raw_source = text
            else:
                message = email.message_from_bytes(data, policy=_POLICY)
                raw_source = data.decode("utf-8", "replace")
            blocks: list[ParseBlock] = []
            warnings: list[str] = []
            extractor = _MessageExtractor(limits, blocks, warnings)
            extractor.extract(message, raw_source, "headers", None)
        except _DECODE_ERRORS:
            # Defensive: the email API reports defects instead of raising,
            # but a hostile message must never escape as an exception.
            warnings.append("undecodable_message")
        if not blocks:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                warnings=tuple(warnings),
                detail="message contains no extractable text",
            )
        return ParseResult(
            outcome=Outcome.OK,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
            truncated=extractor.truncated,
            executor=self.parser_id,
        )


class MboxParser:
    """mbox mailbox over bytes: one ``message`` block plus parts per entry."""

    parser_id = PARSER_ID
    format_ids = ("mbox",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        text = source.body_text
        if "\x00" in text:
            return ParseResult(
                outcome=Outcome.INPUT_REJECTED,
                executor=self.parser_id,
                detail="text input contains a NUL byte",
            )
        data = (
            source.body_bytes
            if source.body_bytes is not None
            else text.encode("utf-8")
        )
        if not data.strip():
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                detail="mailbox contains no bytes",
            )
        if len(data) > limits.max_input_bytes:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail=f"input exceeds max_input_bytes={limits.max_input_bytes}",
            )
        try:
            archive = _MemoryMbox(data)
            total = len(archive)
        except (mailbox.Error, OSError, ValueError, TypeError) as exc:
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                detail=f"{type(exc).__name__}: {exc}"[:_DETAIL_LIMIT],
            )
        if total == 0:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                detail="mailbox contains no messages",
            )
        blocks: list[ParseBlock] = []
        warnings: list[str] = []
        extractor = _MessageExtractor(limits, blocks, warnings)
        rows = min(total, limits.max_rows)
        if total > limits.max_rows:
            extractor.truncated = True
            warnings.append("row_limit_reached")
        for index, entry in enumerate(archive):
            if index >= rows or len(blocks) >= limits.max_blocks:
                break
            try:
                entry_bytes = entry.as_bytes(unixfrom=False)
                raw_source = entry_bytes.decode("utf-8", "replace")
                message = email.message_from_bytes(entry_bytes, policy=_POLICY)
                extractor.extract(message, raw_source, "message", index)
            except _DECODE_ERRORS:
                warnings.append(f"undecodable_message_{index}")
        if not blocks:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                warnings=tuple(warnings),
                detail="mailbox contains no extractable text",
            )
        return ParseResult(
            outcome=Outcome.OK,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
            truncated=extractor.truncated,
            executor=self.parser_id,
        )


PARSERS = {"eml": EmlParser(), "mbox": MboxParser()}
