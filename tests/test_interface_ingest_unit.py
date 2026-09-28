"""Unit tests for ingest parsers and replacement planning (R11).

Parsers are pure; the atomic replacement pipeline is DB-backed and covered by
tests/test_interface_integration.py.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from cortex_v2.ingest.connectors import plan_replacement
from cortex_v2.ingest.parsers import (
    MAX_ITEM_BYTES,
    TranscriptParseError,
    parse_transcript,
)


CODEX_ITEM_EXAMPLES = {
    "AgentMessage": {"content": "synthetic", "phase": "final"},
    "Reasoning": {"summary_text": "synthetic plan", "raw_content": "fake thought"},
    "CommandExecution": {
        "command": "echo synthetic", "cwd": "/fake", "stdout": "synthetic",
        "stderr": "", "status": "completed", "exit_code": 0,
        "aggregated_output": "synthetic", "source": "fake",
    },
    "CollabAgentToolCall": {
        "tool": "fake-tool", "status": "completed", "agents_states": [],
    },
    "FileChange": {"changes": [{"path": "fake.txt"}], "status": "completed"},
    "ImageView": {"path": "/fake/image.png"},
    "McpToolCall": {
        "server": "fake-server", "tool": "fake-tool",
        "arguments": {"query": "fake"}, "result": "synthetic",
        "status": "completed",
    },
    "SubAgentActivity": {
        "kind": "fake", "agent_thread_id": "fake-thread",
        "agent_path": "fake-agent",
    },
    "UserMessage": {"content": "synthetic user message"},
    "Extension": {
        "kind": "fake", "action": "search", "query": "synthetic",
        "results": [],
    },
    "ContextCompaction": {"content": "synthetic compacted context"},
}


def jsonl(*rows: dict) -> str:
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def test_claude_jsonl_extracts_messages_and_timestamps():
    raw = jsonl(
        {
            "type": "user",
            "timestamp": "2026-09-25T10:00:00Z",
            "message": {"role": "user", "content": "hello there"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-25T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "name": "read", "input": {"p": 1}},
                ],
            },
        },
    )
    outcome = parse_transcript("claude_jsonl", raw)
    assert [item.role for item in outcome.items] == ["user", "assistant"]
    assert outcome.items[0].text == "hello there"
    assert "hi" in outcome.items[1].text
    assert "tool_use:read" in outcome.items[1].text
    assert outcome.items[0].observed_at is not None
    assert outcome.warnings == ()


def test_codex_jsonl_supported():
    raw = jsonl(
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "plan it"}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "planned"}]},
    )
    outcome = parse_transcript("codex_jsonl", raw)
    assert [item.text for item in outcome.items] == ["plan it", "planned"]


def test_beat_jsonl_supported():
    raw = jsonl(
        {"kind": "beat", "text": "heartbeat 1", "at": "2026-09-25T09:00:00Z"},
        {"kind": "note", "body": "worker note"},
    )
    outcome = parse_transcript("beat_jsonl", raw)
    assert [item.text for item in outcome.items] == ["heartbeat 1", "worker note"]
    assert all(item.role == "system" for item in outcome.items)


def test_generic_jsonl_requires_role_and_text():
    outcome = parse_transcript(
        "generic_jsonl", jsonl({"role": "user", "text": "ok"})
    )
    assert outcome.items[0].text == "ok"
    with pytest.raises(TranscriptParseError) as excinfo:
        parse_transcript("generic_jsonl", jsonl({"role": "user"}))
    assert excinfo.value.failures[0]["line"] == 1


def test_unknown_line_types_are_warnings_not_silent_loss():
    raw = jsonl(
        {"type": "user", "message": {"role": "user", "content": "a"}},
        {"type": "mystery", "payload": {}},
    )
    outcome = parse_transcript("claude_jsonl", raw)
    assert len(outcome.items) == 1
    assert any("mystery" in warning for warning in outcome.warnings)
    assert any("line 2" in warning or ":2" in warning for warning in outcome.warnings)



@pytest.mark.parametrize(
    ("format_name", "bad_row", "failure_code"),
    [
        (
            "claude_jsonl",
            {"type": "assistant", "message": {
                "role": "assistant", "content": [{"type": "image", "data": "x"}]
            }},
            "missing_text",
        ),
        (
            "codex_jsonl",
            {"type": "message", "role": "assistant",
             "content": [{"type": "unknown", "data": "x"}]},
            "unsupported_content_shape",
        ),
    ],
)
def test_unparseable_message_row_fails_whole_transcript(
    format_name, bad_row, failure_code
):
    good = (
        {"type": "user", "message": {"role": "user", "content": "keep me"}}
        if format_name == "claude_jsonl"
        else {"type": "message", "role": "user", "content": "keep me"}
    )

    with pytest.raises(TranscriptParseError) as excinfo:
        parse_transcript(format_name, jsonl(good, bad_row))

    assert excinfo.value.failures[0]["line"] == 2
    assert excinfo.value.failures[0]["code"] == failure_code


@pytest.mark.parametrize(
    ("bad_row", "failure_code"),
    [
        ({"type": "assistant", "message": "not-an-object"}, "missing_text"),
        (
            {"type": "assistant", "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "partial"},
                    {"type": "text", "text": 42},
                ],
            }},
            "missing_text",
        ),
        (
            {"type": "assistant", "message": {
                "role": "unexpected", "content": "body"
            }},
            "invalid_role",
        ),
    ],
)
def test_invalid_message_shape_or_role_fails_closed(bad_row, failure_code):
    valid = {"type": "user", "message": {"role": "user", "content": "kept"}}
    with pytest.raises(TranscriptParseError) as excinfo:
        parse_transcript("claude_jsonl", jsonl(valid, bad_row))
    assert excinfo.value.failures[0]["line"] == 2
    assert excinfo.value.failures[0]["code"] == failure_code

def test_malformed_json_fails_the_whole_transcript():
    raw = '{"type":"user","message":{"role":"user","content":"a"}}\n{broken\n'
    with pytest.raises(TranscriptParseError) as excinfo:
        parse_transcript("claude_jsonl", raw)
    failures = excinfo.value.failures
    assert len(failures) == 1
    assert failures[0]["line"] == 2
    assert failures[0]["code"] == "malformed_json"


def test_oversized_message_is_typed_failure_not_truncation():
    raw = jsonl(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": "x" * (MAX_ITEM_BYTES + 1),
            },
        }
    )
    with pytest.raises(TranscriptParseError) as excinfo:
        parse_transcript("claude_jsonl", raw)
    assert excinfo.value.failures[0]["code"] == "message_too_large"


def test_encoded_message_body_and_payload_exact_limit_and_one_over():
    from cortex_v2.ingest.connectors import _enforce_item_limit

    payload = {
        "role": "assistant",
        "text": "ok",
        "parts": [{"type": "tool_use", "input": ""}],
    }
    overhead = len("ok".encode()) + len(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode())
    payload["parts"][0]["input"] = "x" * (MAX_ITEM_BYTES - overhead)

    assert _enforce_item_limit("ok", payload, 1) == MAX_ITEM_BYTES
    payload["parts"][0]["input"] += "x"
    with pytest.raises(TranscriptParseError) as excinfo:
        _enforce_item_limit("ok", payload, 1)
    assert excinfo.value.failures[0]["code"] == "message_too_large"


def test_bad_timestamp_is_warning_without_content_loss():
    raw = jsonl(
        {"type": "user", "timestamp": "not-a-time",
         "message": {"role": "user", "content": "kept"}}
    )
    outcome = parse_transcript("claude_jsonl", raw)
    assert outcome.items[0].text == "kept"
    assert outcome.items[0].observed_at is None
    assert any("timestamp" in warning for warning in outcome.warnings)


def test_empty_transcript_is_typed_failure():
    with pytest.raises(TranscriptParseError) as excinfo:
        parse_transcript("claude_jsonl", "   \n")
    assert excinfo.value.failures[0]["code"] == "no_messages"


def test_claude_mixed_blocks_preserve_typed_payload_and_opaque_markers():
    image = b"fake-image"
    hidden = "fake-redacted"
    arguments = {"needle": "synthetic", "limit": 2}
    blocks = [
        {"type": "text", "text": "answer"},
        {"type": "thinking", "thinking": "synthetic reasoning", "signature": "sig"},
        {"type": "redacted_thinking", "data": hidden},
        {"type": "tool_use", "id": "tool-1", "name": "lookup", "input": arguments},
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "found"},
        {"type": "tool_result", "tool_use_id": "tool-1",
         "content": [{"type": "text", "text": "block result"}]},
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(image).decode("ascii"),
        }},
    ]
    row = {"type": "assistant", "message": {
        "role": "assistant", "content": blocks,
    }}

    item = parse_transcript("claude_jsonl", jsonl(row)).items[0]

    assert item.role == "assistant"
    assert item.text.startswith("answer\n[thinking] synthetic reasoning")
    assert '"limit":2,"needle":"synthetic"' in item.text
    assert item.parts[1]["type"] == "thinking"
    assert item.parts[1]["text"] == "synthetic reasoning"
    assert item.parts[1]["signature"] == {
        "field_path": "row.message.content[1].signature",
        "json_type": "string",
        "byte_length": len("sig".encode()),
        "sha256": hashlib.sha256(b"sig").hexdigest(),
    }
    assert item.parts[2]["type"] == "redacted_thinking"
    assert item.parts[2]["byte_length"] == len(hidden.encode())
    assert item.parts[2]["sha256"] == hashlib.sha256(hidden.encode()).hexdigest()
    assert item.parts[2]["data"] == {
        "field_path": "row.message.content[2].data",
        "json_type": "string",
        "byte_length": len(hidden.encode()),
        "sha256": hashlib.sha256(hidden.encode()).hexdigest(),
    }
    assert item.parts[3]["input"] == (
        '{"limit":2,"needle":"synthetic"}'
    )
    assert item.parts[4]["content"] == "found"
    assert item.parts[5]["content"] == "block result"
    assert item.parts[6]["type"] == "image"
    assert item.parts[6]["media_type"] == "image/png"
    assert item.parts[6]["byte_length"] == len(image)
    assert item.parts[6]["sha256"] == hashlib.sha256(image).hexdigest()
    assert item.parts[6]["source_type"] == "base64"
    assert item.parts[6]["source"]["data"] == {
        "field_path": "row.message.content[6].source.data",
        "json_type": "string",
        "byte_length": len(image),
        "sha256": hashlib.sha256(image).hexdigest(),
    }


def test_claude_thinking_only_row_is_a_message():
    row = {"type": "assistant", "message": {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "synthetic plan"}],
    }}
    item = parse_transcript("claude_jsonl", jsonl(row)).items[0]
    assert item.text == "[thinking] synthetic plan"
    assert item.parts == ({"type": "thinking", "text": "synthetic plan"},)


@pytest.mark.parametrize(
    ("payload", "expected_type", "expected_fragment"),
    [
        ({"type": "message", "role": "assistant",
          "content": [{"type": "output_text", "text": "synthetic answer"}]},
         "output_text", "synthetic answer"),
        ({"type": "reasoning", "encrypted_content": "fake-cipher"},
         "reasoning", hashlib.sha256(b"fake-cipher").hexdigest()),
        ({"type": "function_call", "name": "lookup",
          "arguments": '{"z":1,"a":2}', "call_id": "call-1",
          "namespace": "synthetic-tools"},
         "function_call", '{"a":2,"z":1}'),
        ({"type": "function_call_output", "call_id": "call-1",
          "output": "synthetic result"}, "function_call_output", "synthetic result"),
        ({"type": "custom_tool_call", "name": "custom",
          "input": '{"query":"fake"}', "call_id": "call-2"},
         "custom_tool_call", '{"query":"fake"}'),
        ({"type": "custom_tool_call_output", "call_id": "call-2",
          "output": "custom result"}, "custom_tool_call_output", "custom result"),
        ({"type": "agent_message", "content": "synthetic update"},
         "agent_message", "synthetic update"),
    ],
)
def test_codex_response_items_preserve_message_bearing_payloads(
    payload, expected_type, expected_fragment
):
    row = {"type": "response_item", "payload": payload}
    item = parse_transcript("codex_jsonl", jsonl(row)).items[0]
    assert expected_fragment in item.text
    assert item.parts[0]["type"] == expected_type
    if payload["type"] == "function_call":
        assert item.parts[0]["namespace"] == "synthetic-tools"
        assert item.parts[0]["call_id"] == "call-1"


@pytest.mark.parametrize("kind", sorted(CODEX_ITEM_EXAMPLES))
def test_codex_completed_item_retains_synthetic_typed_fields(kind):
    nested = {
        "type": kind, "id": "fake-id", **CODEX_ITEM_EXAMPLES[kind],
    }
    row = {"type": "event_msg", "payload": {
        "type": "item_completed", "item": nested,
    }}
    item = parse_transcript("codex_jsonl", jsonl(row)).items[0]
    canonical = json.dumps(
        nested, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert item.text == f"[item_completed:{kind}] {canonical}"
    assert item.parts == ({"type": "item_completed", "item": canonical},)


def test_codex_explicit_metadata_is_warned_while_message_is_retained():
    metadata = {"type": "event_msg", "payload": {"type": "token_count"}}
    message = {"type": "response_item", "payload": {
        "type": "function_call", "name": "lookup",
        "arguments": '{"q":"fake"}',
    }}
    outcome = parse_transcript("codex_jsonl", jsonl(metadata, message))
    assert len(outcome.items) == 1
    assert '"q":"fake"' in outcome.items[0].text
    assert any("token_count" in warning for warning in outcome.warnings)


@pytest.mark.parametrize("kind", ["attachment", "result"])
def test_claude_message_bearing_sidecars_are_not_skipped(kind):
    row = {"type": kind, kind: {"type": "text", "text": "synthetic sidecar"}}
    item = parse_transcript("claude_jsonl", jsonl(row)).items[0]
    assert item.parts[0]["type"] == kind
    assert "synthetic sidecar" in item.text


def test_claude_system_row_with_content_is_not_discarded_as_metadata():
    row = {"type": "system", "content": "synthetic system instruction"}
    item = parse_transcript("claude_jsonl", jsonl(row)).items[0]
    assert item.role == "system"
    assert item.text == "synthetic system instruction"
    assert item.parts == (
        {"type": "text", "text": "synthetic system instruction"},
    )


@pytest.mark.parametrize("top_type", ["compacted", "response_item"])
def test_codex_compaction_opaque_payload_has_digest_marker(top_type):
    hidden = "fake-cipher"
    payload = {"encrypted_content": hidden}
    if top_type == "response_item":
        payload["type"] = "compaction"
    row = {"type": top_type, "payload": payload}
    item = parse_transcript("codex_jsonl", jsonl(row)).items[0]
    assert item.parts[0]["type"] == "compaction"
    assert item.parts[0]["encrypted_content"] == {
        "field_path": "row.payload.encrypted_content",
        "json_type": "string",
        "byte_length": len(hidden.encode()),
        "sha256": hashlib.sha256(hidden.encode()).hexdigest(),
    }
    assert hidden not in item.text
    assert hashlib.sha256(hidden.encode()).hexdigest() in item.text


def test_codex_custom_tool_input_is_free_form_text():
    row = {"type": "response_item", "payload": {
        "type": "custom_tool_call", "name": "shell",
        "input": "run this command",
    }}
    item = parse_transcript("codex_jsonl", jsonl(row)).items[0]
    assert item.text == "[custom_tool_call:shell] run this command"
    assert item.parts[0]["input"] == "run this command"


@pytest.mark.parametrize("nested_text", ["123", "plain text"])
def test_claude_nested_input_string_keeps_its_type_and_bytes(nested_text):
    row = {"type": "assistant", "message": {
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "lookup",
                     "input": {"input": nested_text}}],
    }}
    item = parse_transcript("claude_jsonl", jsonl(row)).items[0]
    expected = json.dumps(
        {"input": nested_text}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    )
    assert item.parts[0]["input"] == expected
    assert expected in item.text


@pytest.mark.parametrize("format_name", ["claude_jsonl", "codex_jsonl"])
def test_json_string_root_literal_is_parsed_once(format_name):
    root = json.dumps("123")
    if format_name == "claude_jsonl":
        row = {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "lookup",
                         "input": root}],
        }}
    else:
        row = {"type": "response_item", "payload": {
            "type": "function_call", "name": "lookup",
            "arguments": root,
        }}
    item = parse_transcript(format_name, jsonl(row)).items[0]
    field = "input" if format_name == "claude_jsonl" else "arguments"
    assert item.parts[0][field] == root
    assert root in item.text


def test_unknown_data_prefixes_become_plain_markers_not_quarantine():
    values = {
        "unknown_note": "data:notes",
        "unknown_uri": "data:text/plain,hello",
        "url": "data:image/png;base64,aGVsbG8=",
        "image_url": "data:image/png;base64,aGVsbG8=",
    }
    row = {"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": "safe",
        **values,
    }}
    item = parse_transcript("codex_jsonl", jsonl(row)).items[0]
    markers = item.parts[-1]["fields"]
    for key, raw in values.items():
        assert markers[key] == {
            "field_path": f"row.payload.{key}",
            "json_type": "string",
            "byte_length": len(raw.encode()),
            "sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }
        assert raw not in item.text


def test_typed_argument_data_uri_hashes_decoded_bytes():
    encoded = "data:image/png;base64,aGVsbG8="
    row = {"type": "response_item", "payload": {
        "type": "function_call", "name": "inspect",
        "arguments": json.dumps({"url": encoded}),
    }}
    item = parse_transcript("codex_jsonl", jsonl(row)).items[0]
    argument = json.loads(item.parts[0]["arguments"])
    assert argument["url"] == {
        "field_path": "row.payload.arguments.url",
        "json_type": "string",
        "byte_length": 5,
        "sha256": hashlib.sha256(b"hello").hexdigest(),
        "media_type": "image/png",
    }
    assert encoded not in item.text


def test_replacement_plan_pairs_by_ordinal():
    old = [("old0", "u0"), ("old1", "u1"), ("old2", "u2")]
    new = [("new0", "v0"), ("new1", "v1")]
    plan = plan_replacement(old, new)
    assert plan["supersede"] == [
        {"old": "old0", "successor": "new0"},
        {"old": "old1", "successor": "new1"},
    ]
    assert plan["invalidate"] == [{"old": "old2"}]
    assert plan["create_only"] == []


def test_replacement_plan_growth_creates_without_invalidating():
    old = [("old0", "u0")]
    new = [("new0", "v0"), ("new1", "v1")]
    plan = plan_replacement(old, new)
    assert plan["supersede"] == [{"old": "old0", "successor": "new0"}]
    assert plan["invalidate"] == []
    assert plan["create_only"] == ["new1"]


def test_replacement_plan_first_generation_replaces_nothing():
    plan = plan_replacement([], [("new0", "v0")])
    assert plan == {"supersede": [], "invalidate": [], "create_only": ["new0"]}
