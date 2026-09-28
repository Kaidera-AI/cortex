"""Unit tests for the YAML parser adapter (parser.yaml@1)."""

from __future__ import annotations

import sys
from uuid import UUID

import pytest

from cortex_v2.processing.contracts import (
    Outcome,
    Parser,
    ParserLimits,
    SourceRef,
)
from cortex_v2.processing.keys import text_sha256
from cortex_v2.processing.parsers.yaml import DEPENDENCIES, PARSERS, YamlParser

SCOPE_ID = UUID("11111111-1111-4111-8111-111111111111")
CONTENT_ID = UUID("22222222-2222-4222-8222-222222222222")

DEPENDENCY_REASON = "doc-role dependency not present in the w1 candidate test image"

SERVICE_DOC = (
    "service: cortex\n"
    "limits:\n"
    "  max_input_chars: 1000\n"
    "  max_depth: 8\n"
    "tags:\n"
    "  - alpha\n"
    "  - beta\n"
)
DEEP_DOC = "top:\n  a:\n    b:\n      c: 1\n"
SEQ_DOC = "- one\n- two: 2\n"


def require_yaml():
    return pytest.importorskip("yaml", reason=DEPENDENCY_REASON)


def make_source(
    body_text: str,
    *,
    media_type: str = "application/yaml",
) -> SourceRef:
    return SourceRef(
        scope_id=SCOPE_ID,
        content_id=CONTENT_ID,
        revision=1,
        content_class="artifact",
        media_type=media_type,
        body_text=body_text,
        content_hash=text_sha256(body_text),
    )


def test_module_exposes_parser_with_declared_dependency() -> None:
    assert set(PARSERS) == {"yaml"}
    assert isinstance(PARSERS["yaml"], YamlParser)
    assert PARSERS["yaml"].parser_id == "parser.yaml@1"
    assert PARSERS["yaml"].format_ids == ("yaml",)
    assert isinstance(PARSERS["yaml"], Parser)
    assert DEPENDENCIES == {"yaml": "PyYAML"}


def test_registry_loads_yaml_adapter() -> None:
    from cortex_v2.processing import parsers as registry

    assert registry.parser_for("yaml") is PARSERS["yaml"]
    assert registry.declared_dependency("yaml") == "PyYAML"


def test_missing_pyyaml_is_typed_executor_not_activated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "yaml", None)
    result = YamlParser().parse(make_source("a: 1\n"), ParserLimits())
    assert result.outcome is Outcome.EXECUTOR_NOT_ACTIVATED
    assert result.required_role == "doc"
    assert result.executor == "parser.yaml@1"
    assert result.format_id is None
    assert result.detected_by is None
    assert "PyYAML" in (result.detail or "")


def test_top_level_sections_are_verbatim_line_slices() -> None:
    require_yaml()
    result = YamlParser().parse(make_source(SERVICE_DOC), ParserLimits())
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.yaml@1"
    assert [block.block_kind for block in result.blocks] == ["section"] * 3
    assert [block.heading_path for block in result.blocks] == [
        ("service",),
        ("limits",),
        ("tags",),
    ]
    assert [block.span.json_pointer for block in result.blocks] == [
        "/service",
        "/limits",
        "/tags",
    ]
    assert [block.text for block in result.blocks] == [
        "service: cortex",
        "limits:\n  max_input_chars: 1000\n  max_depth: 8",
        "tags:\n  - alpha\n  - beta",
    ]
    assert [(b.span.line_start, b.span.line_end) for b in result.blocks] == [
        (1, 1),
        (2, 4),
        (5, 7),
    ]
    for block in result.blocks:
        assert SERVICE_DOC[block.span.start : block.span.end] == block.text
        assert block.span.detail == {}


