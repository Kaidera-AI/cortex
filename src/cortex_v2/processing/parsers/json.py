"""JSON and JSON Lines parser adapters (``parser.json@1``).

Pure stdlib and verbatim: every emitted value block is an exact slice of
``SourceRef.body_text`` carrying an RFC 6901 JSON pointer, so a citation is
re-verifiable character by character against the preserved original. The
scanner is bounded: leaf indexing stops at ``max_values_indexed`` (explicit
``value_index_truncated``, never silent loss), nesting beyond ``max_depth``
is a hard ``QUOTA_EXCEEDED`` and malformed input is a typed outcome, never a
raise. ``key_count``/``value_count`` in the summary block count every object
member key and every leaf scalar in the whole document, even when indexing
was truncated.
"""

from __future__ import annotations

import json
from typing import Any

from ..contracts import (
    Outcome,
    ParseBlock,
    ParseResult,
    ParserLimits,
    SourceRef,
    Span,
)

PARSER_ID = "parser.json@1"

#: Pure-stdlib adapter: nothing to declare to the doc role.
DEPENDENCIES: dict[str, str] = {}

_WHITESPACE = " \t\n\r"
_DETAIL_LIMIT = 512


class _QuotaExceeded(Exception):
    """Internal signal: a hard ``ParserLimits`` breach stopped the scan."""


def _escape_token(token: str) -> str:
    """RFC 6901 reference-token escaping: ``~`` -> ``~0``, ``/`` -> ``~1``."""
    return token.replace("~", "~0").replace("/", "~1")


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WHITESPACE:
        index += 1
    return index


def _match_container(text: str, start: int) -> int:
    """Index of the brace/bracket closing the container opened at *start*.

    Manual, string-aware matching bounds every scan step to the container it
    walks; the document was already validated by ``json.loads`` so a missing
    closer is a contract violation, reported as corrupt input.
    """
    depth = 0
    in_string = False
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError(f"unterminated container at offset {start}")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"


class _DocumentScanner:
    """Bounded depth-first scan emitting verbatim leaf-value slices."""

    def __init__(self, text: str, limits: ParserLimits, document: Any) -> None:
        self._text = text
        self._limits = limits
        self._decoder = json.JSONDecoder()
        self.top_level_type = _type_name(document)
        self.blocks: list[ParseBlock] = []
        self.warnings: list[str] = []
        self.truncated = False
        self.key_count = 0
        self.value_count = 0
        self.doc_start = 0
        self.doc_end = 0
        self._values_full = False
        self._blocks_full = False
        self._chars_full = False

    def scan(self) -> None:
        start = _skip_whitespace(self._text, 0)
        if start >= len(self._text):
            raise ValueError("document contains no JSON value")
        end = self._scan_value(start, 0, "")
        tail = _skip_whitespace(self._text, end)
        if tail != len(self._text):
            raise ValueError(f"trailing content at offset {tail}")
        self.doc_start, self.doc_end = start, end

    def summary_block(self) -> ParseBlock:
        detail = {
            "top_level_type": self.top_level_type,
            "key_count": self.key_count,
            "value_count": self.value_count,
        }
        text = json.dumps(detail, separators=(",", ":"))
        if len(text) > self._limits.max_block_chars:
            text = text[: self._limits.max_block_chars]
        return ParseBlock(
            block_kind="json_document",
            text=text,
            span=Span(
                start=self.doc_start,
                end=self.doc_end,
                json_pointer="",
                detail=dict(detail),
            ),
        )

    def _scan_value(self, index: int, depth: int, pointer: str) -> int:
        if depth > self._limits.max_depth:
            raise _QuotaExceeded(
                f"nesting beyond max_depth={self._limits.max_depth} at offset {index}"
            )
        char = self._text[index]
        if char == "{":
            return self._scan_object(index, depth, pointer)
        if char == "[":
            return self._scan_array(index, depth, pointer)
        _, end = self._decoder.raw_decode(self._text, index)
        self._emit_leaf(index, end, pointer)
        return end

    def _scan_object(self, start: int, depth: int, pointer: str) -> int:
        close = _match_container(self._text, start)
        index = _skip_whitespace(self._text, start + 1)
        while index < close:
            key, key_end = self._decoder.raw_decode(self._text, index)
            if not isinstance(key, str):
                raise ValueError(f"non-string object key at offset {index}")
            self.key_count += 1
            index = _skip_whitespace(self._text, key_end)
            if index >= close or self._text[index] != ":":
                raise ValueError(f"expected ':' at offset {index}")
            value_start = _skip_whitespace(self._text, index + 1)
            child = f"{pointer}/{_escape_token(key)}"
            value_end = self._scan_value(value_start, depth + 1, child)
            index = _skip_whitespace(self._text, value_end)
            if index < close and self._text[index] == ",":
                index = _skip_whitespace(self._text, index + 1)
        if index != close:
            raise ValueError(f"object at offset {start} closed at offset {index}")
        return close + 1

    def _scan_array(self, start: int, depth: int, pointer: str) -> int:
        close = _match_container(self._text, start)
        index = _skip_whitespace(self._text, start + 1)
        position = 0
        while index < close:
            element_end = self._scan_value(index, depth + 1, f"{pointer}/{position}")
            position += 1
            index = _skip_whitespace(self._text, element_end)
            if index < close and self._text[index] == ",":
                index = _skip_whitespace(self._text, index + 1)
        if index != close:
            raise ValueError(f"array at offset {start} closed at offset {index}")
        return close + 1

    def _emit_leaf(self, start: int, end: int, pointer: str) -> None:
        self.value_count += 1
        if len(self.blocks) >= self._limits.max_values_indexed:
            if not self._values_full:
                self._values_full = True
                self.truncated = True
                self.warnings.append("value_index_truncated")
            return
        # One slot stays reserved for the json_document summary block.
        if len(self.blocks) + 1 >= self._limits.max_blocks:
            if not self._blocks_full:
                self._blocks_full = True
                self.truncated = True
                self.warnings.append("block_limit_reached")
            return
        text = self._text[start:end]
        if len(text) > self._limits.max_block_chars:
            text = text[: self._limits.max_block_chars]
            end = start + len(text)
            if not self._chars_full:
                self._chars_full = True
                self.truncated = True
                self.warnings.append("block_chars_truncated")
        self.blocks.append(
            ParseBlock(
                block_kind="json_value",
                text=text,
                span=Span(start=start, end=end, json_pointer=pointer),
            )
        )


