"""XML parser adapter (``parser.xml@1``).

Two passes, both refusing everything defusedxml refuses. First
``defusedxml.ElementTree.fromstring`` validates with ``forbid_dtd``,
``forbid_entities`` and ``forbid_external``: a DTD, entity or external
reference is a typed ``INPUT_REJECTED`` — no external entities, no DTD
retrieval, no network includes. Then, safe because DTDs and entities were
already refused, ``xml.parsers.expat`` re-reads the same UTF-8 buffer to
capture provenance: every element block carries exact character offsets
into ``SourceRef.body_text`` (byte indexes are mapped back through the
UTF-8 encoding), 1-based lines and its ``/catalog/book[2]/title`` element
path; ``block.text`` is the element's descendant text, which for a simple
leaf is the verbatim inner slice. Element nesting beyond ``max_depth`` is a
hard ``QUOTA_EXCEEDED``. defusedxml is imported inside ``parse``; its
absence is a typed ``EXECUTOR_NOT_ACTIVATED`` outcome for the ``doc`` role.
"""

from __future__ import annotations

import xml.parsers.expat
from xml.etree.ElementTree import ParseError

from ..contracts import (
    Outcome,
    ParseBlock,
    ParseResult,
    ParserLimits,
    SourceRef,
    Span,
)

PARSER_ID = "parser.xml@1"

#: Distribution the doc role must provide for this format.
DEPENDENCIES = {"xml": "defusedxml"}

_DETAIL_LIMIT = 512
_FORBIDDEN_POLICY = "no external entities, no DTD retrieval, no network includes"


class _DepthExceeded(Exception):
    """Internal signal: element nesting breached ``max_depth``."""


class _ElementNode:
    """One element's provenance and extracted text from the expat pass."""

    __slots__ = (
        "children",
        "end_byte",
        "end_line",
        "name",
        "segments",
        "start_byte",
        "start_line",
        "text",
    )

    def __init__(self, name: str, start_byte: int, start_line: int) -> None:
        self.name = name
        self.start_byte = start_byte
        self.start_line = start_line
        self.end_byte = start_byte
        self.end_line = start_line
        self.segments: list[str] = []
        self.children: list[_ElementNode] = []
        self.text = ""


class _ProvenancePass:
    """expat pass building the element tree with byte offsets and lines."""

    def __init__(self, encoded: bytes, max_depth: int) -> None:
        self._encoded = encoded
        self._max_depth = max_depth
        self.roots: list[_ElementNode] = []
        self._stack: list[_ElementNode] = []
        self._parser = xml.parsers.expat.ParserCreate()
        self._parser.StartElementHandler = self._start_element
        self._parser.EndElementHandler = self._end_element
        self._parser.CharacterDataHandler = self._characters

    def run(self) -> None:
        self._parser.Parse(self._encoded, True)

    def _start_element(self, name: str, attributes: dict[str, str]) -> None:
        depth = len(self._stack) + 1
        if depth > self._max_depth:
            raise _DepthExceeded(
                f"element nesting beyond max_depth={self._max_depth} at <{name}>"
            )
        self._stack.append(
            _ElementNode(
                name,
                self._parser.CurrentByteIndex,
                self._parser.CurrentLineNumber,
            )
        )

    def _characters(self, data: str) -> None:
        if self._stack:
            self._stack[-1].segments.append(data)

    def _end_element(self, name: str) -> None:
        node = self._stack.pop()
        cursor = self._parser.CurrentByteIndex
        # For a real end tag expat reports the '<' of "</name>"; for a
        # self-closing element it already reports the byte after the markup.
        if self._encoded[cursor : cursor + 1] == b"<":
            cursor = self._encoded.index(b">", cursor) + 1
        node.end_byte = cursor
        node.end_line = self._parser.CurrentLineNumber
        parts = [segment.strip() for segment in node.segments]
        node.text = "\n".join(part for part in parts if part)
        if self._stack:
            parent = self._stack[-1]
            parent.children.append(node)
            if node.text:
                parent.segments.append(node.text)
        else:
            self.roots.append(node)


def _byte_to_char_map(text: str, encoded: bytes) -> dict[int, int] | None:
    """UTF-8 byte offset -> character offset, or None when they coincide."""
    if len(encoded) == len(text):
        return None
    mapping: dict[int, int] = {}
    byte_position = 0
    for char_position, char in enumerate(text):
        mapping[byte_position] = char_position
        byte_position += len(char.encode("utf-8"))
    mapping[byte_position] = len(text)
    return mapping


