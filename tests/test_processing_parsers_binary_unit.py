"""Unit tests for the routed binary adapters.

These adapters never decode bytes to text, so the tests need no doc-role
dependency: each routed format id must answer with its documented typed
outcome and must not emit any block text or replacement characters.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from cortex_v2.processing.contracts import Outcome, ParserLimits, SourceRef
from cortex_v2.processing.formats import FORMATS, required_role_for
from cortex_v2.processing.parsers import binary

ROUTED_FORMATS = (
    "ole_legacy",
    "archive",
    "unknown_binary",
    "image",
    "audio",
    "video",
)
UNSUPPORTED_FORMATS = ("ole_legacy", "archive", "unknown_binary")
MEDIA_FORMATS = ("image", "audio", "video")

#: Every byte value including NUL and invalid UTF-8 sequences: decoding this
#: to text would necessarily produce replacement characters.
JUNK_BYTES = bytes(range(256)) * 4


def _source(data: bytes | None) -> SourceRef:
    return SourceRef(
        scope_id=uuid4(),
        content_id=uuid4(),
        revision=1,
        content_class="artifact",
        media_type="application/octet-stream",
        body_text="",
        content_hash=hashlib.sha256(data or b"").digest(),
        filename="blob.bin",
        body_bytes=data,
        size_bytes=len(data) if data is not None else None,
    )


def test_registry_shape() -> None:
    assert set(binary.PARSERS) == set(ROUTED_FORMATS)
    for format_id, parser in binary.PARSERS.items():
        assert parser.parser_id == "parser.routed@1"
        assert parser.format_ids == (format_id,)
    assert getattr(binary, "DEPENDENCIES", {}) == {}


@pytest.mark.parametrize("format_id", UNSUPPORTED_FORMATS)
def test_unsupported_binaries_are_typed(format_id) -> None:
    result = binary.PARSERS[format_id].parse(
        _source(JUNK_BYTES), ParserLimits()
    )
    assert result.outcome is Outcome.UNSUPPORTED_FORMAT
    assert result.detail == FORMATS[format_id].notes
    assert result.executor == "parser.routed@1"
    assert result.format_id is None
    assert result.detected_by is None
    assert result.required_role is None
    assert result.blocks == ()


@pytest.mark.parametrize("format_id", MEDIA_FORMATS)
def test_media_formats_name_their_optional_role(format_id) -> None:
    result = binary.PARSERS[format_id].parse(
        _source(JUNK_BYTES), ParserLimits()
    )
    role = required_role_for(format_id)
    assert role == format_id
    assert result.outcome is Outcome.EXECUTOR_NOT_ACTIVATED
    assert result.required_role == role
    assert role in (result.detail or "")
    assert "optional" in (result.detail or "")
    assert result.executor == "parser.routed@1"
    assert result.blocks == ()


@pytest.mark.parametrize("format_id", ROUTED_FORMATS)
def test_outcomes_do_not_depend_on_bytes(format_id) -> None:
    with_bytes = binary.PARSERS[format_id].parse(
        _source(JUNK_BYTES), ParserLimits()
    )
    without_bytes = binary.PARSERS[format_id].parse(
        _source(None), ParserLimits()
    )
    assert with_bytes.outcome is without_bytes.outcome
    assert with_bytes.detail == without_bytes.detail
    assert with_bytes.required_role == without_bytes.required_role


@pytest.mark.parametrize("format_id", ROUTED_FORMATS)
def test_bytes_are_never_decoded_to_text(format_id) -> None:
    result = binary.PARSERS[format_id].parse(
        _source(b"\xff\xfe\x00\x80garbage"), ParserLimits()
    )
    assert result.blocks == ()
    assert not any(block.text for block in result.blocks)
    assert "\ufffd" not in (result.detail or "")
    assert not any("\ufffd" in warning for warning in result.warnings)
    assert result.stats == {}
