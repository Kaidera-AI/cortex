"""OOXML, OpenDocument, EPUB and ZIP-refinement adapters for the doc role.

These parsers read the preserved original bytes through stdlib ``zipfile``
with hard per-member bounds (zip-slip names, compressed size and the declared
decompression ratio) and parse every XML part through ``defusedxml``, which is
imported inside the parse call so a worker image without the doc-role
dependency still boots and answers with a typed ``executor_not_activated``.
Spans index the parser's own extracted text stream; the exact original
location is carried by the part/sheet/cell/slide/row locators plus ``detail``
(R12). Macros are never read, formulas are never evaluated and scripts are
never executed.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from types import ModuleType
from typing import Any
from urllib.parse import unquote

from ..contracts import (
    EXECUTOR_DOC,
    Outcome,
    ParseBlock,
    ParseResult,
    ParserLimits,
    SourceRef,
    Span,
)
from ..formats import FORMATS, ZIP_CONTAINER

PARSER_OOXML = FORMATS["docx"].parser_id
PARSER_OPENDOCUMENT = FORMATS["odt"].parser_id
PARSER_EPUB = FORMATS["epub"].parser_id
PARSER_ZIP = FORMATS[ZIP_CONTAINER].parser_id

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_XL = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_NS_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_NS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_NS_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
_NS_OPF = "http://www.idpf.org/2007/opf"

WARNING_MACROS_IGNORED = "macros_ignored"
WARNING_SCRIPT_SKIPPED = "script_content_skipped"
WARNING_BLOCK_TRUNCATED = "block_text_truncated"
WARNING_BLOCKS_TRUNCATED = "max_blocks_exceeded"
WARNING_ROWS_TRUNCATED = "max_rows_exceeded"

DETAIL_ZIP_SLIP = "zip_slip"
DETAIL_RATIO_EXCEEDED = "decompression_ratio_exceeded"
DETAIL_INPUT_BYTES_EXCEEDED = "input_bytes_exceeded"
DETAIL_UNRECOGNIZED_ZIP = "unrecognized_zip_container"

_DEPENDENCY_DETAIL = "defusedxml unavailable in this worker image"
_BYTES_DETAIL = "the referenced original bytes could not be resolved"

#: Spreadsheet column ceiling (Excel XFD) keeps row assembly bounded (N04).
_MAX_ROW_COLUMNS = 16_384

_HEADING_STYLE = re.compile(r"heading\s*([1-9])", re.IGNORECASE)
_CELL_REFERENCE = re.compile(r"([A-Za-z]+)")
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _problem(
    outcome: Outcome,
    parser_id: str,
    *,
    detail: str | None = None,
    required_role: str | None = None,
    warnings: tuple[str, ...] = (),
    stats: dict[str, int] | None = None,
) -> ParseResult:
    return ParseResult(
        outcome=outcome,
        warnings=warnings,
        executor=parser_id,
        required_role=required_role,
        detail=detail,
        stats=dict(stats or {}),
    )


def _xml_tree(parser_id: str) -> tuple[ModuleType | None, ParseResult | None]:
    """Import the doc-role XML parser lazily; absence is a typed outcome."""
    try:
        from defusedxml import ElementTree as tree
    except ImportError:
        return None, _problem(
            Outcome.EXECUTOR_NOT_ACTIVATED,
            parser_id,
            required_role=EXECUTOR_DOC,
            detail=_DEPENDENCY_DETAIL,
        )
    return tree, None


def _is_unsafe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_LETTER.match(normalized):
        return True
    return any(part == ".." for part in normalized.split("/"))


def _open_container(
    source: SourceRef, limits: ParserLimits, parser_id: str
) -> tuple[zipfile.ZipFile | None, ParseResult | None]:
    """Open the original bytes as a ZIP under the declared quotas (N04)."""
    data = source.body_bytes
    if data is None:
        return None, _problem(
            Outcome.SOURCE_BYTES_UNAVAILABLE,
            parser_id,
            required_role=EXECUTOR_DOC,
            detail=_BYTES_DETAIL,
        )
    if len(data) > limits.max_input_bytes:
        return None, _problem(
            Outcome.QUOTA_EXCEEDED,
            parser_id,
            detail=DETAIL_INPUT_BYTES_EXCEEDED,
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        return None, _problem(
            Outcome.INPUT_CORRUPT, parser_id, detail="bad_zip_container"
        )
    total_declared = 0
    total_compressed = 0
    for info in archive.infolist():
        if _is_unsafe_member(info.filename):
            archive.close()
            return None, _problem(
                Outcome.INPUT_REJECTED, parser_id, detail=DETAIL_ZIP_SLIP
            )
        declared = max(info.file_size, 0)
        compressed = max(info.compress_size, 0)
        total_declared += declared
        total_compressed += compressed
        member_ratio = declared / max(compressed, 1)
        if declared and member_ratio > limits.max_decompression_ratio:
            archive.close()
            return None, _problem(
                Outcome.QUOTA_EXCEEDED,
                parser_id,
                detail=DETAIL_RATIO_EXCEEDED,
            )
    total_ratio = total_declared / max(total_compressed, 1)
    if total_declared and total_ratio > limits.max_decompression_ratio:
        archive.close()
        return None, _problem(
            Outcome.QUOTA_EXCEEDED, parser_id, detail=DETAIL_RATIO_EXCEEDED
        )
    return archive, None


@dataclass(frozen=True, slots=True)
class _Context:
    """One opened, bounds-checked ZIP plus its parse configuration."""

    archive: zipfile.ZipFile
    tree: ModuleType
    limits: ParserLimits
    parser_id: str


def _read(
    context: _Context, name: str, ceiling: int | None = None
) -> tuple[bytes | None, ParseResult | None]:
    limit = context.limits.max_input_bytes if ceiling is None else ceiling
    if _is_unsafe_member(name):
        return None, _problem(
            Outcome.INPUT_REJECTED, context.parser_id, detail=DETAIL_ZIP_SLIP
        )
    try:
        payload = context.archive.read(name)
    except KeyError:
        return None, _problem(
            Outcome.INPUT_CORRUPT,
            context.parser_id,
            detail="missing_zip_member",
        )
    except RuntimeError:
        return None, _problem(
            Outcome.INPUT_ENCRYPTED,
            context.parser_id,
            detail="encrypted_zip_member",
        )
    except zipfile.BadZipFile:
        return None, _problem(
            Outcome.INPUT_CORRUPT, context.parser_id, detail="bad_zip_member"
        )
    if len(payload) > limit:
        return None, _problem(
            Outcome.QUOTA_EXCEEDED,
            context.parser_id,
            detail=DETAIL_INPUT_BYTES_EXCEEDED,
        )
    return payload, None


def _xml_part(context: _Context, name: str) -> tuple[Any | None, ParseResult | None]:
    payload, problem = _read(context, name)
    if problem is not None:
        return None, problem
    try:
        return context.tree.fromstring(payload), None
    except RecursionError:
        return None, _problem(
            Outcome.QUOTA_EXCEEDED,
            context.parser_id,
            detail="xml_nesting_too_deep",
        )
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return None, _problem(
            Outcome.INPUT_CORRUPT,
            context.parser_id,
            detail="malformed_xml_part",
        )


def _resolve_part(base: str, target: str) -> str:
    cleaned = unquote(target)
    if cleaned.startswith("/"):
        return posixpath.normpath(cleaned.lstrip("/"))
    return posixpath.normpath(posixpath.join(base, cleaned))


def _relationship_targets(root: Any) -> dict[str, str]:
    targets: dict[str, str] = {}
    for relation in root.iter(_tag(_NS_PKG_REL, "Relationship")):
        identifier = relation.get("Id")
        target = relation.get("Target")
        if identifier and target:
            targets[identifier] = target
    return targets


def _heading_level(style: str | None) -> int:
    if not style:
        return 0
    match = _HEADING_STYLE.fullmatch(style.strip())
    return int(match.group(1)) if match else 0


def _column_index(reference: str) -> int:
    match = _CELL_REFERENCE.match(reference)
    if match is None:
        return 0
    index = 0
    for char in match.group(1).upper():
        index = index * 26 + (ord(char) - 64)
    return index


def _column_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters or "A"


def _int_attribute(element: Any, name: str, default: int) -> int:
    raw = element.get(name) or ""
    return int(raw) if raw.isdigit() else default


def _macro_warnings(names: list[str]) -> list[str]:
    for name in names:
        if name.rsplit("/", 1)[-1] == "vbaProject.bin":
            return [WARNING_MACROS_IGNORED]
    return []


def _assemble_row(
    entries: list[tuple[int, str, str | None]], row_number: int
) -> tuple[str, dict[str, str], str | None]:
    """Row text, per-cell formulas and the cell-range locator for one row."""
    width = min(
        max((column for column, _, _ in entries), default=0), _MAX_ROW_COLUMNS
    )
    slots = [""] * width
    formulas: dict[str, str] = {}
    meaningful: list[int] = []
    for column, text, formula in entries:
        if not 1 <= column <= width:
            continue
        if formula is not None:
            formulas[f"{_column_letter(column)}{row_number}"] = formula
            meaningful.append(column)
        elif text:
            slots[column - 1] = text.replace("\t", " ").replace("\n", " ")
            meaningful.append(column)
    if not meaningful:
        return "", {}, None
    text = "\t".join(slots).rstrip("\t")
    first, last = min(meaningful), max(meaningful)
    cell = f"{_column_letter(first)}{row_number}"
    if last != first:
        cell = f"{cell}:{_column_letter(last)}{row_number}"
    return text, formulas, cell


class _TextStream:
    """The parser's own extracted text stream that binary spans index."""

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


