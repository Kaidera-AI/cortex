"""CSV/TSV parsers (stdlib only, synchronous, bounded).

One ``row`` block per record whose text is the verbatim source line(s);
quoted multi-line fields produce a single block whose span covers every
physical line of the record. ``csv.reader`` runs over an index-tracking line
iterator so offsets stay exact. Fixed dialect: the format's delimiter,
``quotechar='"'``, doublequote, no header guessing beyond the single-record
rule (a lone record is a header with no data rows).
"""

from __future__ import annotations

import csv
import re

from ..contracts import (
    Outcome,
    ParseBlock,
    ParseResult,
    ParserLimits,
    SourceRef,
    Span,
)

#: Stdlib-only adapter: no optional distribution is required.
DEPENDENCIES: dict[str, str] = {}

_LINE_SPLIT = re.compile(r"\r\n|\r|\n")


def _iter_lines(text: str) -> list[tuple[int, int, int, str, str]]:
    """``(line_number, start, content_end, content, terminator)`` per line."""
    lines: list[tuple[int, int, int, str, str]] = []
    pos = 0
    number = 1
    for match in _LINE_SPLIT.finditer(text):
        lines.append(
            (number, pos, match.start(), text[pos : match.start()], match.group(0))
        )
        pos = match.end()
        number += 1
    if pos < len(text):
        lines.append((number, pos, len(text), text[pos:], ""))
    return lines


def _ends_with_terminator(text: str) -> bool:
    return bool(_LINE_SPLIT.fullmatch(text[-2:])) or bool(
        _LINE_SPLIT.fullmatch(text[-1:])
    )


def _split_pieces(text: str, start: int, max_chars: int) -> list[tuple[str, int]]:
    """Split an oversize verbatim slice at line boundaries, then hard bounds."""
    pieces: list[tuple[str, int]] = []
    pos = 0
    while len(text) - pos > max_chars:
        cut = text.rfind("\n", pos, pos + max_chars)
        end = pos + max_chars if cut < 1 else cut + 1
        pieces.append((text[pos:end], start + pos))
        pos = end
    if pos < len(text):
        pieces.append((text[pos:], start + pos))
    return pieces


def _piece_line_range(line_start: int, preceding: str, piece: str) -> tuple[int, int]:
    first = line_start + len(_LINE_SPLIT.findall(preceding))
    terms = len(_LINE_SPLIT.findall(piece))
    last = first + terms - (1 if _ends_with_terminator(piece) else 0)
    return first, last


class _LineFeeder:
    """Feeds physical lines (with terminators) to ``csv.reader`` and records
    the line indices each record consumed, so spans stay exact even when a
    quoted field spans several lines."""

    def __init__(self, lines: list[tuple[int, int, int, str, str]]) -> None:
        self._lines = lines
        self._index = 0
        self.consumed: list[int] = []

    def __iter__(self) -> _LineFeeder:
        return self

    def __next__(self) -> str:
        if self._index >= len(self._lines):
            raise StopIteration
        _number, _start, _end, content, terminator = self._lines[self._index]
        self.consumed.append(self._index)
        self._index += 1
        return content + terminator


class CsvParser:
    """Row blocks with exact verbatim spans and parsed cell values."""

    parser_id = "parser.delimited@1"

    def __init__(self, delimiter: str) -> None:
        self.delimiter = delimiter
        self.format_ids: tuple[str, ...] = ("csv",) if delimiter == "," else ("tsv",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        if "\x00" in source.body_text:
            return ParseResult(
                outcome=Outcome.INPUT_REJECTED,
                executor=self.parser_id,
                detail="body_text contains a NUL byte",
            )
        body = source.body_text
        warnings: list[str] = []
        truncated = False
        if len(body) > limits.max_input_chars:
            body = body[: limits.max_input_chars]
            truncated = True
            warnings.append("input_truncated")

        lines = _iter_lines(body)
        feeder = _LineFeeder(lines)
        reader = csv.reader(
            feeder,
            delimiter=self.delimiter,
            quotechar='"',
            doublequote=True,
            strict=False,
        )
        records: list[tuple[tuple[int, int], list[str]]] = []
        mark = 0
        try:
            for cells in reader:
                used = feeder.consumed[mark:]
                mark = len(feeder.consumed)
                records.append(((used[0], used[-1]), list(cells)))
        except csv.Error as exc:
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                warnings=tuple(warnings),
                detail=f"csv reader failed: {exc}",
            )
        records = [
            (line_range, cells)
            for line_range, cells in records
            if any(cell.strip() for cell in cells)
        ]
        if len(records) == 1:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                warnings=tuple(warnings),
                truncated=truncated,
                detail="single record treated as a header row; no data rows",
            )
        if not records:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                warnings=tuple(warnings),
                truncated=truncated,
                detail="no extractable text",
            )
        if len(records) > limits.max_rows:
            records = records[: limits.max_rows]
            truncated = True
            warnings.append("rows_truncated")
        width = len(records[0][1])
        if any(len(cells) != width for _range, cells in records):
            warnings.append("ragged_rows")

        blocks: list[ParseBlock] = []
        for ordinal, ((first, last), cells) in enumerate(records, start=1):
            start = lines[first][1]
            end = lines[last][2]
            line_start = lines[first][0]
            line_end = lines[last][0]
            text = body[start:end]
            if len(text) <= limits.max_block_chars:
                blocks.append(
                    self._row(text, start, end, line_start, line_end, ordinal, cells)
                )
                continue
            pieces = _split_pieces(text, start, limits.max_block_chars)
            for piece, piece_start in pieces:
                preceding = text[: piece_start - start]
                piece_first, piece_last = _piece_line_range(
                    line_start, preceding, piece
                )
                blocks.append(
                    self._row(
                        piece,
                        piece_start,
                        piece_start + len(piece),
                        piece_first,
                        piece_last,
                        ordinal,
                        cells,
                    )
                )
        if len(blocks) > limits.max_blocks:
            blocks = blocks[: limits.max_blocks]
            truncated = True
            warnings.append("blocks_truncated")
        if not blocks:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                warnings=tuple(warnings),
                truncated=truncated,
                detail="no extractable text",
            )
        return ParseResult(
            outcome=Outcome.OK,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
            truncated=truncated,
            executor=self.parser_id,
            stats={"blocks": len(blocks), "characters": len(body)},
        )

    def _row(
        self,
        text: str,
        start: int,
        end: int,
        line_start: int,
        line_end: int,
        ordinal: int,
        cells: list[str],
    ) -> ParseBlock:
        return ParseBlock(
            block_kind="row",
            text=text,
            span=Span(
                start=start,
                end=end,
                line_start=line_start,
                line_end=line_end,
                row=ordinal,
                detail={"cells": list(cells)},
            ),
        )


PARSERS: dict[str, CsvParser] = {"csv": CsvParser(","), "tsv": CsvParser("\t")}
