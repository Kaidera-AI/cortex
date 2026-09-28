"""Plain-text and source-code parsers (stdlib only, synchronous, bounded).

Blocks are verbatim slices of ``SourceRef.body_text``: paragraphs split on
blank lines, oversize blocks re-split at line boundaries (then hard
boundaries) so ``body_text[span.start:span.end] == block.text`` always holds.
Nothing is ever imported or executed; source code is treated as text.
"""

from __future__ import annotations

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
_DEFINITION_RE = re.compile(
    r"\b(class|def|function|type|struct|enum|impl|interface|trait|record)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_CONFIG_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:=]")
#: File extensions (without dot) treated as config files, where a bare
#: ``name = value`` / ``name: value`` line counts as a definition-ish symbol.
_CONFIG_LANGUAGES = frozenset(
    {"ini", "cfg", "conf", "toml", "yaml", "yml", "env", "properties", "config"}
)


def _iter_lines(text: str) -> list[tuple[int, int, int, str]]:
    """Return ``(line_number, start, content_end, content)`` per line.

    ``content`` excludes the line terminator; ``content_end`` is the offset of
    the terminator (or of EOF), so ``text[start:content_end]`` is the line's
    visible content. Terminators ``\\r\\n``, ``\\r`` and ``\\n`` all count as
    one line break, matching ``str.splitlines``.
    """
    lines: list[tuple[int, int, int, str]] = []
    pos = 0
    number = 1
    for match in _LINE_SPLIT.finditer(text):
        lines.append((number, pos, match.start(), text[pos : match.start()]))
        pos = match.end()
        number += 1
    if pos < len(text):
        lines.append((number, pos, len(text), text[pos:]))
    return lines


def _ends_with_terminator(text: str) -> bool:
    return bool(_LINE_SPLIT.fullmatch(text[-2:])) or bool(
        _LINE_SPLIT.fullmatch(text[-1:])
    )


def _split_pieces(text: str, start: int, max_chars: int) -> list[tuple[str, int]]:
    """Split an oversize verbatim slice at line boundaries, then hard bounds.

    Returns ``(piece, piece_start_offset)`` pairs whose concatenation is
    exactly ``text``; every piece is at most ``max_chars`` characters.
    """
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
    """1-based line range a verbatim piece covers, given the block's start."""
    first = line_start + len(_LINE_SPLIT.findall(preceding))
    terms = len(_LINE_SPLIT.findall(piece))
    last = first + terms - (1 if _ends_with_terminator(piece) else 0)
    return first, last


def _language_for(filename: str | None) -> str | None:
    if not filename:
        return None
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in base:
        return None
    return base.rsplit(".", 1)[-1].lower() or None


class TextParser:
    """Paragraph blocks split on blank lines, with exact verbatim spans."""

    parser_id = "parser.text@1"
    format_ids = ("text", "unknown_text")
    _block_kind = "paragraph"

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        body = source.body_text
        if "\x00" in body:
            return ParseResult(
                outcome=Outcome.INPUT_REJECTED,
                executor=self.parser_id,
                detail="body_text contains a NUL byte",
            )
        warnings: list[str] = []
        truncated = False
        if len(body) > limits.max_input_chars:
            body = body[: limits.max_input_chars]
            truncated = True
            warnings.append("input_truncated")

        language = self._language(source)
        raw_blocks: list[tuple[int, int, int, int]] = []
        lines = _iter_lines(body)
        index = 0
        while index < len(lines):
            if not lines[index][3].strip():
                index += 1
                continue
            first = index
            while index + 1 < len(lines) and lines[index + 1][3].strip():
                index += 1
            raw_blocks.append(
                (lines[first][1], lines[index][2], lines[first][0], lines[index][0])
            )
            index += 1

        blocks: list[ParseBlock] = []
        for start, end, line_start, line_end in raw_blocks:
            text = body[start:end]
            if len(text) <= limits.max_block_chars:
                blocks.append(
                    self._block(text, start, end, line_start, line_end, language)
                )
                continue
            pieces = _split_pieces(text, start, limits.max_block_chars)
            for piece, piece_start in pieces:
                preceding = text[: piece_start - start]
                piece_start_line, piece_end_line = _piece_line_range(
                    line_start, preceding, piece
                )
                blocks.append(
                    self._block(
                        piece,
                        piece_start,
                        piece_start + len(piece),
                        piece_start_line,
                        piece_end_line,
                        language,
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

    def _language(self, source: SourceRef) -> str | None:
        return None

    def _symbols(self, text: str, language: str | None) -> list[str]:
        return []

    def _block(
        self,
        text: str,
        start: int,
        end: int,
        line_start: int,
        line_end: int,
        language: str | None,
    ) -> ParseBlock:
        symbols = self._symbols(text, language)
        detail: dict[str, object] = {}
        symbol: str | None = None
        if symbols:
            symbol = symbols[0]
            if len(symbols) > 1:
                detail["symbols"] = list(symbols)
        return ParseBlock(
            block_kind=self._block_kind,
            text=text,
            span=Span(
                start=start,
                end=end,
                line_start=line_start,
                line_end=line_end,
                symbol=symbol,
                detail=detail,
            ),
            language=language,
        )


class SourceCodeParser(TextParser):
    """Code blocks with language from the filename and definition symbols."""

    parser_id = "parser.source_code@1"
    format_ids = ("source_code",)
    _block_kind = "code"

    def _language(self, source: SourceRef) -> str | None:
        return _language_for(source.filename)

    def _symbols(self, text: str, language: str | None) -> list[str]:
        symbols: list[str] = []
        for _number, _start, _end, content in _iter_lines(text):
            match = _DEFINITION_RE.search(content)
            if match is None and language in _CONFIG_LANGUAGES:
                match = _CONFIG_ASSIGN_RE.match(content)
            if match is not None:
                name = match.group(2) if match.lastindex == 2 else match.group(1)
                if name not in symbols:
                    symbols.append(name)
        return symbols


PARSERS: dict[str, TextParser] = {
    "text": TextParser(),
    "source_code": SourceCodeParser(),
    "unknown_text": TextParser(),
}