class _BlockBuilder:
    """Bounded block accumulation with explicit truncation (N04)."""

    __slots__ = (
        "blocks",
        "full",
        "limits",
        "stream",
        "truncated",
        "warnings",
    )

    def __init__(self, limits: ParserLimits, warnings: list[str]) -> None:
        self.limits = limits
        self.warnings = warnings
        self.stream = _TextStream()
        self.blocks: list[ParseBlock] = []
        self.full = False
        self.truncated = False

    def warn_once(self, warning: str) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)

    def mark_truncated(self, warning: str) -> None:
        self.full = True
        self.truncated = True
        self.warn_once(warning)

    def add(
        self,
        text: str,
        block_kind: str,
        *,
        heading_path: Iterable[str] = (),
        part: str | None = None,
        page: int | None = None,
        sheet: str | None = None,
        cell: str | None = None,
        slide: int | None = None,
        row: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if self.full:
            return
        if len(self.blocks) >= self.limits.max_blocks:
            self.mark_truncated(WARNING_BLOCKS_TRUNCATED)
            return
        if len(text) > self.limits.max_block_chars:
            text = text[: self.limits.max_block_chars]
            self.truncated = True
            self.warn_once(WARNING_BLOCK_TRUNCATED)
        start, end, line_start, line_end = self.stream.add(text)
        self.blocks.append(
            ParseBlock(
                block_kind=block_kind,
                text=text,
                span=Span(
                    start=start,
                    end=end,
                    line_start=line_start,
                    line_end=line_end,
                    page=page,
                    sheet=sheet,
                    cell=cell,
                    slide=slide,
                    row=row,
                    part=part,
                    detail=dict(detail) if detail else {},
                ),
                heading_path=tuple(heading_path),
            )
        )


def _finish(parser_id: str, builder: _BlockBuilder) -> ParseResult:
    warnings = tuple(dict.fromkeys(builder.warnings))
    stats = {"blocks": len(builder.blocks)}
    if not builder.blocks:
        return ParseResult(
            outcome=Outcome.INPUT_EMPTY,
            warnings=warnings,
            executor=parser_id,
            stats=stats,
        )
    return ParseResult(
        outcome=Outcome.OK,
        blocks=tuple(builder.blocks),
        warnings=warnings,
        truncated=builder.truncated,
        executor=parser_id,
        stats=stats,
    )


def _docx_blocks(
    context: _Context, warnings: list[str]
) -> tuple[_BlockBuilder | None, ParseResult | None]:
    root, problem = _xml_part(context, "word/document.xml")
    if problem is not None:
        return None, problem
    builder = _BlockBuilder(context.limits, warnings)
    run_text = _tag(_NS_W, "t")
    style_path = f"{_tag(_NS_W, 'pPr')}/{_tag(_NS_W, 'pStyle')}"
    stack: list[str] = []
    for index, paragraph in enumerate(root.iter(_tag(_NS_W, "p"))):
        if builder.full:
            break
        text = "".join(node.text or "" for node in paragraph.iter(run_text))
        style = paragraph.find(style_path)
        level = _heading_level(
            style.get(_tag(_NS_W, "val")) if style is not None else None
        )
        if level:
            del stack[level - 1 :]
            stack.append(text.strip())
        if not text.strip():
            continue
        builder.add(
            text,
            "heading" if level else "paragraph",
            heading_path=[item for item in stack if item],
            part="word/document.xml",
            detail={"paragraph_index": index},
        )
    return builder, None


def _pptx_blocks(
    context: _Context, warnings: list[str]
) -> tuple[_BlockBuilder | None, ParseResult | None]:
    presentation, problem = _xml_part(context, "ppt/presentation.xml")
    if problem is not None:
        return None, problem
    relations, problem = _xml_part(context, "ppt/_rels/presentation.xml.rels")
    if problem is not None:
        return None, problem
    targets = _relationship_targets(relations)
    builder = _BlockBuilder(context.limits, warnings)
    run_text = _tag(_NS_A, "t")
    slide = 0
    for slide_id in presentation.iter(_tag(_NS_P, "sldId")):
        if builder.full:
            break
        slide += 1
        target = targets.get(slide_id.get(_tag(_NS_R, "id")) or "")
        if not target:
            return None, _problem(
                Outcome.INPUT_CORRUPT,
                context.parser_id,
                detail="unresolved_slide_relationship",
            )
        path = _resolve_part("ppt", target)
        root, problem = _xml_part(context, path)
        if problem is not None:
            return None, problem
        paragraphs: list[str] = []
        for paragraph in root.iter(_tag(_NS_A, "p")):
            raw = "".join(node.text or "" for node in paragraph.iter(run_text))
            text = " ".join(raw.split())
            if text:
                paragraphs.append(text)
        if paragraphs:
            builder.add(
                "\n".join(paragraphs), "slide_text", slide=slide, part=path
            )
    return builder, None


def _xlsx_cell_value(cell: Any, shared: list[str]) -> tuple[str, str | None]:
    formula_element = cell.find(_tag(_NS_XL, "f"))
    if formula_element is not None:
        # Formulas are reported as formulas; their cached result is never
        # presented as a value and never recomputed.
        return "", "".join(formula_element.itertext())
    kind = cell.get("t") or ""
    if kind == "inlineStr":
        inline = cell.find(_tag(_NS_XL, "is"))
        if inline is None:
            return "", None
        return (
            "".join(
                node.text or "" for node in inline.iter(_tag(_NS_XL, "t"))
            ),
            None,
        )
    value_element = cell.find(_tag(_NS_XL, "v"))
    raw = (value_element.text or "") if value_element is not None else ""
    if kind == "s":
        index = int(raw) if raw.isdigit() else -1
        return (shared[index] if 0 <= index < len(shared) else ""), None
    if kind == "b":
        boolean = {"1": "TRUE", "0": "FALSE"}.get(raw, raw)
        return boolean, None
    return raw, None


def _xlsx_blocks(
    context: _Context, warnings: list[str]
) -> tuple[_BlockBuilder | None, ParseResult | None]:
    workbook, problem = _xml_part(context, "xl/workbook.xml")
    if problem is not None:
        return None, problem
    relations, problem = _xml_part(context, "xl/_rels/workbook.xml.rels")
    if problem is not None:
        return None, problem
    targets = _relationship_targets(relations)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in context.archive.namelist():
        strings, problem = _xml_part(context, "xl/sharedStrings.xml")
        if problem is not None:
            return None, problem
        for item in strings.iter(_tag(_NS_XL, "si")):
            shared.append(
                "".join(
                    node.text or "" for node in item.iter(_tag(_NS_XL, "t"))
                )
            )
    builder = _BlockBuilder(context.limits, warnings)
    for sheet_element in workbook.iter(_tag(_NS_XL, "sheet")):
        if builder.full:
            break
        sheet_name = sheet_element.get("name") or ""
        target = targets.get(sheet_element.get(_tag(_NS_R, "id")) or "")
        if not target:
            return None, _problem(
                Outcome.INPUT_CORRUPT,
                context.parser_id,
                detail="unresolved_sheet_relationship",
            )
        path = _resolve_part("xl", target)
        sheet_root, problem = _xml_part(context, path)
        if problem is not None:
            return None, problem
        fallback_row = 0
        for row_element in sheet_root.iter(_tag(_NS_XL, "row")):
            if builder.full:
                break
            fallback_row += 1
            raw = row_element.get("r") or ""
            row_number = int(raw) if raw.isdigit() else fallback_row
            fallback_row = row_number
            entries: list[tuple[int, str, str | None]] = []
            column = 0
            for cell in row_element.findall(_tag(_NS_XL, "c")):
                column = _column_index(cell.get("r") or "") or column + 1
                text, formula = _xlsx_cell_value(cell, shared)
                entries.append((column, text, formula))
            text, formulas, cell_ref = _assemble_row(entries, row_number)
            if not text.strip() and not formulas:
                continue
            if len(builder.blocks) >= context.limits.max_rows:
                builder.mark_truncated(WARNING_ROWS_TRUNCATED)
                break
            builder.add(
                text,
                "row",
                sheet=sheet_name,
                row=row_number,
                cell=cell_ref,
                part=path,
                detail={"formula": formulas} if formulas else None,
            )
    return builder, None


_ODF_KINDS = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
}

