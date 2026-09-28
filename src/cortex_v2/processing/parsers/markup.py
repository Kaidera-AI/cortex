"""Markdown and HTML parsers (stdlib only, synchronous, bounded).

Markdown is parsed line-wise (ATX/setext headings, fenced and indented code,
paragraphs, list items, blockquotes, table rows); MDX/JSX components are kept
as inert text. HTML uses :class:`html.parser.HTMLParser` with ``getpos()``
converted to character offsets through a precomputed line index; script-like
tags and comments are skipped, nothing is fetched, rendered or executed.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from html import unescape
from html.parser import HTMLParser

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

_NEWLINE = re.compile(r"\n")


def _iter_lines(text: str) -> list[tuple[int, int, int, str]]:
    """``(line_number, start, content_end, content)`` per line (see text.py)."""
    lines: list[tuple[int, int, int, str]] = []
    pos = 0
    number = 1
    for match in re.finditer(r"\r\n|\r|\n", text):
        lines.append((number, pos, match.start(), text[pos : match.start()]))
        pos = match.end()
        number += 1
    if pos < len(text):
        lines.append((number, pos, len(text), text[pos:]))
    return lines


def _finalize(
    parser_id: str,
    limits: ParserLimits,
    blocks: list[ParseBlock],
    warnings: list[str],
    truncated: bool,
    characters: int,
) -> ParseResult:
    if len(blocks) > limits.max_blocks:
        blocks = blocks[: limits.max_blocks]
        truncated = True
        warnings.append("blocks_truncated")
    if not blocks:
        return ParseResult(
            outcome=Outcome.INPUT_EMPTY,
            executor=parser_id,
            warnings=tuple(warnings),
            truncated=truncated,
            detail="no extractable text",
        )
    return ParseResult(
        outcome=Outcome.OK,
        blocks=tuple(blocks),
        warnings=tuple(warnings),
        truncated=truncated,
        executor=parser_id,
        stats={"blocks": len(blocks), "characters": characters},
    )


_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:\s+(.*?))?\s*#*\s*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
_QUOTE = re.compile(r"^ {0,3}>\s?(.*)$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_MDX = re.compile(r"</?[A-Z][A-Za-z0-9_.-]*(?:\s[^<>]*)?/?>")


def _is_table_row(
    content: str, lines: list[tuple[int, int, int, str]], index: int
) -> bool:
    stripped = content.strip()
    if "|" not in stripped:
        return False
    if stripped.startswith("|") or stripped.endswith("|"):
        return True
    return index + 1 < len(lines) and bool(_TABLE_SEP.match(lines[index + 1][3]))


class MarkdownParser:
    """Structural Markdown blocks with heading paths and fence languages."""

    parser_id = "parser.markdown@1"
    format_ids = ("markdown",)

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
        specs: list[
            tuple[
                str,
                list[str],
                list[tuple[int, int, int]],
                tuple[str, ...],
                str | None,
            ]
        ] = []
        path: list[str] = []
        mdx = False
        index = 0

        def note(text: str) -> None:
            nonlocal mdx
            if not mdx and _MDX.search(text):
                mdx = True

        def emit(
            kind: str,
            text_lines: list[str],
            raws: list[tuple[int, int, int]],
            language: str | None = None,
        ) -> None:
            specs.append((kind, text_lines, raws, tuple(path), language))

        while index < len(lines):
            number, start, end, content = lines[index]
            stripped = content.strip()
            if not stripped:
                index += 1
                continue
            fence = _FENCE_OPEN.match(content)
            if fence:
                marker, info = fence.group(1), fence.group(2)
                language = info.strip().split()[0] if info.strip() else None
                if language and "`" in language:
                    language = None
                inner: list[tuple[int, int, int, str]] = []
                index += 1
                while index < len(lines):
                    close = _FENCE_CLOSE.match(lines[index][3])
                    if (
                        close
                        and close.group(1)[0] == marker[0]
                        and len(close.group(1)) >= len(marker)
                    ):
                        index += 1
                        break
                    inner.append(lines[index])
                    index += 1
                if inner:
                    emit(
                        "code",
                        [line[3] for line in inner],
                        [(line[1], line[2], line[0]) for line in inner],
                        language,
                    )
                continue
            atx = _ATX.match(content)
            if atx and atx.group(2):
                level = len(atx.group(1))
                title = atx.group(2).strip()
                note(title)
                path[:] = path[: level - 1] + [title]
                emit("heading", [title], [(start, end, number)])
                index += 1
                continue
            if content.startswith("    "):
                run: list[tuple[int, int, int, str]] = []
                while index < len(lines) and (
                    lines[index][3].startswith("    ")
                    or (
                        not lines[index][3].strip()
                        and index + 1 < len(lines)
                        and lines[index + 1][3].startswith("    ")
                    )
                ):
                    if lines[index][3].strip():
                        run.append(lines[index])
                    index += 1
                if run:
                    emit(
                        "code",
                        [line[3][4:] for line in run],
                        [(line[1], line[2], line[0]) for line in run],
                    )
                continue
            if _QUOTE.match(content):
                run = []
                while index < len(lines) and _QUOTE.match(lines[index][3]):
                    run.append(lines[index])
                    index += 1
                texts = [_QUOTE.match(line[3]).group(1) for line in run]
                for text in texts:
                    note(text)
                emit("quote", texts, [(line[1], line[2], line[0]) for line in run])
                continue
            item = _LIST.match(content)
            if item:
                indent = len(item.group(1))
                texts = [item.group(3)]
                raws = [(start, end, number)]
                note(item.group(3))
                index += 1
                while index < len(lines):
                    follow = lines[index][3]
                    if follow.startswith(" " * (indent + 2)) and follow.strip():
                        texts.append(follow.strip())
                        raws.append((lines[index][1], lines[index][2], lines[index][0]))
                        index += 1
                        continue
                    break
                emit("list_item", texts, raws)
                continue
            if _is_table_row(content, lines, index) and not _TABLE_SEP.match(content):
                run = []
                while index < len(lines):
                    cell_line = lines[index][3].strip()
                    if _TABLE_SEP.match(lines[index][3]):
                        index += 1
                        continue
                    if not _is_table_row(lines[index][3], lines, index):
                        break
                    run.append((lines[index], cell_line))
                    index += 1
                for line, cell_line in run:
                    note(cell_line)
                    text = cell_line
                    if text.startswith("|"):
                        text = text[1:]
                    if text.endswith("|"):
                        text = text[:-1]
                    emit("row", [text.strip()], [(line[1], line[2], line[0])])
                continue
            paragraph = [content]
            raws = [(start, end, number)]
            index += 1
            while index < len(lines):
                follow = lines[index][3]
                if (
                    not follow.strip()
                    or _FENCE_OPEN.match(follow)
                    or (_ATX.match(follow) and _ATX.match(follow).group(2))
                    or _QUOTE.match(follow)
                    or _LIST.match(follow)
                    or _is_table_row(follow, lines, index)
                ):
                    break
                if _SETEXT.match(follow) and len(paragraph) == 1:
                    level = 1 if follow.strip().startswith("=") else 2
                    title = paragraph[0].strip()
                    note(title)
                    path[:] = path[: level - 1] + [title]
                    specs.append(("heading", [title], raws, tuple(path), None))
                    index += 1
                    paragraph = []
                    break
                paragraph.append(follow)
                raws.append((lines[index][1], lines[index][2], lines[index][0]))
                index += 1
            if paragraph:
                for text in paragraph:
                    note(text)
                emit("paragraph", paragraph, raws)
                continue
        if mdx:
            warnings.append("mdx_component_inert")

        blocks: list[ParseBlock] = []
        for kind, text_lines, raws, heading_path, language in specs:
            text = "\n".join(text_lines)
            if len(text) <= limits.max_block_chars:
                blocks.append(
                    ParseBlock(
                        block_kind=kind,
                        text=text,
                        span=Span(
                            start=raws[0][0],
                            end=raws[-1][1],
                            line_start=raws[0][2],
                            line_end=raws[-1][2],
                        ),
                        heading_path=heading_path,
                        language=language,
                    )
                )
                continue
            group: list[int] = []
            groups: list[list[int]] = []
            for position, line in enumerate(text_lines):
                candidate = group + [position]
                joined = "\n".join(text_lines[p] for p in candidate)
                if group and len(joined) > limits.max_block_chars:
                    groups.append(group)
                    group = [position]
                else:
                    group = candidate
            if group:
                groups.append(group)
            for member in groups:
                piece = "\n".join(text_lines[p] for p in member)
                if len(member) == 1 and len(piece) > limits.max_block_chars:
                    raw_start, raw_end, raw_line = raws[member[0]]
                    size = raw_end - raw_start
                    for offset in range(0, len(piece), limits.max_block_chars):
                        chunk = piece[offset : offset + limits.max_block_chars]
                        low = raw_start + offset * size // len(piece)
                        high = raw_start + (offset + len(chunk)) * size // len(piece)
                        blocks.append(
                            ParseBlock(
                                block_kind=kind,
                                text=chunk,
                                span=Span(
                                    start=low,
                                    end=high,
                                    line_start=raw_line,
                                    line_end=raw_line,
                                ),
                                heading_path=heading_path,
                                language=language,
                            )
                        )
                    continue
                blocks.append(
                    ParseBlock(
                        block_kind=kind,
                        text=piece,
                        span=Span(
                            start=raws[member[0]][0],
                            end=raws[member[-1]][1],
                            line_start=raws[member[0]][2],
                            line_end=raws[member[-1]][2],
                        ),
                        heading_path=heading_path,
                        language=language,
                    )
                )
        return _finalize(self.parser_id, limits, blocks, warnings, truncated, len(body))


_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_EMIT_TAGS = frozenset(
    _HEADING_TAGS
    + ("p", "li", "td", "th", "pre", "code", "blockquote", "figcaption", "title")
)
_KINDS = {
    **{tag: "heading" for tag in _HEADING_TAGS},
    "p": "paragraph",
    "li": "list_item",
    "td": "cell",
    "th": "cell",
    "pre": "code",
    "code": "code",
    "blockquote": "quote",
    "figcaption": "caption",
    "title": "title",
}
_SKIP_TAGS = frozenset(
    {"script", "style", "template", "noscript", "iframe", "svg", "object", "embed"}
)
_VOID_SKIP = frozenset({"embed"})
_CONTAINERS = frozenset(
    {
        "div",
        "section",
        "article",
        "body",
        "html",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "ul",
        "ol",
        "dl",
        "form",
        "figure",
        "header",
        "footer",
        "main",
        "nav",
        "aside",
    }
)
_AUTO_CLOSE: dict[str, frozenset[str]] = {
    "p": frozenset(
        {"p", "li", "div", "ul", "ol", "table", "blockquote", "pre", *_HEADING_TAGS}
    ),
    "li": frozenset({"li"}),
    "td": frozenset({"td", "th", "tr"}),
    "th": frozenset({"td", "th", "tr"}),
}


class _HtmlCollector(HTMLParser):
    """Collects block elements with exact character offsets per data chunk."""

    def __init__(self, line_starts: list[int]) -> None:
        super().__init__(convert_charrefs=False)
        self._line_starts = line_starts
        self.blocks: list[tuple[str, str, int, int]] = []
        self.skipped = False
        self._stack: list[list[tuple[int, int, str]]] = []
        self._tags: list[str] = []
        self._loose: list[tuple[int, int, str]] = []
        self._skip: list[str] = []
        self._path: list[str] = []
        self.heading_paths: list[tuple[str, ...]] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def _target(self) -> list[tuple[int, int, str]]:
        if self._stack:
            return self._stack[-1]
        return self._loose

    def _add(self, text: str, raw_len: int) -> None:
        if self._skip:
            return
        start = self._offset()
        self._target().append((start, start + raw_len, text))

    def _flush_loose(self) -> None:
        if self._loose and "".join(chunk[2] for chunk in self._loose).strip():
            self._emit_block("paragraph", self._loose)
        self._loose = []

    def _emit_block(self, kind: str, chunks: list[tuple[int, int, str]]) -> None:
        text = "".join(chunk[2] for chunk in chunks)
        if not text.strip():
            return
        self.blocks.append((kind, text, chunks[0][0], chunks[-1][1]))
        self.heading_paths.append(tuple(self._path))

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._skip:
            if tag in _SKIP_TAGS:
                self._skip.append(tag)
            return
        if tag in _SKIP_TAGS:
            self.skipped = True
            if tag not in _VOID_SKIP:
                self._skip.append(tag)
            return
        if tag in _EMIT_TAGS:
            self._flush_loose()
            if self._tags and (
                self._tags[-1] == tag
                or tag in _AUTO_CLOSE.get(self._tags[-1], frozenset())
            ):
                self._pop()
            self._stack.append([])
            self._tags.append(tag)
            return
        if tag in _CONTAINERS:
            self._flush_loose()

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self.skipped = True
            return
        if tag in _EMIT_TAGS:
            self._flush_loose()
            self._stack.append([])
            self._tags.append(tag)
            self._pop()

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag in _EMIT_TAGS:
            while self._tags:
                current = self._tags[-1]
                self._pop()
                if current == tag:
                    break
            return
        if tag in _CONTAINERS:
            self._flush_loose()

    def _pop(self) -> None:
        chunks = self._stack.pop()
        tag = self._tags.pop()
        kind = _KINDS[tag]
        text = "".join(chunk[2] for chunk in chunks)
        if not text.strip():
            return
        self.blocks.append((kind, text, chunks[0][0], chunks[-1][1]))
        if kind == "heading":
            level = int(tag[1])
            self._path[:] = self._path[: level - 1] + [text.strip()]
        self.heading_paths.append(tuple(self._path))

    def handle_data(self, data: str) -> None:
        self._add(data, len(data))

    def handle_entityref(self, name: str) -> None:
        raw = f"&{name};"
        self._add(unescape(raw), len(raw))

    def handle_charref(self, name: str) -> None:
        raw = f"&#{name};"
        self._add(unescape(raw), len(raw))

    def handle_comment(self, data: str) -> None:
        return

    def close(self) -> None:
        super().close()
        self._flush_loose()
        while self._tags:
            self._pop()

    def ordered_blocks(self) -> list[tuple[str, str, int, int, tuple[str, ...]]]:
        out: list[tuple[str, str, int, int, tuple[str, ...]]] = []
        paths = list(self.heading_paths)
        for (kind, text, start, end), path in zip(self.blocks, paths, strict=True):
            out.append((kind, text, start, end, path))
        return out


class HtmlParser:
    """Structural HTML blocks; script-like content and comments are skipped."""

    parser_id = "parser.html@1"
    format_ids = ("html",)

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

        line_starts = [0] + [match.end() for match in _NEWLINE.finditer(body)]
        collector = _HtmlCollector(line_starts)
        collector.feed(body)
        collector.close()
        if collector.skipped:
            warnings.append("script_content_skipped")

        blocks: list[ParseBlock] = []
        for kind, text, start, end, heading_path in collector.ordered_blocks():
            if len(text) <= limits.max_block_chars:
                blocks.append(
                    ParseBlock(
                        block_kind=kind,
                        text=text,
                        span=Span(
                            start=start,
                            end=end,
                            line_start=_line_at(line_starts, start),
                            line_end=_line_at(line_starts, max(start, end - 1)),
                        ),
                        heading_path=heading_path,
                    )
                )
                continue
            size = end - start
            for offset in range(0, len(text), limits.max_block_chars):
                chunk = text[offset : offset + limits.max_block_chars]
                low = start + offset * size // len(text)
                high = start + (offset + len(chunk)) * size // len(text)
                blocks.append(
                    ParseBlock(
                        block_kind=kind,
                        text=chunk,
                        span=Span(
                            start=low,
                            end=high,
                            line_start=_line_at(line_starts, low),
                            line_end=_line_at(line_starts, max(low, high - 1)),
                        ),
                        heading_path=heading_path,
                    )
                )
        return _finalize(self.parser_id, limits, blocks, warnings, truncated, len(body))


def _line_at(line_starts: list[int], offset: int) -> int:
    return bisect_right(line_starts, offset)


PARSERS: dict[str, MarkdownParser | HtmlParser] = {
    "markdown": MarkdownParser(),
    "html": HtmlParser(),
}