class JsonParser:
    """Verbatim JSON value extractor with an RFC 6901 location index."""

    parser_id = PARSER_ID
    format_ids = ("json",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        text = source.body_text
        if "\x00" in text:
            return ParseResult(
                outcome=Outcome.INPUT_REJECTED,
                executor=self.parser_id,
                detail="text input contains a NUL byte",
            )
        if len(text) > limits.max_input_chars:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail=f"input exceeds max_input_chars={limits.max_input_chars}",
            )
        if not text.strip():
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                detail="document contains no JSON value",
            )
        try:
            document = json.loads(text)
        except RecursionError:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail="document nesting exceeds the recursion budget",
            )
        except ValueError as exc:
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                detail=str(exc)[:_DETAIL_LIMIT],
            )
        scanner = _DocumentScanner(text, limits, document)
        try:
            scanner.scan()
        except _QuotaExceeded as exc:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail=str(exc)[:_DETAIL_LIMIT],
            )
        except RecursionError:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail="document nesting exceeds the recursion budget",
            )
        except (ValueError, IndexError) as exc:
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                detail=f"{type(exc).__name__}: {exc}"[:_DETAIL_LIMIT],
            )
        return ParseResult(
            outcome=Outcome.OK,
            blocks=(scanner.summary_block(), *scanner.blocks),
            warnings=tuple(scanner.warnings),
            truncated=scanner.truncated,
            executor=self.parser_id,
        )


class JsonLinesParser:
    """Line-oriented JSONL extractor; a malformed line is reported, not lost."""

    parser_id = PARSER_ID
    format_ids = ("jsonl",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        text = source.body_text
        if "\x00" in text:
            return ParseResult(
                outcome=Outcome.INPUT_REJECTED,
                executor=self.parser_id,
                detail="text input contains a NUL byte",
            )
        if len(text) > limits.max_input_chars:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail=f"input exceeds max_input_chars={limits.max_input_chars}",
            )
        if not text.strip():
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                detail="stream contains no JSON lines",
            )
        blocks: list[ParseBlock] = []
        warnings: list[str] = []
        truncated = False
        warned: set[str] = set()
        rows = 0
        non_empty = 0
        malformed = 0
        position = 0
        line_index = 0
        while position <= len(text):
            newline = text.find("\n", position)
            line_stop = len(text) if newline == -1 else newline
            line = text[position:line_stop]
            content_end = line_stop
            if line.endswith("\r"):
                line = line[:-1]
                content_end -= 1
            if line.strip():
                non_empty += 1
                if rows >= limits.max_rows:
                    truncated = True
                    if "row_limit_reached" not in warned:
                        warned.add("row_limit_reached")
                        warnings.append("row_limit_reached")
                    break
                rows += 1
                try:
                    json.loads(line)
                except (ValueError, RecursionError):
                    malformed += 1
                    warnings.append(f"malformed_line_{line_index}")
                else:
                    if len(blocks) >= limits.max_blocks:
                        truncated = True
                        if "block_limit_reached" not in warned:
                            warned.add("block_limit_reached")
                            warnings.append("block_limit_reached")
                        break
                    block, clipped = self._line_block(
                        line, position, content_end, line_index, limits
                    )
                    if clipped and "block_chars_truncated" not in warned:
                        warned.add("block_chars_truncated")
                        truncated = True
                        warnings.append("block_chars_truncated")
                    blocks.append(block)
            if newline == -1:
                break
            position = line_stop + 1
            line_index += 1
        if non_empty and not blocks and malformed == non_empty:
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                warnings=tuple(warnings),
                truncated=truncated,
                executor=self.parser_id,
                detail=f"all {malformed} non-empty lines are malformed",
            )
        return ParseResult(
            outcome=Outcome.OK,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
            truncated=truncated,
            executor=self.parser_id,
        )

    def _line_block(
        self,
        line: str,
        start: int,
        content_end: int,
        line_index: int,
        limits: ParserLimits,
    ) -> tuple[ParseBlock, bool]:
        text = line
        end = content_end
        clipped = False
        if len(text) > limits.max_block_chars:
            text = text[: limits.max_block_chars]
            end = start + len(text)
            clipped = True
        block = ParseBlock(
            block_kind="json_line",
            text=text,
            span=Span(
                start=start,
                end=end,
                line_start=line_index + 1,
                line_end=line_index + 1,
                json_pointer=f"/{line_index}",
            ),
        )
        return block, clipped


PARSERS = {"json": JsonParser(), "jsonl": JsonLinesParser()}