_ZIP_MIMETYPES = {**_ODF_KINDS, "application/epub+zip": "epub"}

_OOXML_CONTENT_MARKERS = (
    ("wordprocessingml.document.main", "docx"),
    ("presentationml.presentation.main", "pptx"),
    ("spreadsheetml.sheet.main", "xlsx"),
)


def _odf_kind(context: _Context, root: Any) -> str | None:
    if "mimetype" in context.archive.namelist():
        payload, problem = _read(context, "mimetype", 4096)
        if problem is None:
            try:
                declared = payload.decode("utf-8").strip()
            except UnicodeDecodeError:
                declared = ""
            kind = _ODF_KINDS.get(declared)
            if kind:
                return kind
    declared = root.get(_tag(_NS_OFFICE, "mimetype")) or ""
    kind = _ODF_KINDS.get(declared)
    if kind:
        return kind
    for body, candidate in (
        ("spreadsheet", "ods"),
        ("presentation", "odp"),
        ("text", "odt"),
    ):
        if root.find(f".//{_tag(_NS_OFFICE, body)}") is not None:
            return candidate
    return None


def _odt_blocks(
    context: _Context, root: Any, warnings: list[str]
) -> tuple[_BlockBuilder | None, ParseResult | None]:
    builder = _BlockBuilder(context.limits, warnings)
    paragraph_tag = _tag(_NS_TEXT, "p")
    heading_tag = _tag(_NS_TEXT, "h")
    outline = _tag(_NS_TEXT, "outline-level")
    stack: list[str] = []
    index = 0
    for element in root.iter():
        if builder.full:
            break
        if element.tag == heading_tag:
            level = _int_attribute(element, outline, 1)
            kind = "heading"
        elif element.tag == paragraph_tag:
            level = 0
            kind = "paragraph"
        else:
            continue
        text = " ".join("".join(element.itertext()).split())
        current = index
        index += 1
        if level:
            del stack[level - 1 :]
            stack.append(text)
        if not text:
            continue
        builder.add(
            text,
            kind,
            heading_path=[item for item in stack if item],
            part="content.xml",
            detail={"paragraph_index": current},
        )
    return builder, None