class _Emitter:
    """Document-order element blocks, bounded by ``max_blocks``."""

    def __init__(self, limits: ParserLimits, mapping: dict[int, int] | None) -> None:
        self._limits = limits
        self._mapping = mapping
        self.blocks: list[ParseBlock] = []
        self.warnings: list[str] = []
        self.truncated = False
        self._warned: set[str] = set()

    def emit(self, roots: list[_ElementNode]) -> None:
        self._walk(roots, "", ())

    def _char(self, byte_index: int) -> int:
        if self._mapping is None:
            return byte_index
        return self._mapping[byte_index]

    def _warn(self, name: str) -> None:
        if name not in self._warned:
            self._warned.add(name)
            self.warnings.append(name)

    def _walk(
        self,
        nodes: list[_ElementNode],
        prefix: str,
        heading: tuple[str, ...],
    ) -> None:
        counts: dict[str, int] = {}
        for node in nodes:
            counts[node.name] = counts.get(node.name, 0) + 1
        positions: dict[str, int] = {}
        for node in nodes:
            if len(self.blocks) >= self._limits.max_blocks:
                self.truncated = True
                self._warn("block_limit_reached")
                return
            position = positions[node.name] = positions.get(node.name, 0) + 1
            step = node.name if counts[node.name] == 1 else f"{node.name}[{position}]"
            path = f"{prefix}/{step}"
            if node.text:
                self.blocks.append(self._block(node, path, heading))
            self._walk(node.children, path, (*heading, node.name))

    def _block(
        self, node: _ElementNode, path: str, heading: tuple[str, ...]
    ) -> ParseBlock:
        text = node.text
        if len(text) > self._limits.max_block_chars:
            text = text[: self._limits.max_block_chars]
            self.truncated = True
            self._warn("block_chars_truncated")
        return ParseBlock(
            block_kind="element",
            text=text,
            span=Span(
                start=self._char(node.start_byte),
                end=self._char(node.end_byte),
                line_start=node.start_line,
                line_end=node.end_line,
                part=path,
            ),
            heading_path=heading,
        )


class XmlParser:
    """defusedxml-gated XML extractor with expat-captured element provenance."""

    parser_id = PARSER_ID
    format_ids = ("xml",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        try:
            from defusedxml import ElementTree as DefusedElementTree
            from defusedxml.common import (
                DTDForbidden,
                EntitiesForbidden,
                ExternalReferenceForbidden,
            )
        except ImportError:
            return ParseResult(
                outcome=Outcome.EXECUTOR_NOT_ACTIVATED,
                executor=self.parser_id,
                required_role="doc",
                detail="defusedxml unavailable: the doc executor role serves xml",
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
                detail="document contains no XML content",
            )
        try:
            DefusedElementTree.fromstring(
                text,
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
        except DTDForbidden:
            return self._rejected(f"forbidden_dtd: {_FORBIDDEN_POLICY}")
        except EntitiesForbidden:
            return self._rejected(f"forbidden_entities: {_FORBIDDEN_POLICY}")
        except ExternalReferenceForbidden:
            return self._rejected(
                f"forbidden_external_reference: {_FORBIDDEN_POLICY}"
            )
        except ParseError as exc:
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                detail=str(exc)[:_DETAIL_LIMIT],
            )
        except RecursionError:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail="document nesting exceeds the recursion budget",
            )
        encoded = text.encode("utf-8")
        provenance = _ProvenancePass(encoded, limits.max_depth)
        try:
            provenance.run()
        except _DepthExceeded as exc:
            return ParseResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                executor=self.parser_id,
                detail=str(exc)[:_DETAIL_LIMIT],
            )
        except (xml.parsers.expat.ExpatError, ValueError, IndexError) as exc:
            # Defensive: the document was already validated above.
            return ParseResult(
                outcome=Outcome.INPUT_CORRUPT,
                executor=self.parser_id,
                detail=f"{type(exc).__name__}: {exc}"[:_DETAIL_LIMIT],
            )
        emitter = _Emitter(limits, _byte_to_char_map(text, encoded))
        emitter.emit(provenance.roots)
        if not emitter.blocks:
            return ParseResult(
                outcome=Outcome.INPUT_EMPTY,
                executor=self.parser_id,
                detail="document contains no element text",
            )
        return ParseResult(
            outcome=Outcome.OK,
            blocks=tuple(emitter.blocks),
            warnings=tuple(emitter.warnings),
            truncated=emitter.truncated,
            executor=self.parser_id,
        )

    def _rejected(self, detail: str) -> ParseResult:
        return ParseResult(
            outcome=Outcome.INPUT_REJECTED,
            executor=self.parser_id,
            detail=detail[:_DETAIL_LIMIT],
        )


PARSERS = {"xml": XmlParser()}
