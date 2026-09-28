"""Transcript parsers with parse-loss protection (R11).

Parsing is pure and strict: a malformed line fails the whole transcript with
typed failures (the connector quarantines the original payload rather than
partially applying it), unrecognized-but-parsable lines become explicit
warnings, and an oversized message is a typed failure — never a silent
truncation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_ITEM_BYTES = 65_536
MAX_ITEMS = 512
VALID_MESSAGE_ROLES = frozenset(("user", "assistant", "system", "tool"))
CLAUDE_METADATA_TYPES = frozenset((
    "summary", "file-history-snapshot", "file-history-delta",
    "agent-name", "ai-title", "atis-latch", "bridge-session", "cost-state",
    "custom-title", "last-prompt", "launched", "mode", "permission-mode",
    "queue-operation", "started",
))
CODEX_METADATA_TYPES = frozenset((
    "session_meta", "turn_context", "token_usage_record", "world_state",
    "inter_agent_communication_metadata", "token_count",
    "task_complete", "task_started", "thread_goal_updated",
    "thread_settings_applied", "turn_aborted",
))
CODEX_COMPLETED_ITEM_TYPES = frozenset((
    "AgentMessage", "Reasoning", "CommandExecution", "CollabAgentToolCall",
    "FileChange", "ImageView", "McpToolCall", "SubAgentActivity",
    "UserMessage", "Extension", "ContextCompaction",
))


class TranscriptParseError(Exception):
    def __init__(self, failures: list[dict[str, Any]]) -> None:
        self.failures = failures
        super().__init__(
            f"transcript parse failed: {failures[0]['code']} "
            f"(line {failures[0].get('line')})"
        )


@dataclass(frozen=True, slots=True)
class ParsedItem:
    role: str
    text: str
    observed_at: datetime | None
    source_line: int
    parts: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    items: tuple[ParsedItem, ...]
    warnings: tuple[str, ...]


def _parse_timestamp(value: Any, line: int, warnings: list[str]
                     ) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            warnings.append(f"line {line}: unparseable timestamp {value!r}")
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        warnings.append(f"line {line}: unparseable timestamp {value!r}")
        return None


def _check_size(text: str, line: int, failures: list[dict[str, Any]]) -> None:
    if len(text.encode("utf-8")) > MAX_ITEM_BYTES:
        failures.append(
            {"line": line, "code": "message_too_large",
             "detail": f"message exceeds {MAX_ITEM_BYTES} bytes"}
        )


def _iter_lines(raw: str):
    for number, line in enumerate(raw.splitlines(), start=1):
        if line.strip():
            yield number, line


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


SEMANTIC_FIELDS: dict[str, frozenset[str]] = {
    "claude.user": frozenset(("type", "message", "timestamp")),
    "claude.assistant": frozenset(("type", "message", "timestamp")),
    "claude.system": frozenset(("type", "content", "timestamp")),
    "claude.attachment": frozenset(("type", "attachment", "timestamp")),
    "claude.result": frozenset(("type", "result", "timestamp")),
    "claude.message": frozenset(("role", "content")),
    "beat.row": frozenset(("kind", "text", "body", "at", "ts")),
    "generic.row": frozenset(("role", "text", "at", "timestamp")),
    "codex.response_item": frozenset(("type", "payload", "ts", "timestamp")),
    "codex.event_msg": frozenset(("type", "payload", "ts", "timestamp")),
    "codex.compacted": frozenset(("type", "payload", "ts", "timestamp")),
    "codex.message": frozenset(("type", "role", "content", "text", "id", "phase")),
    "codex.reasoning": frozenset(("type", "content", "summary", "id")),
    "codex.function_call": frozenset(
        ("type", "name", "arguments", "call_id", "namespace", "id")
    ),
    "codex.function_call_output": frozenset(
        ("type", "output", "call_id", "id")
    ),
    "codex.custom_tool_call": frozenset(
        ("type", "name", "input", "call_id", "id")
    ),
    "codex.custom_tool_call_output": frozenset(
        ("type", "output", "call_id", "id")
    ),
    "codex.agent_message": frozenset(
        ("type", "content", "author", "recipient", "id")
    ),
    "codex.item_completed": frozenset(
        ("type", "item", "thread_id", "turn_id", "started_at_ms",
         "completed_at_ms")
    ),
    "codex.compaction": frozenset(("type", "content", "summary", "id")),
    "block.text": frozenset(("type", "text")),
    "block.input_text": frozenset(("type", "text")),
    "block.output_text": frozenset(("type", "text")),
    "block.summary_text": frozenset(("type", "text")),
    "block.thinking": frozenset(("type", "thinking")),
    "block.redacted_thinking": frozenset(("type",)),
    "block.encrypted_content": frozenset(("type",)),
    "block.tool_use": frozenset(("type", "name", "id", "caller", "input")),
    "block.tool_result": frozenset(
        ("type", "content", "tool_use_id", "is_error")
    ),
    "block.image": frozenset(("type", "source")),
    "image.source": frozenset(("type", "media_type")),
    "argument": frozenset((
        "a", "z", "p", "q", "query", "needle", "limit", "path",
        "file_path", "command", "content", "text", "offset", "args",
        "input", "url", "pattern",
    )),
    "item.AgentMessage": frozenset(
        ("type", "id", "content", "phase", "delivery", "questions",
         "memory_citation")
    ),
    "item.Reasoning": frozenset(
        ("type", "id", "summary_text", "raw_content", "summary")
    ),
    "item.CommandExecution": frozenset((
        "type", "id", "command", "cwd", "stdout", "stderr", "status",
        "exit_code", "aggregated_output", "source", "duration",
        "process_id", "parsed_cmd", "script_path", "formatted_output",
        "plugin_id", "arguments",
    )),
    "item.CollabAgentToolCall": frozenset(
        ("type", "id", "tool", "status", "agents_states", "arguments",
         "receiver_agents", "receiver_thread_ids", "sender_thread_id")
    ),
    "item.FileChange": frozenset(
        ("type", "id", "changes", "status")
    ),
    "item.ImageView": frozenset(("type", "id", "path", "image_url")),
    "item.McpToolCall": frozenset((
        "type", "id", "server", "tool", "arguments", "result", "status",
        "duration", "pluginId", "readOnlyHint", "actionName", "appName",
        "connectorId", "linkId", "mcpAppResourceUri", "mcpAppUi",
    )),
    "item.SubAgentActivity": frozenset(
        ("type", "id", "kind", "agent_thread_id", "agent_path")
    ),
    "item.UserMessage": frozenset(("type", "id", "content", "client_id")),
    "item.Extension": frozenset(
        ("type", "id", "kind", "action", "query", "results", "durationMs")
    ),
    "item.ContextCompaction": frozenset(("type", "id", "content")),
}

CHILD_SHAPES: dict[tuple[str, str], str] = {
    ("claude.user", "message"): "claude.message",
    ("claude.assistant", "message"): "claude.message",
    ("claude.message", "content"): "block",
    ("claude.system", "content"): "block",
    ("claude.attachment", "attachment"): "block",
    ("claude.result", "result"): "block",
    ("codex.response_item", "payload"): "codex_payload",
    ("codex.event_msg", "payload"): "codex_payload",
    ("codex.compacted", "payload"): "codex_payload",
    ("codex.message", "content"): "block",
    ("codex.reasoning", "content"): "block",
    ("codex.reasoning", "summary"): "block",
    ("codex.agent_message", "content"): "block",
    ("codex.compaction", "content"): "block",
    ("codex.compaction", "summary"): "block",
    ("codex.item_completed", "item"): "completed_item",
    ("codex.function_call", "arguments"): "argument_json_root",
    ("codex.custom_tool_call", "input"): "argument",
    ("codex.function_call_output", "output"): "argument",
    ("codex.custom_tool_call_output", "output"): "argument",
    ("block.tool_use", "input"): "argument_json_root",
    ("block.tool_result", "content"): "block",
    ("block.image", "source"): "image.source",
}
for item_shape in CODEX_COMPLETED_ITEM_TYPES:
    for field in (
        "arguments", "changes", "parsed_cmd", "result",
        "agents_states", "results", "questions",
    ):
        CHILD_SHAPES[(f"item.{item_shape}", field)] = "argument"

CHILD_SHAPES[("argument", "args")] = "argument"
CHILD_SHAPES[("argument", "input")] = "argument"

class ProjectionError(Exception):
    pass


def _marker(
    value: Any, path: str, key: str, parent: dict[str, Any],
    typed_data_uri: bool = False,
) -> dict[str, Any]:
    media_type = None
    if value is None:
        json_type, raw = "null", b"null"
    elif isinstance(value, bool):
        json_type, raw = "boolean", _canonical_json(value).encode()
    elif isinstance(value, str):
        json_type, raw = "string", value.encode("utf-8")
        if key == "data" and parent.get("type") == "base64":
            try:
                raw = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ProjectionError("invalid base64 opaque data") from exc
        elif typed_data_uri and value.startswith("data:"):
            prefix, separator, encoded = value.partition(",")
            match = re.fullmatch(r"data:([^;,]+);base64", prefix)
            if not separator or match is None:
                raise ProjectionError("invalid typed data URI")
            media_type = match.group(1)
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ProjectionError("invalid typed data URI") from exc
    elif isinstance(value, (int, float)):
        json_type, raw = "number", _canonical_json(value).encode()
    elif isinstance(value, list):
        json_type, raw = "array", _canonical_json(value).encode("utf-8")
    else:
        json_type, raw = "object", _canonical_json(value).encode("utf-8")
    marker = {
        "field_path": path,
        "json_type": json_type,
        "byte_length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if media_type is not None:
        marker["media_type"] = media_type
    return marker


def _project(value: Any, shape: str, path: str, depth: int = 0) -> Any:
    if depth > 12:
        raise ProjectionError("content nesting exceeds projection depth")
    if shape == "argument_json_root":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ProjectionError("invalid JSON-string arguments") from exc
        shape = "argument"
    if isinstance(value, list):
        if shape not in ("block", "argument"):
            raise ProjectionError("unsupported list content")
        return [
            _project(item, shape, f"{path}[{index}]", depth + 1)
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    if shape in ("block", "codex_payload", "completed_item"):
        family = {
            "block": "block",
            "codex_payload": "codex",
            "completed_item": "item",
        }[shape]
        shape = f"{family}.{value.get('type')}"
    allowed = SEMANTIC_FIELDS.get(shape)
    if allowed is None:
        raise ProjectionError("unrecognized message-bearing shape")
    projected: dict[str, Any] = {}
    for key, field in value.items():
        safe_key = (
            key if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
            else f"field_{hashlib.sha256(key.encode()).hexdigest()[:12]}"
        )
        field_path = f"{path}.{safe_key}"
        typed_data_uri = (
            (key == "url" and shape in ("argument", "image.source"))
            or (key == "image_url" and shape == "item.ImageView")
        )
        opaque = key in ("data", "signature", "encrypted_content") or (
            typed_data_uri and isinstance(field, str)
            and field.startswith("data:")
        )
        output_key = key if key in allowed else safe_key
        if opaque or key not in allowed:
            projected[output_key] = _marker(
                field, field_path, key, value, typed_data_uri
            )
        else:
            child_shape = CHILD_SHAPES.get((shape, key), "scalar")
            if isinstance(field, (dict, list)) and child_shape == "scalar":
                projected[output_key] = _marker(field, field_path, key, value)
            else:
                projected[output_key] = _project(
                    field, child_shape, field_path, depth + 1
                )
    return projected




def _blocks_to_text(
    content: Any, line: int, warnings: list[str], depth: int = 0
) -> tuple[str | None, tuple[dict[str, Any], ...]]:
    if depth > 4:
        warnings.append(f"line {line}: nested content exceeds parser depth")
        return None, ()
    if isinstance(content, str):
        return content, ({"type": "text", "text": content},)
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return None, ()

    rendered: list[str] = []
    parts: list[dict[str, Any]] = []
    incomplete = False
    for block in content:
        if not isinstance(block, dict):
            warnings.append(f"line {line}: non-object content block")
            incomplete = True
            continue
        kind = block.get("type")
        if kind in ("text", "input_text", "output_text", "summary_text"):
            text = block.get("text")
            if not isinstance(text, str) or not text:
                warnings.append(f"line {line}: text block has no text")
                incomplete = True
                continue
            part = {"type": kind, "text": text}
            part.update({
                key: value for key, value in block.items()
                if key not in ("type", "text")
            })
            rendered.append(text)
            parts.append(part)
        elif kind == "thinking":
            thought = block.get("thinking")
            if not isinstance(thought, str) or not thought:
                incomplete = True
                continue
            part = {"type": kind, "text": thought}
            part.update({
                key: value for key, value in block.items()
                if key not in ("type", "thinking")
            })
            parts.append(part)
            rendered.append(f"[thinking] {thought}")
        elif kind in ("redacted_thinking", "encrypted_content"):
            opaque = block.get("data", block.get("encrypted_content"))
            if not isinstance(opaque, dict) or "sha256" not in opaque:
                incomplete = True
                continue
            part = {
                "type": kind,
                "byte_length": opaque["byte_length"],
                "sha256": opaque["sha256"],
            }
            part.update({
                key: value for key, value in block.items()
                if key != "type"
            })
            parts.append(part)
            rendered.append(
                f"[{kind} bytes={opaque['byte_length']} sha256={opaque['sha256']}]"
            )
        elif kind == "tool_use":
            name = block.get("name")
            if not isinstance(name, str) or not name or "input" not in block:
                incomplete = True
                continue
            arguments = _canonical_json(block["input"])
            part = {"type": kind, "name": name, "input": arguments}
            part.update({
                key: value for key, value in block.items()
                if key not in ("type", "name", "input")
            })
            parts.append(part)
            rendered.append(f"[tool_use:{name}] {arguments}")
        elif kind == "tool_result":
            inner = block.get("content")
            if isinstance(inner, (str, list, dict)):
                text, inner_parts = _blocks_to_text(
                    inner, line, warnings, depth + 1
                )
            else:
                text, inner_parts = None, ()
            if not text:
                incomplete = True
                continue
            part = {"type": kind, "content": text, "parts": list(inner_parts)}
            part.update({
                key: value for key, value in block.items()
                if key not in ("type", "content")
            })
            parts.append(part)
            rendered.append(f"[tool_result] {text}")
        elif kind == "image":
            source = block.get("source")
            if not isinstance(source, dict):
                incomplete = True
                continue
            media_type = source.get("media_type")
            opaque = source.get("data", source.get("url"))
            if (
                not isinstance(media_type, str)
                or not isinstance(opaque, dict)
                or "sha256" not in opaque
            ):
                incomplete = True
                continue
            part = {
                "type": kind,
                "media_type": media_type,
                "byte_length": opaque["byte_length"],
                "sha256": opaque["sha256"],
                "source_type": source.get("type"),
                "source": source,
            }
            part.update({
                key: value for key, value in block.items()
                if key not in ("type", "source")
            })
            parts.append(part)
            rendered.append(
                f"[image media_type={media_type} bytes={opaque['byte_length']} "
                f"sha256={opaque['sha256']}]"
            )
        else:
            warnings.append(
                f"line {line}: unrecognized content block type {kind!r}"
            )
            incomplete = True
    if incomplete or not rendered:
        return None, tuple(parts)
    return "\n".join(rendered), tuple(parts)


def _parse_claude(lines, warnings, failures):
    items = []
    for number, line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            failures.append({"line": number, "code": "malformed_json"})
            continue
        if not isinstance(row, dict):
            failures.append({"line": number, "code": "malformed_json"})
            continue
        kind = row.get("type")
        if kind == "system" and "content" not in row:
            warnings.append(f"line {number}: metadata type 'system' skipped")
            continue
        if kind in CLAUDE_METADATA_TYPES:
            warnings.append(f"line {number}: metadata type {kind!r} skipped")
            continue
        if kind not in ("user", "assistant", "system", "attachment", "result"):
            warnings.append(
                f"line {number}: unrecognized line type {kind!r} skipped"
            )
            continue
        try:
            row = _project(row, f"claude.{kind}", "row")
        except ProjectionError:
            failures.append({"line": number, "code": "unsupported_content_shape"})
            continue
        metadata = {
            key: value for key, value in row.items()
            if key not in ("type", "message", "content", "attachment",
                           "result", "timestamp")
        }
        if kind in ("attachment", "result"):
            text, inner_parts = _blocks_to_text(row.get(kind), number, warnings)
            if not text:
                failures.append({"line": number, "code": "missing_text"})
                continue
            part = {"type": kind, "content": text, "parts": list(inner_parts)}
            if metadata:
                part["metadata"] = metadata
            body = f"[{kind}] {text}"
            _check_size(body, number, failures)
            items.append(ParsedItem(
                role="user" if kind == "attachment" else "assistant",
                text=body,
                observed_at=_parse_timestamp(
                    row.get("timestamp"), number, warnings
                ),
                source_line=number, parts=(part,),
            ))
            continue
        if kind == "system":
            text, parts = _blocks_to_text(row.get("content"), number, warnings)
            role = "system"
        else:
            message = row.get("message")
            if not isinstance(message, dict):
                failures.append({"line": number, "code": "missing_text"})
                continue
            role = message.get("role") or kind
            metadata.update({
                f"message.{key}": value for key, value in message.items()
                if key not in ("role", "content")
            })
            text, parts = _blocks_to_text(
                message.get("content"), number, warnings
            )
        if not isinstance(role, str) or role not in VALID_MESSAGE_ROLES:
            failures.append({"line": number, "code": "invalid_role"})
            continue
        if not text:
            failures.append({
                "line": number, "code": "missing_text",
                "detail": "message content is missing or cannot be parsed",
            })
            continue
        if metadata:
            parts = (*parts, {"type": "message_metadata", "fields": metadata})
        _check_size(text, number, failures)
        items.append(ParsedItem(
            role=role, text=text,
            observed_at=_parse_timestamp(
                row.get("timestamp"), number, warnings
            ),
            source_line=number, parts=parts,
        ))
    return items




def _codex_message(
    payload: dict[str, Any], number: int, row: dict[str, Any],
    warnings: list[str], failures: list[dict[str, Any]],
) -> ParsedItem | None:
    kind = payload.get("type")
    role = payload.get("role") or (
        "user" if kind == "message" and row.get("type") is None else "assistant"
    )
    parts: tuple[dict[str, Any], ...] = ()
    text: str | None = None

    if kind == "message":
        text, parts = _blocks_to_text(
            payload.get("content", payload.get("text")), number, warnings
        )
        metadata = {
            key: value for key, value in payload.items()
            if key not in ("type", "role", "content", "text")
        }
        if metadata and parts:
            parts = (*parts, {"type": "message_metadata", "fields": metadata})
    elif kind == "reasoning":
        fragments: list[str] = []
        part: dict[str, Any] = {"type": kind}
        opaque = payload.get("encrypted_content")
        if isinstance(opaque, dict) and "sha256" in opaque:
            part["encrypted_content"] = opaque
            fragments.append(
                f"[reasoning encrypted bytes={opaque['byte_length']} "
                f"sha256={opaque['sha256']}]"
            )
        summary = payload.get("content", payload.get("summary"))
        if isinstance(summary, list) and all(
            isinstance(value, str) for value in summary
        ):
            summary = "\n".join(summary)
        if summary is not None:
            summary_text, summary_parts = _blocks_to_text(
                summary, number, warnings
            )
            if summary_text is None:
                failures.append({"line": number, "code": "missing_text"})
            else:
                fragments.append(summary_text)
                part["summary"] = list(summary_parts)
        part.update({
            key: value for key, value in payload.items()
            if key not in ("type", "encrypted_content", "content", "summary")
        })
        text = "\n".join(fragments) if fragments else None
        parts = (part,) if text else ()
    elif kind in ("function_call", "custom_tool_call"):
        name = payload.get("name")
        field = "arguments" if kind == "function_call" else "input"
        if isinstance(name, str) and name and field in payload:
            arguments = (
                payload[field]
                if kind == "custom_tool_call" and isinstance(payload[field], str)
                else _canonical_json(payload[field])
            )
            text = f"[{kind}:{name}] {arguments}"
            part = {"type": kind, "name": name, field: arguments}
            part.update({
                key: value for key, value in payload.items()
                if key not in ("type", "name", field)
            })
            parts = (part,)
    elif kind in ("function_call_output", "custom_tool_call_output"):
        if "output" in payload:
            output = payload["output"]
            rendered = output if isinstance(output, str) else _canonical_json(output)
            text = f"[{kind}] {rendered}"
            part = {"type": kind, "output": rendered}
            part.update({
                key: value for key, value in payload.items()
                if key not in ("type", "output")
            })
            parts = (part,)
    elif kind == "agent_message":
        content, content_parts = _blocks_to_text(
            payload.get("content"), number, warnings
        )
        if content:
            text = content
            part = {"type": kind, "content": content,
                    "parts": list(content_parts)}
            part.update({
                key: value for key, value in payload.items()
                if key not in ("type", "content")
            })
            parts = (part,)
    elif kind == "item_completed":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") in CODEX_COMPLETED_ITEM_TYPES:
            serialized = _canonical_json(item)
            text = f"[item_completed:{item['type']}] {serialized}"
            part = {"type": kind, "item": serialized}
            part.update({
                key: value for key, value in payload.items()
                if key not in ("type", "item")
            })
            parts = (part,)
            if item["type"] == "UserMessage":
                role = "user"
        else:
            warnings.append(
                f"line {number}: unknown completed item type skipped"
            )
            return None
    elif kind == "compaction":
        opaque = payload.get("encrypted_content")
        if isinstance(opaque, dict) and "sha256" in opaque:
            text = (
                f"[compaction encrypted bytes={opaque['byte_length']} "
                f"sha256={opaque['sha256']}]"
            )
            part = {"type": kind, "encrypted_content": opaque}
            part.update({
                key: value for key, value in payload.items()
                if key not in ("type", "encrypted_content")
            })
            parts = (part,)
        else:
            serialized = _canonical_json(payload)
            text = f"[compaction] {serialized}"
            parts = ({"type": kind, "item": serialized},)

    if not isinstance(role, str) or role not in VALID_MESSAGE_ROLES:
        failures.append({"line": number, "code": "invalid_role"})
        return None
    if not text:
        failures.append({
            "line": number, "code": "missing_text",
            "detail": "message-bearing item cannot be represented",
        })
        return None
    if row.get("type") in ("response_item", "event_msg", "compacted"):
        outer = {
            key: value for key, value in row.items()
            if key not in ("type", "payload", "ts", "timestamp", "ordinal")
        }
        if outer:
            parts = (*parts, {"type": "wrapper_metadata", "fields": outer})
    _check_size(text, number, failures)
    return ParsedItem(
        role=role, text=text,
        observed_at=_parse_timestamp(
            row.get("ts", row.get("timestamp")), number, warnings
        ),
        source_line=number, parts=parts,
    )


def _parse_codex(lines, warnings, failures):
    items = []
    message_types = {
        "message", "reasoning", "function_call", "function_call_output",
        "custom_tool_call", "custom_tool_call_output", "agent_message",
        "item_completed", "compaction",
    }
    for number, line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            failures.append({"line": number, "code": "malformed_json"})
            continue
        if not isinstance(row, dict):
            failures.append({"line": number, "code": "malformed_json"})
            continue
        top_type = row.get("type")
        if top_type is not None and not isinstance(top_type, str):
            failures.append({"line": number, "code": "unsupported_content_shape"})
            continue
        if top_type in CODEX_METADATA_TYPES:
            warnings.append(f"line {number}: metadata type {top_type!r} skipped")
            continue
        if top_type == "compacted":
            original = row.get("payload")
            if not isinstance(original, dict):
                failures.append({"line": number, "code": "missing_text"})
                continue
            row = {**row, "payload": {**original, "type": "compaction"}}
            kind = "compaction"
        elif top_type in ("response_item", "event_msg"):
            payload = row.get("payload")
            if not isinstance(payload, dict):
                failures.append({"line": number, "code": "missing_text"})
                continue
            kind = payload.get("type")
        elif top_type in message_types or top_type is None:
            kind = top_type or "message"
            row = {**row, "type": kind}
        else:
            warnings.append(
                f"line {number}: unrecognized line type {top_type!r} skipped"
            )
            continue
        if kind in CODEX_METADATA_TYPES:
            warnings.append(f"line {number}: metadata type {kind!r} skipped")
            continue
        if kind not in message_types:
            warnings.append(
                f"line {number}: unrecognized item type {kind!r} skipped"
            )
            continue
        try:
            shape = (
                f"codex.{top_type}"
                if top_type in ("response_item", "event_msg", "compacted")
                else f"codex.{kind}"
            )
            row = _project(row, shape, "row")
        except ProjectionError:
            failures.append({"line": number, "code": "unsupported_content_shape"})
            continue
        payload = (
            row["payload"]
            if top_type in ("response_item", "event_msg", "compacted")
            else row
        )
        try:
            item = _codex_message(payload, number, row, warnings, failures)
        except ProjectionError:
            failures.append({"line": number, "code": "unsupported_content_shape"})
            continue
        if item is not None:
            items.append(item)
    return items


def _parse_beat(lines, warnings, failures):
    items = []
    for number, line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            failures.append({"line": number, "code": "malformed_json"})
            continue
        if not isinstance(row, dict):
            failures.append({"line": number, "code": "malformed_json"})
            continue
        kind = row.get("kind")
        if kind not in ("beat", "note", "status"):
            warnings.append(
                f"line {number}: unrecognized beat kind {kind!r} skipped"
            )
            continue
        try:
            row = _project(row, "beat.row", "row")
        except ProjectionError:
            failures.append({"line": number, "code": "unsupported_content_shape"})
            continue
        text = row.get("text", row.get("body"))
        if not isinstance(text, str) or not text:
            failures.append({"line": number, "code": "missing_text"})
            continue
        _check_size(text, number, failures)
        metadata = {
            key: value for key, value in row.items()
            if key not in ("kind", "text", "body", "at", "ts")
        }
        parts = ({"type": "text", "text": text},)
        if metadata:
            parts = (*parts, {"type": "message_metadata", "fields": metadata})
        items.append(
            ParsedItem(role="system", text=text,
                       observed_at=_parse_timestamp(
                           row.get("at", row.get("ts")), number, warnings),
                       source_line=number, parts=parts)
        )
    return items


def _parse_generic(lines, warnings, failures):
    items = []
    for number, line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            failures.append({"line": number, "code": "malformed_json"})
            continue
        if not isinstance(row, dict):
            failures.append({"line": number, "code": "malformed_json"})
            continue
        try:
            row = _project(row, "generic.row", "row")
        except ProjectionError:
            failures.append({"line": number, "code": "unsupported_content_shape"})
            continue
        role = row.get("role")
        text = row.get("text")
        if not isinstance(role, str) or not isinstance(text, str) or not text:
            failures.append({"line": number, "code": "missing_text",
                             "detail": "role and text are required"})
            continue
        if role not in VALID_MESSAGE_ROLES:
            failures.append({"line": number, "code": "invalid_role"})
            continue
        _check_size(text, number, failures)
        metadata = {
            key: value for key, value in row.items()
            if key not in ("role", "text", "at", "timestamp")
        }
        parts = ({"type": "text", "text": text},)
        if metadata:
            parts = (*parts, {"type": "message_metadata", "fields": metadata})
        items.append(
            ParsedItem(role=role, text=text,
                       observed_at=_parse_timestamp(
                           row.get("at", row.get("timestamp")), number,
                           warnings),
                       source_line=number, parts=parts)
        )
    return items


_PARSERS = {
    "claude_jsonl": _parse_claude,
    "codex_jsonl": _parse_codex,
    "beat_jsonl": _parse_beat,
    "generic_jsonl": _parse_generic,
}


def parse_transcript(transcript_format: str, raw: str) -> ParseOutcome:
    parser = _PARSERS.get(transcript_format)
    if parser is None:
        raise TranscriptParseError(
            [{"line": 0, "code": "unsupported_format",
              "detail": transcript_format}]
        )
    warnings: list[str] = []
    failures: list[dict[str, Any]] = []
    items = parser(_iter_lines(raw), warnings, failures)
    if not items and not failures:
        failures.append({"line": 0, "code": "no_messages",
                         "detail": "the transcript contains no messages"})
    if len(items) > MAX_ITEMS:
        failures.append(
            {"line": 0, "code": "too_many_messages",
             "detail": f"transcript exceeds {MAX_ITEMS} messages"}
        )
    if failures:
        raise TranscriptParseError(failures)
    return ParseOutcome(items=tuple(items), warnings=tuple(warnings))