def _odp_blocks(
    context: _Context, root: Any, warnings: list[str]
) -> tuple[_BlockBuilder | None, ParseResult | None]:
    builder = _BlockBuilder(context.limits, warnings)
    text_tags = (_tag(_NS_TEXT, "p"), _tag(_NS_TEXT, "h"))
    slide = 0
    for page in root.iter(_tag(_NS_DRAW, "page")):
        if builder.full:
            break
        slide += 1
        paragraphs: list[str] = []
        for element in page.iter():
            if element.tag in text_tags:
                text = " ".join("".join(element.itertext()).split())
                if text:
                    paragraphs.append(text)
        if not paragraphs:
            continue
        name = page.get(_tag(_NS_DRAW, "name"))
        builder.add(
            "\n".join(paragraphs),
            "slide_text",
            slide=slide,
            part="content.xml",
            detail={"page_name": name} if name else None,
        )
    return builder, None


def _ods_cell_text(cell: Any) -> str:
    paragraphs: list[str] = []
    for paragraph in cell.iter(_tag(_NS_TEXT, "p")):
        text = " ".join("".join(paragraph.itertext()).split())
        if text:
            paragraphs.append(text)
    if paragraphs:
        return "\n".join(paragraphs)
    value_type = cell.get(_tag(_NS_OFFICE, "value-type")) or ""
    attribute = {
        "float": "value",
        "percentage": "value",
        "currency": "value",
        "date": "date-value",
        "time": "time-value",
        "boolean": "boolean-value",
    }.get(value_type)
    if attribute:
        return cell.get(_tag(_NS_OFFICE, attribute)) or ""
    return ""


