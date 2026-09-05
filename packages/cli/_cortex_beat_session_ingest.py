#!/usr/bin/env python3
"""Import Beat PI and harness session logs through the Cortex sessions API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROLE_MAP = {
    "user": "user",
    "human": "human",
    "assistant": "assistant",
    "agent": "agent",
    "system": "system",
}


@dataclass
class ParsedSession:
    payload: dict[str, Any]
    source_path: Path
    original_agent: str
    messages: int
    skipped_thinking_parts: int = 0


class CortexClient:
    def __init__(self, *, api_url: str, project: str, timeout: int = 60):
        self.api_url = api_url.rstrip("/")
        self.project = project
        self.timeout = timeout

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        agent: str = "",
    ) -> dict[str, Any]:
        data = None
        headers = {"X-Project": self.project}
        if agent:
            headers["X-Agent-Name"] = agent
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = Request(
            f"{self.api_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail = body
            try:
                parsed = json.loads(body)
                detail = str(parsed.get("detail") or parsed.get("error") or body)
            except json.JSONDecodeError:
                pass
            raise RuntimeError(f"API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"API request failed: {exc}") from exc

        if not raw.strip():
            return {}
        return json.loads(raw)


def normalize_agent(value: str, fallback: str = "") -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", (value or "").lower()).strip("-._")
    return text or fallback


def coerce_uuid(value: object, *, path: Path) -> str:
    candidates = []
    if value:
        candidates.append(str(value))
    candidates.extend(re.split(r"[_\s]+", path.stem))
    for candidate in candidates:
        try:
            return str(uuid.UUID(candidate))
        except (TypeError, ValueError):
            continue
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(path.resolve())))


def compact_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def content_part_text(part: Any) -> tuple[str, int]:
    if part is None:
        return "", 0
    if isinstance(part, str):
        return part, 0
    if not isinstance(part, dict):
        return compact_json(part), 0

    part_type = re.sub(r"[^a-z0-9]+", "", str(part.get("type") or "").lower())
    if part_type == "thinking":
        return "", 1
    if part_type in {"text", "inputtext", "outputtext"}:
        return str(part.get("text") or part.get("content") or ""), 0
    if part_type in {"toolcall", "functioncall"}:
        name = (
            part.get("name")
            or part.get("toolName")
            or (part.get("function") or {}).get("name")
            or "tool"
        )
        args = (
            part.get("arguments")
            or part.get("args")
            or part.get("input")
            or (part.get("function") or {}).get("arguments")
            or {}
        )
        return f"[tool call: {name}]\n{compact_json(args)}", 0
    if part_type in {"toolresult", "functionresult", "tooloutput"}:
        name = (
            part.get("name")
            or part.get("toolName")
            or part.get("toolCallId")
            or part.get("tool_call_id")
            or "tool"
        )
        output = (
            part.get("output")
            or part.get("result")
            or part.get("content")
            or part.get("text")
            or ""
        )
        return f"[tool result: {name}]\n{compact_json(output)}", 0

    for key in ("text", "content", "output", "message"):
        if part.get(key):
            return compact_json(part[key]), 0
    return compact_json(part), 0


def extract_content(content: Any) -> tuple[str, int]:
    parts = content if isinstance(content, list) else [content]
    text_parts: list[str] = []
    skipped_thinking = 0
    for part in parts:
        text, skipped = content_part_text(part)
        skipped_thinking += skipped
        text = text.strip()
        if text:
            text_parts.append(text)
    return "\n\n".join(text_parts).strip(), skipped_thinking


def writer_for(
    original_agent: str,
    *,
    ingest_agent: str,
    registered_writers: set[str],
) -> str:
    original = normalize_agent(original_agent, fallback=ingest_agent)
    if original in registered_writers:
        return original
    return ingest_agent


def parse_pi_session(
    path: Path,
    *,
    project: str,
    ingest_agent: str,
    registered_writers: set[str],
    allow_placeholder: bool = False,
) -> ParsedSession | None:
    original_agent = normalize_agent(path.parent.name, fallback=ingest_agent)
    writer = writer_for(
        original_agent,
        ingest_agent=ingest_agent,
        registered_writers=registered_writers,
    )
    session_id = ""
    session_ts = ""
    cwd = ""
    provider = "pi"
    model = ""
    messages: list[dict[str, Any]] = []
    parsed_records = 0
    skipped_thinking_parts = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            parsed_records += 1
            record_type = str(item.get("type") or "")
            if record_type == "session":
                session_id = str(item.get("id") or session_id)
                session_ts = str(item.get("timestamp") or session_ts)
                cwd = str(item.get("cwd") or cwd)
                continue
            if record_type == "model_change":
                provider = str(item.get("provider") or provider)
                model = str(item.get("modelId") or item.get("model") or model)
                continue
            if record_type != "message":
                continue

            msg = item.get("message") if isinstance(item.get("message"), dict) else item
            role = ROLE_MAP.get(str(msg.get("role") or "system").lower(), "system")
            content, skipped = extract_content(msg.get("content"))
            skipped_thinking_parts += skipped
            if not content:
                continue
            messages.append(
                {
                    "role": role,
                    "content": content,
                    "ts": item.get("timestamp") or msg.get("timestamp") or session_ts or None,
                    "metadata": {
                        "source": "beat-pi-session",
                        "source_agent": original_agent,
                        "original_agent": original_agent,
                        "record_id": item.get("id"),
                        "parent_id": item.get("parentId"),
                        "provider": provider,
                        "model": model,
                        "line": line_number,
                    },
                }
            )

    if not messages:
        if not allow_placeholder:
            return None
        messages.append(
            {
                "role": "system",
                "content": f"Imported empty Beat PI session file {path.name}",
                "ts": session_ts or None,
                "metadata": {
                    "source": "beat-pi-session",
                    "source_agent": original_agent,
                    "original_agent": original_agent,
                    "placeholder": True,
                },
            }
        )

    payload = {
        "session_uuid": coerce_uuid(session_id, path=path),
        "agent": writer,
        "task": f"Imported Beat PI session for {original_agent}",
        "source_path": str(path.resolve()),
        "provider": provider,
        "cwd": cwd or None,
        "source_kind": "beat-pi-session",
        "metadata": {
            "source": "beat-pi-session",
            "original_agent": original_agent,
            "writer_agent": writer,
            "project": project,
            "provider": provider,
            "model": model,
            "filename": path.name,
            "parsed_records": parsed_records,
            "messages_parsed": len(messages),
            "skipped_thinking_parts": skipped_thinking_parts,
        },
        "messages": messages,
    }
    return ParsedSession(
        payload=payload,
        source_path=path,
        original_agent=original_agent,
        messages=len(messages),
        skipped_thinking_parts=skipped_thinking_parts,
    )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def parse_harness_session(
    directory: Path,
    *,
    project: str,
    ingest_agent: str,
    registered_writers: set[str],
    allow_placeholder: bool = False,
) -> ParsedSession | None:
    session_json = directory / "session.json"
    try:
        meta = json.loads(session_json.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    original_agent = normalize_agent(str(meta.get("agent") or directory.name), fallback=ingest_agent)
    writer = writer_for(
        original_agent,
        ingest_agent=ingest_agent,
        registered_writers=registered_writers,
    )
    provider = str(meta.get("harness") or "beat-harness")
    model = str(meta.get("model") or "")
    messages: list[dict[str, Any]] = []

    for entry in load_jsonl(directory / "prompts.jsonl"):
        card = str(entry.get("card") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        content = card or compact_json(entry)
        if not content.strip():
            continue
        messages.append(
            {
                "role": "system",
                "content": content,
                "ts": entry.get("ts") or meta.get("updated_at") or None,
                "metadata": {
                    "source": "beat-harness-prompts",
                    "source_agent": original_agent,
                    "original_agent": original_agent,
                    "summary": summary,
                    "handoff_id": entry.get("handoff_id"),
                },
            }
        )

    for entry in load_jsonl(directory / "tool-summary.jsonl"):
        messages.append(
            {
                "role": "system",
                "content": compact_json(entry),
                "ts": entry.get("ts") or meta.get("updated_at") or None,
                "metadata": {
                    "source": "beat-harness-tool-summary",
                    "source_agent": original_agent,
                    "original_agent": original_agent,
                },
            }
        )

    visible_path = directory / "visible-thinking.md"
    if visible_path.exists():
        visible_text = visible_path.read_text(encoding="utf-8", errors="replace").strip()
        boilerplate = "This file stores operator-visible reasoning summaries"
        if visible_text and not (boilerplate in visible_text and visible_text.count("## ") == 0):
            messages.append(
                {
                    "role": "agent",
                    "content": visible_text,
                    "ts": meta.get("updated_at") or None,
                    "metadata": {
                        "source": "beat-harness-visible-work",
                        "source_agent": original_agent,
                        "original_agent": original_agent,
                    },
                }
            )

    if not messages:
        if not allow_placeholder:
            return None
        messages.append(
            {
                "role": "system",
                "content": f"Imported empty Beat harness session mirror for {original_agent}",
                "ts": meta.get("updated_at") or None,
                "metadata": {
                    "source": "beat-harness-session",
                    "source_agent": original_agent,
                    "original_agent": original_agent,
                    "placeholder": True,
                },
            }
        )

    payload = {
        "session_uuid": coerce_uuid(meta.get("session_id"), path=session_json),
        "agent": writer,
        "task": f"Imported Beat harness session mirror for {original_agent}",
        "source_path": str(session_json.resolve()),
        "provider": provider,
        "cwd": str(meta.get("cwd") or ""),
        "source_kind": "beat-harness-session",
        "metadata": {
            "source": "beat-harness-session",
            "original_agent": original_agent,
            "writer_agent": writer,
            "project": project,
            "provider": provider,
            "model": model,
            "messages_parsed": len(messages),
        },
        "messages": messages,
    }
    return ParsedSession(
        payload=payload,
        source_path=session_json,
        original_agent=original_agent,
        messages=len(messages),
    )


def discover_sessions(
    root: Path,
    *,
    project: str,
    ingest_agent: str,
    registered_writers: set[str],
    include_pi: bool,
    include_harness: bool,
    allow_placeholder: bool,
) -> list[ParsedSession]:
    sessions: list[ParsedSession] = []
    if include_pi:
        pi_root = root / "beat" / "state" / "pi-sessions" / project
        if pi_root.exists():
            for path in sorted(pi_root.rglob("*.jsonl")):
                parsed = parse_pi_session(
                    path,
                    project=project,
                    ingest_agent=ingest_agent,
                    registered_writers=registered_writers,
                    allow_placeholder=allow_placeholder,
                )
                if parsed is not None:
                    sessions.append(parsed)
    if include_harness:
        harness_root = root / "beat" / "state" / "harness-sessions" / project
        if harness_root.exists():
            for directory in sorted(p for p in harness_root.rglob("*") if p.is_dir() and (p / "session.json").exists()):
                parsed = parse_harness_session(
                    directory,
                    project=project,
                    ingest_agent=ingest_agent,
                    registered_writers=registered_writers,
                    allow_placeholder=allow_placeholder,
                )
                if parsed is not None:
                    sessions.append(parsed)
    return sessions


def default_registered_writers(project: str, ingest_agent: str) -> set[str]:
    env_value = os.environ.get("CORTEX_INGEST_REGISTERED_WRITERS", "").strip()
    if env_value:
        return {normalize_agent(part, fallback=ingest_agent) for part in env_value.split(",") if part.strip()}
    return {normalize_agent(ingest_agent)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.getcwd(), help="Repository root. Default: cwd")
    parser.add_argument("--project", default=os.environ.get("CORTEX_PROJECT", ""))
    parser.add_argument("--agent", default=os.environ.get("CORTEX_AGENT", ""), help="Fallback registered ingest writer")
    parser.add_argument("--force", action="store_true", help="Reingest sessions even when Cortex has the session id")
    parser.add_argument("--limit", type=int, default=0, help="Maximum sessions to ingest after filtering")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--max-errors", type=int, default=10)
    parser.add_argument("--include-pi", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-harness", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--allow-placeholder", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Memory efficiency (E2+E4): the API reads CORTEX_E2_DISTILL and
    # CORTEX_E4_COMPACT from its own environment.
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    project = normalize_agent(args.project, fallback="")
    if not project:
        print("ERROR: --project or CORTEX_PROJECT is required", file=sys.stderr)
        return 2
    ingest_agent = normalize_agent(args.agent, fallback="")
    if not ingest_agent:
        print("ERROR: --agent or CORTEX_AGENT is required; Kaidera OS will not guess a writer.", file=sys.stderr)
        return 2
    registered_writers = default_registered_writers(project, ingest_agent)
    client = CortexClient(
        api_url=os.environ.get("CORTEX_API", os.environ.get("CORTEX_API_URL", "http://localhost:8501")),
        project=project,
        timeout=int(os.environ.get("CORTEX_API_MAX_TIME", "120")),
    )

    ingested_ids: set[str] = set()
    if not args.force:
        data = client.request_json("GET", "/sessions/ingested-ids")
        ingested_ids = {str(value) for value in data.get("ids", [])}

    sessions = discover_sessions(
        root,
        project=project,
        ingest_agent=ingest_agent,
        registered_writers=registered_writers,
        include_pi=args.include_pi,
        include_harness=args.include_harness,
        allow_placeholder=args.allow_placeholder,
    )

    attempted = imported = skipped = failed = messages = skipped_thinking = 0
    for parsed in sessions:
        session_uuid = str(parsed.payload["session_uuid"])
        if not args.force and session_uuid in ingested_ids:
            skipped += 1
            continue
        if args.limit > 0 and attempted >= args.limit:
            break
        attempted += 1
        try:
            result = client.request_json(
                "POST",
                "/sessions/ingest",
                parsed.payload,
                agent=str(parsed.payload.get("agent") or ingest_agent),
            )
        except RuntimeError as exc:
            failed += 1
            print(f"ERROR: {parsed.source_path}: {exc}", file=sys.stderr)
            if failed >= args.max_errors:
                break
            continue
        imported += 1
        inserted = int(result.get("messages_inserted") or parsed.messages)
        messages += inserted
        skipped_thinking += parsed.skipped_thinking_parts
        print(
            "Ingested "
            f"{parsed.payload.get('source_kind')} {session_uuid} "
            f"agent={parsed.payload.get('agent')} "
            f"original_agent={parsed.original_agent} "
            f"messages={inserted}"
        )
        if args.sleep_seconds:
            time.sleep(args.sleep_seconds)

    print(
        "Summary: "
        f"discovered={len(sessions)} attempted={attempted} imported={imported} "
        f"skipped={skipped} failed={failed} messages={messages} "
        f"skipped_thinking_parts={skipped_thinking}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
