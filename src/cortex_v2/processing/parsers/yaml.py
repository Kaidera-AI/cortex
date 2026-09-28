"""YAML parser adapter (``parser.yaml@1``).

Validation runs through ``yaml.safe_load`` only — never ``load``, never
custom tags — so an unknown or object-constructing tag is a typed
``INPUT_REJECTED`` (``unsafe_yaml_tag``) instead of code execution. Blocks
come from an indentation scan of the raw text, not from the loaded object:
one verbatim ``section`` block per top-level document section with exact
line ranges and an RFC 6901 pointer for the section key, so every citation
re-verifies against ``SourceRef.body_text``. Nested structure deeper than
``max_depth`` is summarized in ``span.detail`` instead of expanded.
PyYAML is imported inside ``parse``; its absence is a typed
``EXECUTOR_NOT_ACTIVATED`` outcome for the ``doc`` role, never an
ImportError at boot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts import (
    Outcome,
    ParseBlock,
    ParseResult,
    ParserLimits,
    SourceRef,
    Span,
)

PARSER_ID = "parser.yaml@1"

#: Distribution the doc role must provide for this format.
DEPENDENCIES = {"yaml": "PyYAML"}

_DETAIL_LIMIT = 512
_INDICATORS = "{[&*!|>%@`"


def _escape_token(token: str) -> str:
    """RFC 6901 reference-token escaping: ``~`` -> ``~0``, ``/`` -> ``~1``."""
    return token.replace("~", "~0").replace("/", "~1")


@dataclass(slots=True)
class _Line:
    index: int
    start: int
    content_end: int
    content: str


@dataclass(slots=True)
class _Section:
    start: int
    start_line: int
    content_end: int
    key: str | None
    item_index: int | None
    indents: set[int] = field(default_factory=set)


def _scan_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    position = 0
    index = 0
    while position <= len(text):
        newline = text.find("\n", position)
        stop = len(text) if newline == -1 else newline
        content = text[position:stop]
        content_end = stop
        if content.endswith("\r"):
            content = content[:-1]
            content_end -= 1
        lines.append(_Line(index, position, content_end, content))
        if newline == -1:
            break
        position = stop + 1
        index += 1
    return lines


def _section_key(stripped: str) -> str | None:
    """Top-level mapping key of a section-opening line, when identifiable."""
    if not stripped or stripped[0] in _INDICATORS:
        return None
    first = stripped[0]
    if first in "\"'":
        close = stripped.find(first, 1)
        if close > 0 and stripped[close + 1 : close + 2] == ":":
            return stripped[1:close]
        return None
    colon = stripped.find(":")
    while colon != -1:
        if colon + 1 == len(stripped) or stripped[colon + 1] in " \t":
            key = stripped[:colon].strip()
            return key or None
        colon = stripped.find(":", colon + 1)
    return None


def _collect_sections(lines: list[_Line]) -> list[_Section]:
    """Group raw lines into top-level sections (mapping keys or sequence items)."""
    sections: list[_Section] = []
    baseline: int | None = None
    current: _Section | None = None
    pending: tuple[int, int] | None = None
    item_counter = 0
    for line in lines:
        stripped = line.content.strip()
        if not stripped:
            continue
        indent = len(line.content) - len(line.content.lstrip())
        is_comment = stripped.startswith("#")
        is_marker = (
            stripped in ("---", "...")
            or stripped.startswith("--- ")
            or stripped.startswith("%")
        )
        if is_marker:
            if current is not None:
                sections.append(current)
                current = None
            if pending is None:
                pending = (line.start, line.index)
            continue
        if baseline is None and not is_comment:
            baseline = indent
        if current is not None and (is_comment or indent > baseline):
            # Folded into the open section: comment or deeper-indented detail.
            current.content_end = line.content_end
            if not is_comment:
                current.indents.add(indent)
            continue
        if current is not None:
            sections.append(current)
            current = None
        if is_comment:
            if pending is None:
                pending = (line.start, line.index)
            continue
        if stripped == "-" or stripped.startswith("- "):
            key: str | None = None
            item_index: int | None = item_counter
            item_counter += 1
        else:
            key = _section_key(stripped)
            item_index = None
        start, start_line = pending if pending is not None else (line.start, line.index)
        current = _Section(
            start=start,
            start_line=start_line,
            content_end=line.content_end,
            key=key,
            item_index=item_index,
            indents={indent},
        )
        pending = None
    if current is not None:
        sections.append(current)
    return sections


def _clip_lines(raw: str, limit: int) -> str:
    """Prefix of *raw* cut at a line boundary (mid-line only if one is huge)."""
    lines = raw.split("\n")
    kept = lines[0]
    if len(kept) > limit:
        return kept[:limit]
    index = 1
    while index < len(lines):
        candidate = f"{kept}\n{lines[index]}"
        if len(candidate) > limit:
            break
        kept = candidate
        index += 1
    return kept


def _section_blocks(
    text: str, sections: list[_Section], limits: ParserLimits
) -> tuple[list[ParseBlock], list[str], bool]:
    blocks: list[ParseBlock] = []
    warnings: list[str] = []
    truncated = False
    warned: set[str] = set()
    for section in sections:
        if len(blocks) >= limits.max_values_indexed:
            if "value_index_truncated" not in warned:
                warned.add("value_index_truncated")
                warnings.append("value_index_truncated")
            truncated = True
            break
        if len(blocks) >= limits.max_blocks:
            if "block_limit_reached" not in warned:
                warned.add("block_limit_reached")
                warnings.append("block_limit_reached")
            truncated = True
            break
        raw = text[section.start : section.content_end].rstrip()
        if not raw:
            continue
        if len(raw) > limits.max_block_chars:
            raw = _clip_lines(raw, limits.max_block_chars)
            if "block_chars_truncated" not in warned:
                warned.add("block_chars_truncated")
                warnings.append("block_chars_truncated")
            truncated = True
        detail: dict[str, Any] = {}
        levels = len(section.indents)
        if levels - 1 > limits.max_depth:
            # Nested detail beyond the depth budget stays summarized, never
            # expanded into deeper blocks.
            detail = {"depth_summarized": True, "indent_levels": levels}
            if "depth_summarized" not in warned:
                warned.add("depth_summarized")
                warnings.append("depth_summarized")
        if section.key is not None:
            pointer: str | None = f"/{_escape_token(section.key)}"
            heading: tuple[str, ...] = (section.key,)
        elif section.item_index is not None:
            pointer = f"/{section.item_index}"
            heading = ()
        else:
            pointer = None
            heading = ()
        line_start = section.start_line + 1
        blocks.append(
            ParseBlock(
                block_kind="section",
                text=raw,
                span=Span(
                    start=section.start,
                    end=section.start + len(raw),
                    line_start=line_start,
                    line_end=line_start + raw.count("\n"),
                    json_pointer=pointer,
                    detail=detail,
                ),
                heading_path=heading,
            )
        )
    return blocks, warnings, truncated


class YamlParser:
    """safe_load-validated YAML with verbatim top-level section blocks."""

    parser_id = PARSER_ID
    format_ids = ("yaml",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        try:
            import yaml
        except ImportError:
            return ParseResult(
                outcome=Outcome.EXECUTOR_NOT_ACTIVATED,
                executor=self.parser_id,
                required_role="doc",
                detail="PyYAML unavailable: the doc executor role serves yaml",
            )
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
                detail="document contains no YAML content",
            )
        try:
            document = yaml.safe_load(text)
        except RecursionError:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail="document nesting exceeds the recursion budget",
            )
        except yaml.YAMLError as exc:
            if isinstance(exc, yaml.constructor.ConstructorError):
                # Unknown or object-constructing tag: refused, never built.
                return ParseResult(
                    outcome=Outcome.INPUT_REJECTED,
                    executor=self.parser_id,
                    detail=f"unsafe_yaml_tag: {exc}"[:_DETAIL_LIMIT],
                )
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                detail=str(exc)[:_DETAIL_LIMIT],
            )
        if document is None:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                detail="document contains no YAML content",
            )
        sections = _collect_sections(_scan_lines(text))
        blocks, warnings, truncated = _section_blocks(text, sections, limits)
        if not blocks:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                warnings=tuple(warnings),
                detail="document contains no YAML content",
            )
        return ParseResult(
            outcome=Outcome.OK,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
            truncated=truncated,
            executor=self.parser_id,
        )


PARSERS = {"yaml": YamlParser()}
