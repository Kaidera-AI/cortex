"""Typed routing outcomes for binary formats this worker never decodes.

Legacy OLE2 compound documents, compressed archives and unrecognized binary
payloads have no executor in this image; image/audio/video formats belong to
the optional media roles. Every answer is a typed outcome carrying the format
table's own note or the name of the missing optional role — bytes are never
converted to replacement-character text and never presented as empty text
(R12/F06).
"""

from __future__ import annotations

from ..contracts import Outcome, ParseResult, ParserLimits, SourceRef
from ..formats import (
    ARCHIVE,
    FORMATS,
    OLE_LEGACY,
    UNKNOWN_BINARY,
    required_role_for,
)

#: The format table pins no parser_id for routed formats; this adapter names
#: itself so persisted results still identify their extractor revision.
PARSER_ID = "parser.routed@1"

UNSUPPORTED_FORMAT_IDS = (OLE_LEGACY, ARCHIVE, UNKNOWN_BINARY)
MEDIA_FORMAT_IDS = ("image", "audio", "video")


class RoutedBinaryParser:
    """Reports the typed disposition of one routed binary format."""

    parser_id = PARSER_ID

    def __init__(self, format_id: str) -> None:
        self.format_id = format_id
        self.format_ids = (format_id,)

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        spec = FORMATS[self.format_id]
        if self.format_id in UNSUPPORTED_FORMAT_IDS:
            return ParseResult(
                outcome=Outcome.UNSUPPORTED_FORMAT,
                executor=self.parser_id,
                detail=spec.notes,
            )
        role = required_role_for(self.format_id)
        return ParseResult(
            outcome=Outcome.EXECUTOR_NOT_ACTIVATED,
            executor=self.parser_id,
            required_role=role,
            detail=(
                f"{spec.label} is routed to the optional {role} executor "
                "role, which is not installed or not activated"
            ),
        )


PARSERS = {
    format_id: RoutedBinaryParser(format_id)
    for format_id in (*UNSUPPORTED_FORMAT_IDS, *MEDIA_FORMAT_IDS)
}
