"""Document format registry, signature detection and the discovery table.

The Document Processor role is multi-format: plain text, Markdown/MDX, HTML,
PDF, CSV/TSV, JSON/JSONL, YAML, XML, OOXML (DOCX/PPTX/XLSX), OpenDocument
(ODT/ODP/ODS), RTF, EPUB, EML/MBOX and source/code/config. Detection never
trusts a caller assertion alone — file signatures win over media type, media
type wins over extension, and a text sniff is only used when the bytes decode
as UTF-8. Unknown binary is reported as unknown binary; it is never laundered
into replacement-character text.

``FORMAT_TABLE`` is also the payload of the ``processing.doc.formats``
discovery operation, so what workers can select and what has been tested come
from one source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .contracts import EXECUTOR_AUDIO, EXECUTOR_CORE, EXECUTOR_DOC, EXECUTOR_IMAGE, EXECUTOR_VIDEO

STATUS_TESTED = "tested"
STATUS_DEPENDENCY = "optional_dependency"
STATUS_ROUTED = "routed_to_media_role"
STATUS_UNSUPPORTED = "unsupported"

#: Format ids whose bytes are a ZIP container needing inner refinement.
ZIP_CONTAINER = "zip_container"
UNKNOWN_BINARY = "unknown_binary"
UNKNOWN_TEXT = "unknown_text"
OLE_LEGACY = "ole_legacy"
ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class FormatSpec:
    """One row of the tested format table."""

    format_id: str
    label: str
    roles: tuple[str, ...]
    media_types: tuple[str, ...]
    extensions: tuple[str, ...]
    parser_id: str
    status: str
    provenance: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    limits: dict[str, int] = field(default_factory=dict)
    notes: str = ""
    signatures: tuple[bytes, ...] = ()
    refine: bool = False

    def as_discovery_row(self) -> dict[str, object]:
        return {
            "format_id": self.format_id,
            "label": self.label,
            "executor_roles": list(self.roles),
            "media_types": list(self.media_types),
            "extensions": list(self.extensions),
            "parser_id": self.parser_id or None,
            "status": self.status,
            "provenance": list(self.provenance),
            "dependencies": list(self.dependencies),
            "limits": dict(self.limits),
            "notes": self.notes,
        }


_TEXT_LIMITS = {"max_input_chars": 1_000_000, "max_blocks": 20_000, "max_block_chars": 8_192}
_TABLE_LIMITS = {"max_input_bytes": 32_000_000, "max_rows": 200_000, "max_block_chars": 8_192}
_ZIP_LIMITS = {
    "max_input_bytes": 64_000_000,
    "max_decompression_ratio": 64,
    "max_blocks": 20_000,
    "max_block_chars": 8_192,
}
_PDF_LIMITS = {"max_input_bytes": 64_000_000, "max_pages": 2_000, "max_block_chars": 8_192}
_MAIL_LIMITS = {
    "max_input_bytes": 64_000_000,
    "max_attachments": 32,
    "max_blocks": 20_000,
    "max_block_chars": 8_192,
}

FORMAT_TABLE: tuple[FormatSpec, ...] = (
    FormatSpec(
        format_id="text",
        label="Plain text",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("text/plain",),
        extensions=(".txt", ".text", ".log", ".rst"),
        parser_id="parser.text@1",
        status=STATUS_TESTED,
        provenance=("offset", "line"),
        limits=dict(_TEXT_LIMITS),
        notes="Paragraph/line blocks with exact character offsets and 1-based lines.",
    ),
    FormatSpec(
        format_id="markdown",
        label="Markdown and MDX",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("text/markdown", "text/x-markdown", "text/mdx"),
        extensions=(".md", ".markdown", ".mdx", ".mdown"),
        parser_id="parser.markdown@1",
        status=STATUS_TESTED,
        provenance=("offset", "line", "heading"),
        limits=dict(_TEXT_LIMITS),
        notes="Structural blocks with heading paths. MDX/JSX is inert text: never executed.",
    ),
    FormatSpec(
        format_id="html",
        label="HTML and HTM",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("text/html", "application/xhtml+xml"),
        extensions=(".html", ".htm", ".xhtml"),
        parser_id="parser.html@1",
        status=STATUS_TESTED,
        provenance=("offset", "line"),
        limits=dict(_TEXT_LIMITS),
        notes=(
            "stdlib html.parser only: no scripts, no rendering, no fetching, no "
            "entity expansion beyond the stdlib's named entities."
        ),
    ),
    FormatSpec(
        format_id="csv",
        label="Comma-separated values",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("text/csv",),
        extensions=(".csv",),
        parser_id="parser.delimited@1",
        status=STATUS_TESTED,
        provenance=("offset", "line", "row", "cell"),
        limits=dict(_TABLE_LIMITS),
        notes="Verbatim rows, fixed dialect (comma, double-quote, CRLF/LF). No header guessing.",
    ),
    FormatSpec(
        format_id="tsv",
        label="Tab-separated values",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("text/tab-separated-values",),
        extensions=(".tsv", ".tab"),
        parser_id="parser.delimited@1",
        status=STATUS_TESTED,
        provenance=("offset", "line", "row", "cell"),
        limits=dict(_TABLE_LIMITS),
        notes="Verbatim rows, fixed tab dialect. No header guessing.",
    ),
    FormatSpec(
        format_id="json",
        label="JSON document",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("application/json",),
        extensions=(".json",),
        parser_id="parser.json@1",
        status=STATUS_TESTED,
        provenance=("offset", "json_pointer"),
        limits={**_TEXT_LIMITS, "max_values_indexed": 5_000, "max_depth": 64},
        notes="Verbatim values plus a bounded RFC 6901 value-location index.",
    ),
    FormatSpec(
        format_id="jsonl",
        label="JSON lines",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("application/x-ndjson", "application/jsonl"),
        extensions=(".jsonl", ".ndjson"),
        parser_id="parser.json@1",
        status=STATUS_TESTED,
        provenance=("offset", "line", "json_pointer"),
        limits={**_TEXT_LIMITS, "max_values_indexed": 5_000, "max_rows": 200_000},
        notes="One JSON value per line; a malformed line is reported, not silently dropped.",
    ),
    FormatSpec(
        format_id="source_code",
        label="Source code and configuration",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("text/x-source", "text/x-script", "application/toml"),
        extensions=(
            ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs", ".java",
            ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
            ".sql", ".sh", ".bash", ".zsh", ".ps1", ".toml", ".ini", ".cfg", ".conf",
            ".properties", ".dockerfile", ".gitignore", ".editorconfig",
        ),
        parser_id="parser.source_code@1",
        status=STATUS_TESTED,
        provenance=("offset", "line", "symbol"),
        limits=dict(_TEXT_LIMITS),
        notes=(
            "Line-oriented text with symbol-ish provenance from comments/definitions. "
            "Never executed, never imported, no toolchain invocation."
        ),
    ),
    FormatSpec(
        format_id="eml",
        label="RFC 822 email message",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("message/rfc822",),
        extensions=(".eml",),
        parser_id="parser.mail@1",
        status=STATUS_TESTED,
        provenance=("offset", "part", "line"),
        limits=dict(_MAIL_LIMITS),
        notes="stdlib email; headers, text parts and bounded attachment inventory.",
    ),
    FormatSpec(
        format_id="mbox",
        label="Mbox mailbox",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=("application/mbox",),
        extensions=(".mbox",),
        parser_id="parser.mail@1",
        status=STATUS_TESTED,
        provenance=("offset", "part", "line"),
        limits={**_MAIL_LIMITS, "max_rows": 20_000},
        notes="stdlib mailbox over bytes; per-message blocks with bounded attachments.",
    ),
    FormatSpec(
        format_id="xml",
        label="XML document",
        roles=(EXECUTOR_DOC,),
        media_types=("application/xml", "text/xml"),
        extensions=(".xml",),
        parser_id="parser.xml@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "line", "json_pointer"),
        dependencies=("defusedxml",),
        limits={**_TEXT_LIMITS, "max_depth": 64},
        notes="defusedxml only: no external entities, no DTD retrieval, no network includes.",
    ),
    FormatSpec(
        format_id="yaml",
        label="YAML document",
        roles=(EXECUTOR_DOC,),
        media_types=("application/yaml", "text/yaml"),
        extensions=(".yaml", ".yml"),
        parser_id="parser.yaml@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "line", "json_pointer"),
        dependencies=("PyYAML",),
        limits={**_TEXT_LIMITS, "max_depth": 64, "max_values_indexed": 5_000},
        notes="safe_load only: no custom tags, no object construction, no anchors bomb.",
    ),
    FormatSpec(
        format_id="rtf",
        label="Rich Text Format",
        roles=(EXECUTOR_DOC,),
        media_types=("application/rtf", "text/rtf"),
        extensions=(".rtf",),
        parser_id="parser.rtf@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "line"),
        dependencies=("striprtf",),
        limits=dict(_TEXT_LIMITS),
        notes="striprtf text extraction; embedded objects are inventoried, not executed.",
        signatures=(b"{\\rtf",),
    ),
    FormatSpec(
        format_id="pdf",
        label="Portable Document Format",
        roles=(EXECUTOR_DOC,),
        media_types=("application/pdf",),
        extensions=(".pdf",),
        parser_id="parser.pdf@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "page", "line"),
        dependencies=("pypdf",),
        limits=dict(_PDF_LIMITS),
        notes=(
            "Text layer plus page references; encrypted files report input_encrypted and "
            "pages without a text layer report ocr_needed instead of inventing content."
        ),
        signatures=(b"%PDF-",),
    ),
    FormatSpec(
        format_id="docx",
        label="Word document (OOXML)",
        roles=(EXECUTOR_DOC,),
        media_types=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        extensions=(".docx",),
        parser_id="parser.ooxml@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "part", "line"),
        dependencies=("defusedxml",),
        limits=dict(_ZIP_LIMITS),
        notes="zipfile + defusedxml paragraphs in document order; macros never evaluated.",
    ),
    FormatSpec(
        format_id="pptx",
        label="PowerPoint presentation (OOXML)",
        roles=(EXECUTOR_DOC,),
        media_types=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        extensions=(".pptx",),
        parser_id="parser.ooxml@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "slide", "part"),
        dependencies=("defusedxml",),
        limits=dict(_ZIP_LIMITS),
        notes="zipfile + defusedxml slides in presentation order; formulas/macros never evaluated.",
    ),
    FormatSpec(
        format_id="xlsx",
        label="Excel workbook (OOXML)",
        roles=(EXECUTOR_DOC,),
        media_types=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        extensions=(".xlsx",),
        parser_id="parser.ooxml@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "sheet", "cell", "row"),
        dependencies=("defusedxml",),
        limits=dict(_ZIP_LIMITS),
        notes=(
            "zipfile + defusedxml sheet/cell structure with cached values only; formulas are "
            "reported as formulas and never evaluated."
        ),
    ),
    FormatSpec(
        format_id="odt",
        label="OpenDocument text",
        roles=(EXECUTOR_DOC,),
        media_types=("application/vnd.oasis.opendocument.text",),
        extensions=(".odt",),
        parser_id="parser.opendocument@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "part", "line"),
        dependencies=("defusedxml",),
        limits=dict(_ZIP_LIMITS),
        notes="zipfile + defusedxml content.xml paragraphs; macros never evaluated.",
    ),
    FormatSpec(
        format_id="odp",
        label="OpenDocument presentation",
        roles=(EXECUTOR_DOC,),
        media_types=("application/vnd.oasis.opendocument.presentation",),
        extensions=(".odp",),
        parser_id="parser.opendocument@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "slide", "part"),
        dependencies=("defusedxml",),
        limits=dict(_ZIP_LIMITS),
        notes="zipfile + defusedxml slide order; macros never evaluated.",
    ),
    FormatSpec(
        format_id="ods",
        label="OpenDocument spreadsheet",
        roles=(EXECUTOR_DOC,),
        media_types=("application/vnd.oasis.opendocument.spreadsheet",),
        extensions=(".ods",),
        parser_id="parser.opendocument@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "sheet", "cell", "row"),
        dependencies=("defusedxml",),
        limits=dict(_ZIP_LIMITS),
        notes="zipfile + defusedxml table/cell structure; formulas never evaluated.",
    ),
    FormatSpec(
        format_id="epub",
        label="EPUB book",
        roles=(EXECUTOR_DOC,),
        media_types=("application/epub+zip",),
        extensions=(".epub",),
        parser_id="parser.epub@1",
        status=STATUS_DEPENDENCY,
        provenance=("offset", "part", "line"),
        dependencies=("defusedxml",),
        limits=dict(_ZIP_LIMITS),
        notes="zip + spine-ordered XHTML via stdlib html.parser; scripts inert, no fetching.",
    ),
    FormatSpec(
        format_id=ZIP_CONTAINER,
        label="ZIP container needing refinement",
        roles=(EXECUTOR_DOC,),
        media_types=("application/zip",),
        extensions=(".zip",),
        parser_id="parser.zip@1",
        status=STATUS_DEPENDENCY,
        provenance=("part",),
        dependencies=("defusedxml",),
        limits=dict(_ZIP_LIMITS),
        refine=True,
        notes="Signature-only detection; the parser refines to docx/pptx/xlsx/od*/epub.",
        signatures=(b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ),
    FormatSpec(
        format_id=OLE_LEGACY,
        label="Legacy OLE2 compound document (DOC/XLS/PPT)",
        roles=(),
        media_types=("application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint"),
        extensions=(".doc", ".xls", ".ppt", ".msg"),
        parser_id="",
        status=STATUS_UNSUPPORTED,
        provenance=(),
        notes=(
            "Requires an external converter (for example LibreOffice) that is deliberately not "
            "in scope; reported as unsupported_format, never as empty text."
        ),
        signatures=(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ),
    FormatSpec(
        format_id=ARCHIVE,
        label="Compressed archive",
        roles=(),
        media_types=("application/gzip", "application/x-bzip2", "application/x-xz", "application/x-7z-compressed", "application/vnd.rar"),
        extensions=(".gz", ".bz2", ".xz", ".7z", ".rar", ".tar"),
        parser_id="",
        status=STATUS_UNSUPPORTED,
        provenance=(),
        limits={"max_decompression_ratio": 1},
        notes="Archives are not documents; decompression bombs are never expanded.",
        signatures=(
            b"\x1f\x8b",
            b"BZh",
            b"\xfd7zXZ\x00",
            b"7z\xbc\xaf\x27\x1c",
            b"Rar!\x1a\x07",
            b"ustar",
        ),
    ),
    FormatSpec(
        format_id="image",
        label="Image",
        roles=(EXECUTOR_IMAGE,),
        media_types=("image/png", "image/jpeg", "image/gif", "image/webp", "image/tiff", "image/bmp", "image/heic"),
        extensions=(".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic"),
        parser_id="",
        status=STATUS_ROUTED,
        provenance=(),
        notes="Routed to the optional image role; typed executor_not_activated when absent.",
        signatures=(
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"II*\x00",
            b"MM\x00*",
            b"BM",
        ),
    ),
    FormatSpec(
        format_id="audio",
        label="Audio",
        roles=(EXECUTOR_AUDIO,),
        media_types=("audio/mpeg", "audio/ogg", "audio/flac", "audio/wav", "audio/mp4", "audio/webm"),
        extensions=(".mp3", ".oga", ".ogg", ".flac", ".wav", ".m4a", ".weba"),
        parser_id="",
        status=STATUS_ROUTED,
        provenance=(),
        notes="Routed to the optional audio role; typed executor_not_activated when absent.",
        signatures=(b"ID3", b"OggS", b"fLaC", b"RIFF"),
    ),
    FormatSpec(
        format_id="video",
        label="Video",
        roles=(EXECUTOR_VIDEO,),
        media_types=("video/mp4", "video/webm", "video/x-matroska", "video/quicktime", "video/x-msvideo"),
        extensions=(".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi"),
        parser_id="",
        status=STATUS_ROUTED,
        provenance=(),
        notes="Routed to the optional video role; typed executor_not_activated when absent.",
        signatures=(b"\x1aE\xdf\xa3",),
    ),
    FormatSpec(
        format_id=UNKNOWN_BINARY,
        label="Unrecognized binary",
        roles=(),
        media_types=("application/octet-stream",),
        extensions=(),
        parser_id="",
        status=STATUS_UNSUPPORTED,
        provenance=(),
        notes="Never converted to replacement-character text; reported as unsupported_format.",
    ),
    FormatSpec(
        format_id=UNKNOWN_TEXT,
        label="Unrecognized text",
        roles=(EXECUTOR_CORE, EXECUTOR_DOC),
        media_types=(),
        extensions=(),
        parser_id="parser.text@1",
        status=STATUS_TESTED,
        provenance=("offset", "line"),
        notes="UTF-8 text with no better identification; parsed as plain text.",
    ),
)

FORMATS: dict[str, FormatSpec] = {spec.format_id: spec for spec in FORMAT_TABLE}

_MEDIA_TYPES: dict[str, FormatSpec] = {}
for _spec in FORMAT_TABLE:
    for _media_type in _spec.media_types:
        _MEDIA_TYPES.setdefault(_media_type.lower(), _spec)

_EXTENSIONS: dict[str, FormatSpec] = {}
for _spec in FORMAT_TABLE:
    for _extension in _spec.extensions:
        _EXTENSIONS.setdefault(_extension.lower(), _spec)

_SIGNATURES: tuple[tuple[bytes, int, FormatSpec], ...] = tuple(
    (signature, 0, spec)
    for spec in FORMAT_TABLE
    for signature in spec.signatures
)

#: MP4/MOV family: ``ftyp`` brand at offset 4.
_FTYP_OFFSET = 4
#: WEBP/AVI/WAVE: RIFF chunk name at offset 8.
_RIFF_OFFSET = 8


#: Markdown detection is deliberately precise: an ATX heading, a fenced code
#: block or an inline link. A setext-only or bullet-only document is ambiguous
#: with plain prose, and misreading prose as markdown buys nothing — the plain
#: text parser preserves every character verbatim either way.
_ATX_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+\S", re.MULTILINE)
_CODE_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})", re.MULTILINE)
_INLINE_LINK = re.compile(r"\[[^]\n]{1,256}\]\([^)\n]{1,512}\)")


def _looks_like_markdown(sample: str) -> bool:
    return bool(
        _ATX_HEADING.search(sample)
        or _CODE_FENCE.search(sample)
        or _INLINE_LINK.search(sample)
    )


@dataclass(frozen=True, slots=True)
class Detection:
    """Result of format detection with its evidence."""

    format_id: str | None
    detected_by: str
    spec: FormatSpec | None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.spec is not None


def _sniff_text(head: str) -> str | None:
    stripped = head.lstrip()
    if stripped.startswith("<?xml") or stripped.startswith("<!DOCTYPE") and "<" in stripped:
        return "xml" if stripped.startswith("<?xml") else "html"
    lowered = stripped[:512].lower()
    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        return "html"
    if stripped.startswith(("{", "[")) or (
        "\n" in stripped and stripped.rstrip().endswith(("}", "]"))
    ):
        return "json"
    first = stripped.splitlines()[0] if stripped.splitlines() else ""
    if first.startswith("From ") and "\n" in stripped:
        return "mbox"
    # Header block = the lines before the first blank line. Reading past it
    # would classify ordinary prose containing a colon as an email.
    header_block: list[str] = []
    for line in stripped.splitlines()[:32]:
        if not line.strip():
            break
        header_block.append(line)
    if (
        len(header_block) >= 2
        and all(
            ": " in line
            and line.split(": ", 1)[0].replace("-", "").replace("_", "").isalnum()
            for line in header_block[:4]
        )
        and any(
            line.lower().startswith(
                (
                    "from:",
                    "subject:",
                    "message-id:",
                    "date:",
                    "to:",
                    "received:",
                    "return-path:",
                    "mime-version:",
                )
            )
            for line in header_block[:8]
        )
    ):
        return "eml"
    if stripped.startswith("---\n") or stripped.startswith("%YAML"):
        return "yaml"
    return None


def detect_format(
    *,
    media_type: str | None = None,
    filename: str | None = None,
    head: bytes | None = None,
    body_text: str | None = None,
) -> Detection:
    """Identify a format by signature, then media type, then extension, then text sniff.

    ``head`` should be the first bytes of the original (64 bytes is enough for
    every signature below). When only text is available the sniff runs over it.
    """
    if head:
        if len(head) > _FTYP_OFFSET and head[_FTYP_OFFSET : _FTYP_OFFSET + 4] == b"ftyp":
            return Detection("video", "signature", FORMATS["video"])
        if len(head) > _RIFF_OFFSET + 4 and head[:4] == b"RIFF":
            chunk = head[_RIFF_OFFSET : _RIFF_OFFSET + 4]
            if chunk == b"WAVE":
                return Detection("audio", "signature", FORMATS["audio"])
            if chunk in (b"AVI ", b"WEBP"):
                format_id = "video" if chunk == b"AVI " else "image"
                return Detection(format_id, "signature", FORMATS[format_id])
        if head[4:8] == b"heic" or head[4:12] in (b"ftypheic", b"ftypmif1"):
            return Detection("image", "signature", FORMATS["image"])
        for signature, offset, spec in _SIGNATURES:
            if head[offset : offset + len(signature)] == signature:
                if spec.format_id == "audio" and signature == b"RIFF":
                    continue
                return Detection(spec.format_id, "signature", spec)
        if b"\x00" in head[:64]:
            return Detection(
                UNKNOWN_BINARY,
                "signature",
                FORMATS[UNKNOWN_BINARY],
                reason="binary content with no recognized signature",
            )

    if media_type:
        normalized = media_type.split(";", 1)[0].strip().lower()
        spec = _MEDIA_TYPES.get(normalized)
        if spec is not None:
            return Detection(spec.format_id, "media_type", spec)
        if normalized.startswith("text/"):
            return Detection("text", "media_type", FORMATS["text"])

    if filename:
        lowered = filename.lower()
        if lowered in ("dockerfile", "makefile", "license", "readme"):
            return Detection("source_code", "extension", FORMATS["source_code"])
        for extension, spec in _EXTENSIONS.items():
            if lowered.endswith(extension):
                return Detection(spec.format_id, "extension", spec)

    text = body_text if body_text is not None else None
    if text is None and head is not None:
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            text = None
    if text:
        sniffed = _sniff_text(text)
        if sniffed is not None and sniffed in FORMATS:
            return Detection(sniffed, "content", FORMATS[sniffed])
        if any(marker in text[:4096] for marker in ("```", "def ", "function ", "import ", "#!")):
            return Detection("source_code", "content", FORMATS["source_code"])
        if _looks_like_markdown(text[:4096]):
            return Detection("markdown", "content", FORMATS["markdown"])
        return Detection(UNKNOWN_TEXT, "content", FORMATS[UNKNOWN_TEXT])

    if head is not None:
        return Detection(
            UNKNOWN_BINARY,
            "signature",
            FORMATS[UNKNOWN_BINARY],
            reason="empty or non-UTF-8 content with no recognized signature",
        )
    return Detection(None, "none", None, reason="no media type, filename or bytes supplied")


def spec_for(format_id: str | None) -> FormatSpec | None:
    return FORMATS.get(format_id) if format_id else None


def roles_for(format_id: str | None) -> tuple[str, ...]:
    spec = spec_for(format_id)
    return spec.roles if spec else ()


def required_role_for(format_id: str | None) -> str:
    """Narrowest executor role a job needs for this format.

    ``core`` keeps a minimal deployment (API + embed worker) able to chunk text
    formats; ``doc`` is required only for formats that need the Document
    Processor's dependency set. Media formats name their optional role so the
    job is visibly waiting instead of silently failing (R12).
    """
    roles = roles_for(format_id)
    if not roles:
        return EXECUTOR_CORE
    for role in (EXECUTOR_CORE, EXECUTOR_DOC, EXECUTOR_IMAGE, EXECUTOR_AUDIO, EXECUTOR_VIDEO):
        if role in roles:
            return role
    return roles[0]


def discovery_table() -> dict[str, object]:
    """Payload for the ``processing.doc.formats`` discovery operation."""
    return {
        "formats": [spec.as_discovery_row() for spec in FORMAT_TABLE],
        "detection_precedence": ["signature", "media_type", "extension", "content"],
        "executor_roles": [EXECUTOR_CORE, EXECUTOR_DOC, EXECUTOR_IMAGE, EXECUTOR_AUDIO, EXECUTOR_VIDEO],
    }