def _ods_row_entries(row: Any) -> list[tuple[int, str, str | None]]:
    entries: list[tuple[int, str, str | None]] = []
    cell_tags = (
        _tag(_NS_TABLE, "table-cell"),
        _tag(_NS_TABLE, "covered-table-cell"),
    )
    column = 0
    for child in row:
        if child.tag not in cell_tags:
            continue
        repeat = min(
            _int_attribute(
                child, _tag(_NS_TABLE, "number-columns-repeated"), 1
            ),
            _MAX_ROW_COLUMNS,
        )
        formula = child.get(_tag(_NS_TABLE, "formula"))
        text = "" if formula is not None else _ods_cell_text(child)
        if not text and formula is None:
            column += repeat
            continue
        for _ in range(repeat):
            column += 1
            if column > _MAX_ROW_COLUMNS:
                break
            entries.append((column, text, formula))
    return entries


def _ods_blocks(
    context: _Context, root: Any, warnings: list[str]
) -> tuple[_BlockBuilder | None, ParseResult | None]:
    builder = _BlockBuilder(context.limits, warnings)
    for table in root.iter(_tag(_NS_TABLE, "table")):
        if builder.full:
            break
        sheet_name = table.get(_tag(_NS_TABLE, "name")) or ""
        row_number = 0
        for row in table.iter(_tag(_NS_TABLE, "table-row")):
            if builder.full:
                break
            row_number += 1
            entries = _ods_row_entries(row)
            text, formulas, cell_ref = _assemble_row(entries, row_number)
            repeated = _int_attribute(
                row, _tag(_NS_TABLE, "number-rows-repeated"), 1
            )
            if text.strip() or formulas:
                if len(builder.blocks) >= context.limits.max_rows:
                    builder.mark_truncated(WARNING_ROWS_TRUNCATED)
                    break
                builder.add(
                    text,
                    "row",
                    sheet=sheet_name,
                    row=row_number,
                    cell=cell_ref,
                    part="content.xml",
                    detail={"formula": formulas} if formulas else None,
                )
            row_number += max(repeated - 1, 0)
    return builder, None