def test_top_level_sequence_items_get_index_pointers() -> None:
    require_yaml()
    result = YamlParser().parse(make_source(SEQ_DOC), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [block.span.json_pointer for block in result.blocks] == ["/0", "/1"]
    assert [block.text for block in result.blocks] == ["- one", "- two: 2"]
    assert all(block.heading_path == () for block in result.blocks)
    for block in result.blocks:
        assert SEQ_DOC[block.span.start : block.span.end] == block.text


def test_document_marker_joins_first_section() -> None:
    require_yaml()
    doc = "---\nservice: cortex\nlimits: 8\n"
    result = YamlParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.OK
    assert [block.text for block in result.blocks] == [
        "---\nservice: cortex",
        "limits: 8",
    ]
    assert result.blocks[0].span.line_start == 1
    assert doc[result.blocks[0].span.start : result.blocks[0].span.end] == (
        result.blocks[0].text
    )


def test_unknown_tag_is_rejected() -> None:
    require_yaml()
    result = YamlParser().parse(make_source("value: !foo bar\n"), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED
    assert (result.detail or "").startswith("unsafe_yaml_tag")


def test_python_object_tag_is_rejected() -> None:
    require_yaml()
    doc = "exploit: !!python/object/apply:os.system ['id']\n"
    result = YamlParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED
    assert (result.detail or "").startswith("unsafe_yaml_tag")


def test_broken_yaml_is_corrupt() -> None:
    require_yaml()
    result = YamlParser().parse(make_source("{ unclosed\n"), ParserLimits())
    assert result.outcome is Outcome.INPUT_CORRUPT


def test_multi_document_stream_is_corrupt() -> None:
    require_yaml()
    result = YamlParser().parse(make_source("a: 1\n---\nb: 2\n"), ParserLimits())
    assert result.outcome is Outcome.INPUT_CORRUPT


@pytest.mark.parametrize("doc", ["", "# only a comment\n", "null\n"])
def test_no_content_is_input_empty(doc: str) -> None:
    require_yaml()
    result = YamlParser().parse(make_source(doc), ParserLimits())
    assert result.outcome is Outcome.INPUT_EMPTY


def test_nul_byte_is_input_rejected() -> None:
    require_yaml()
    result = YamlParser().parse(make_source("a: x\u0000y\n"), ParserLimits())
    assert result.outcome is Outcome.INPUT_REJECTED


def test_oversize_input_is_quota_exceeded() -> None:
    require_yaml()
    result = YamlParser().parse(
        make_source("a: 0123456789\n"), ParserLimits(max_input_chars=8)
    )
    assert result.outcome is Outcome.QUOTA_EXCEEDED


def test_value_index_truncates_sections() -> None:
    require_yaml()
    result = YamlParser().parse(
        make_source(SERVICE_DOC), ParserLimits(max_values_indexed=2)
    )
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 2
    assert result.truncated is True
    assert result.warnings == ("value_index_truncated",)


def test_deep_nesting_is_summarized_not_expanded() -> None:
    require_yaml()
    result = YamlParser().parse(make_source(DEEP_DOC), ParserLimits(max_depth=1))
    assert result.outcome is Outcome.OK
    assert len(result.blocks) == 1
    assert result.blocks[0].heading_path == ("top",)
    assert result.blocks[0].span.detail == {
        "depth_summarized": True,
        "indent_levels": 4,
    }
    assert "depth_summarized" in result.warnings
    assert DEEP_DOC[result.blocks[0].span.start : result.blocks[0].span.end] == (
        result.blocks[0].text
    )


def test_long_section_is_clipped_at_line_boundary() -> None:
    require_yaml()
    result = YamlParser().parse(
        make_source(SERVICE_DOC), ParserLimits(max_block_chars=40)
    )
    assert result.outcome is Outcome.OK
    assert result.truncated is True
    assert "block_chars_truncated" in result.warnings
    limits_block = result.blocks[1]
    assert limits_block.text == "limits:\n  max_input_chars: 1000"
    assert (limits_block.span.line_start, limits_block.span.line_end) == (2, 3)
    assert SERVICE_DOC[limits_block.span.start : limits_block.span.end] == (
        limits_block.text
    )