_INERT_TAGS = frozenset({"head", "script", "style"})
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


class _XhtmlText(HTMLParser):
    """Bounded stdlib HTML walk: scripts, styles and head stay inert."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str, tuple[str, ...]]] = []
        self.script_skipped = False
        self._buffer: list[str] = []
        self._skip: list[str] = []
        self._headings: list[str] = []
        self._heading_level = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in _INERT_TAGS:
            self._skip.append(tag)
            if tag == "script":
                self.script_skipped = True
            return
        if self._skip:
            return
        if tag == "br":
            self._buffer.append(" ")
            return
        level = _HEADING_TAGS.get(tag, 0)
        if level:
            self._flush()
            self._heading_level = level
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            elif tag in self._skip:
                while self._skip and self._skip.pop() != tag:
                    pass
            return
        if tag in _HEADING_TAGS or tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        text = " ".join("".join(self._buffer).split())
        self._buffer = []
        level = self._heading_level
        self._heading_level = 0
        if not text:
            return
        if level:
            del self._headings[level - 1 :]
            self._headings.append(text)
        path = tuple(item for item in self._headings if item)
        self.blocks.append(("heading" if level else "paragraph", text, path))


def _opf_path(context: _Context) -> tuple[str | None, ParseResult | None]:
    root, problem = _xml_part(context, "META-INF/container.xml")
    if problem is not None:
        return None, problem
    for element in root.iter():
        path = element.get("full-path")
        if path:
            return path, None
    return None, _problem(
        Outcome.INPUT_CORRUPT, context.parser_id, detail="missing_rootfile"
    )


def _is_xhtml(href: str, media_type: str) -> bool:
    if media_type in ("application/xhtml+xml", "text/html"):
        return True
    return href.lower().endswith((".xhtml", ".html", ".htm"))


class OoxmlParser:
    """Word/PowerPoint/Excel OOXML packages, selected by package shape."""

    parser_id = PARSER_OOXML
    format_ids = ("docx", "pptx", "xlsx")

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        tree, problem = _xml_tree(self.parser_id)
        if problem is not None:
            return problem
        archive, problem = _open_container(source, limits, self.parser_id)
        if problem is not None:
            return problem
        with archive:
            context = _Context(archive, tree, limits, self.parser_id)
            names = archive.namelist()
            warnings = _macro_warnings(names)
            if "word/document.xml" in names:
                builder, problem = _docx_blocks(context, warnings)
            elif "ppt/presentation.xml" in names:
                builder, problem = _pptx_blocks(context, warnings)
            elif "xl/workbook.xml" in names:
                builder, problem = _xlsx_blocks(context, warnings)
            else:
                return _problem(
                    Outcome.UNSUPPORTED_FORMAT,
                    self.parser_id,
                    detail=DETAIL_UNRECOGNIZED_ZIP,
                    warnings=tuple(warnings),
                )
            if problem is not None:
                return problem
            return _finish(self.parser_id, builder)


class OpenDocumentParser:
    """ODF text/presentation/spreadsheet, selected by mimetype or body."""

    parser_id = PARSER_OPENDOCUMENT
    format_ids = ("odt", "odp", "ods")

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        tree, problem = _xml_tree(self.parser_id)
        if problem is not None:
            return problem
        archive, problem = _open_container(source, limits, self.parser_id)
        if problem is not None:
            return problem
        with archive:
            context = _Context(archive, tree, limits, self.parser_id)
            warnings = _macro_warnings(archive.namelist())
            if "content.xml" not in archive.namelist():
                return _problem(
                    Outcome.UNSUPPORTED_FORMAT,
                    self.parser_id,
                    detail=DETAIL_UNRECOGNIZED_ZIP,
                    warnings=tuple(warnings),
                )
            root, problem = _xml_part(context, "content.xml")
            if problem is not None:
                return problem
            kind = _odf_kind(context, root)
            if kind == "odt":
                builder, problem = _odt_blocks(context, root, warnings)
            elif kind == "odp":
                builder, problem = _odp_blocks(context, root, warnings)
            elif kind == "ods":
                builder, problem = _ods_blocks(context, root, warnings)
            else:
                return _problem(
                    Outcome.UNSUPPORTED_FORMAT,
                    self.parser_id,
                    detail=DETAIL_UNRECOGNIZED_ZIP,
                    warnings=tuple(warnings),
                )
            if problem is not None:
                return problem
            return _finish(self.parser_id, builder)


class EpubParser:
    """EPUB: container -> OPF spine order -> XHTML via stdlib html.parser."""

    parser_id = PARSER_EPUB
    format_ids = ("epub",)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        tree, problem = _xml_tree(self.parser_id)
        if problem is not None:
            return problem
        archive, problem = _open_container(source, limits, self.parser_id)
        if problem is not None:
            return problem
        with archive:
            context = _Context(archive, tree, limits, self.parser_id)
            opf_path, problem = _opf_path(context)
            if problem is not None:
                return problem
            package, problem = _xml_part(context, opf_path or "")
            if problem is not None:
                return problem
            manifest: dict[str, tuple[str, str]] = {}
            for item in package.iter(_tag(_NS_OPF, "item")):
                identifier = item.get("id")
                if identifier:
                    manifest[identifier] = (
                        item.get("href") or "",
                        item.get("media-type") or "",
                    )
            builder = _BlockBuilder(limits, [])
            base = posixpath.dirname(opf_path or "")
            for itemref in package.iter(_tag(_NS_OPF, "itemref")):
                if builder.full:
                    break
                entry = manifest.get(itemref.get("idref") or "")
                if entry is None:
                    return _problem(
                        Outcome.INPUT_CORRUPT,
                        self.parser_id,
                        detail="unresolved_spine_item",
                    )
                href, media_type = entry
                if not _is_xhtml(href, media_type):
                    continue
                path = _resolve_part(base, href)
                payload, problem = _read(context, path)
                if problem is not None:
                    return problem
                try:
                    markup = payload.decode("utf-8")
                except UnicodeDecodeError:
                    return _problem(
                        Outcome.INPUT_CORRUPT,
                        self.parser_id,
                        detail="undecodable_part",
                    )
                extractor = _XhtmlText()
                try:
                    extractor.feed(markup)
                    extractor.close()
                except Exception:  # html.parser asserts on damaged markup
                    return _problem(
                        Outcome.INPUT_CORRUPT,
                        self.parser_id,
                        detail="malformed_xhtml_part",
                    )
                if extractor.script_skipped:
                    builder.warn_once(WARNING_SCRIPT_SKIPPED)
                for index, extracted in enumerate(extractor.blocks):
                    kind, text, heading_path = extracted
                    builder.add(
                        text,
                        kind,
                        heading_path=heading_path,
                        part=path,
                        detail={"paragraph_index": index},
                    )
            return _finish(self.parser_id, builder)


def _refine(context: _Context) -> tuple[str | None, ParseResult | None]:
    """Concrete format id behind a signature-only ZIP, when recognizable."""
    names = set(context.archive.namelist())
    if "mimetype" in names:
        payload, problem = _read(context, "mimetype", 4096)
        if problem is not None:
            return None, problem
        try:
            declared = payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            declared = ""
        kind = _ZIP_MIMETYPES.get(declared)
        if kind:
            return kind, None
    if "[Content_Types].xml" in names:
        root, problem = _xml_part(context, "[Content_Types].xml")
        if problem is not None:
            return None, problem
        for override in root.iter(_tag(_NS_CT, "Override")):
            content = (override.get("ContentType") or "").lower()
            for marker, kind in _OOXML_CONTENT_MARKERS:
                if marker in content:
                    return kind, None
    if "META-INF/container.xml" in names:
        root, problem = _xml_part(context, "META-INF/container.xml")
        if problem is not None:
            return None, problem
        for element in root.iter():
            if element.get("full-path"):
                return "epub", None
    return None, None


class ZipContainerParser:
    """Signature-only ZIP: refine to the concrete format, then delegate."""

    parser_id = PARSER_ZIP
    format_ids = (ZIP_CONTAINER,)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        tree, problem = _xml_tree(self.parser_id)
        if problem is not None:
            return problem
        archive, problem = _open_container(source, limits, self.parser_id)
        if problem is not None:
            return problem
        with archive:
            refined, problem = _refine(
                _Context(archive, tree, limits, self.parser_id)
            )
        if problem is not None:
            return problem
        if refined is None:
            return _problem(
                Outcome.UNSUPPORTED_FORMAT,
                self.parser_id,
                detail=DETAIL_UNRECOGNIZED_ZIP,
            )
        result = _CONCRETE_PARSERS[refined].parse(source, limits)
        if result.format_id is None:
            return replace(result, format_id=refined)
        return result


OOXML_PARSER = OoxmlParser()
OPENDOCUMENT_PARSER = OpenDocumentParser()
EPUB_PARSER = EpubParser()
ZIP_CONTAINER_PARSER = ZipContainerParser()

_CONCRETE_PARSERS: dict[str, Any] = {
    "docx": OOXML_PARSER,
    "pptx": OOXML_PARSER,
    "xlsx": OOXML_PARSER,
    "odt": OPENDOCUMENT_PARSER,
    "odp": OPENDOCUMENT_PARSER,
    "ods": OPENDOCUMENT_PARSER,
    "epub": EPUB_PARSER,
}

PARSERS: dict[str, Any] = {
    **_CONCRETE_PARSERS,
    ZIP_CONTAINER: ZIP_CONTAINER_PARSER,
}

DEPENDENCIES: dict[str, str] = {
    format_id: "defusedxml" for format_id in PARSERS
}
