"""Isolated legacy-row conversion; production credentials are never an input."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .content import content_hash

_POLICY_KEYS = {"version", "installation_id", "projects", "handoff_status"}
_ALLOWED_HANDOFF_STATUSES = {
    "pending": "open",
    "released": "open",
}
_MEMORY_CLASSES = {
    "public.decisions": ("decision", "summary", "statement"),
    "public.lessons": ("lesson", "summary", "lesson"),
    "public.knowledge": ("knowledge", "content", "content"),
    "cortex.decisions": ("decision", "summary", "statement"),
    "cortex.lessons": ("lesson", "summary", "lesson"),
    "cortex.knowledge": ("knowledge", "body", "content"),
}
_CONTENT_CLASSES = {
    "public.agent_diaries": ("diary", "summary", "entry"),
    "public.messages": ("message", "content", "text"),
    "cortex.messages": ("message", "content", "text"),
    "public.work_products": ("work_product", "summary", "summary"),
}
_UUID_NAMESPACE = uuid.UUID("dcabdf23-4048-484e-b6bb-8a650484d7f6")
_HEX_SHA = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class ProjectMapping:
    scope_id: uuid.UUID
    principal_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ConversionPolicy:
    version: str
    installation_id: uuid.UUID
    projects: Mapping[str, ProjectMapping]
    handoff_status: Mapping[str, str]
    sha256: str

    @classmethod
    def from_bytes(cls, raw: bytes) -> ConversionPolicy:
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != _POLICY_KEYS:
                raise ValueError("mapping policy has unexpected fields")
            version = value["version"]
            if not isinstance(version, str) or not 1 <= len(version) <= 64:
                raise ValueError("mapping policy version is invalid")
            project_values = value["projects"]
            if not isinstance(project_values, dict) or not project_values:
                raise ValueError("mapping policy requires explicit projects")
            projects: dict[str, ProjectMapping] = {}
            for name, mapping in project_values.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or not isinstance(mapping, dict)
                    or set(mapping) != {"scope_id", "principal_id"}
                ):
                    raise ValueError("project mapping is invalid")
                projects[name] = ProjectMapping(
                    uuid.UUID(mapping["scope_id"]), uuid.UUID(mapping["principal_id"])
                )
            statuses = value["handoff_status"]
            if not isinstance(statuses, dict) or not statuses:
                raise ValueError("handoff mapping is required")
            if any(_ALLOWED_HANDOFF_STATUSES.get(k) != v for k, v in statuses.items()):
                raise ValueError("unsafe handoff mapping")
            installation = uuid.UUID(value["installation_id"])
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("mapping policy is malformed") from exc
        return cls(
            version,
            installation,
            projects,
            statuses,
            hashlib.sha256(raw).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class ConversionDecision:
    outcome: str
    reason: str | None = None
    target_relation: str | None = None
    target_id: uuid.UUID | None = None
    scope_id: uuid.UUID | None = None
    principal_id: uuid.UUID | None = None
    status: str | None = None
    content_class: str | None = None
    body: str | None = field(default=None, repr=False)
    payload: dict[str, Any] | None = field(default=None, repr=False)


def _reject(reason: str, *, skipped: bool = False) -> ConversionDecision:
    return ConversionDecision("skipped" if skipped else "quarantined", reason)


def _source_id(namespace: str, row: Mapping[str, Any]) -> uuid.UUID | None:
    value = row.get("id")
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    if isinstance(value, int) and not isinstance(value, bool):
        return uuid.uuid5(_UUID_NAMESPACE, f"{namespace}:{value}")
    return None


def _body(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    return value if len(value) <= 65536 else None


def classify_row(
    namespace: str, row: Mapping[str, Any], policy: ConversionPolicy
) -> ConversionDecision:
    """Classify a source row before writing anything; never log the input row."""
    if namespace.startswith("cortex_auth."):
        return _reject("retained_legacy_credentials", skipped=True)
    if namespace not in {*_MEMORY_CLASSES, *_CONTENT_CLASSES, "public.handoffs"}:
        return _reject("unsupported_entity")
    project = row.get("project") or row.get("project_id")
    mapping = policy.projects.get(str(project)) if project is not None else None
    if mapping is None:
        return _reject("unmapped_scope")
    target_id = _source_id(namespace, row)
    if target_id is None:
        return _reject("invalid_source_id")
    if namespace == "public.handoffs":
        status = row.get("status")
        mapped = policy.handoff_status.get(status)
        if mapped is None:
            return _reject("ambiguous_lifecycle")
        body = _body(row.get("summary"))
        if body is None:
            return _reject("missing_or_oversized_original")
        addressed_role = row.get("to_role")
        if not isinstance(addressed_role, str) or len(addressed_role) > 64:
            return _reject("unsupported_addressee")
        return ConversionDecision(
            "migrated", target_relation="cortex_coord.handoffs",
            target_id=target_id, scope_id=mapping.scope_id,
            principal_id=mapping.principal_id, status=mapped, body=body,
            payload={"title": body.splitlines()[0][:256], "to_role": addressed_role},
        )
    content_class, source_field, payload_field = (
        _MEMORY_CLASSES.get(namespace) or _CONTENT_CLASSES[namespace]
    )
    body = _body(row.get(source_field))
    if body is None:
        return _reject("missing_or_oversized_original")
    payload: dict[str, Any] = {payload_field: body}
    if content_class == "message":
        role = {"human": "user", "agent": "assistant", "system": "system"}.get(
            row.get("role")
        )
        if role is None:
            return _reject("unsupported_message_role")
        payload["role"] = role
    elif content_class == "work_product":
        payload["kind"] = row.get("activity_type") or "legacy-work-product"
        payload["references"] = row.get("artifact_refs") or []
    payload["legacy_source"] = namespace
    payload["legacy_source_id"] = str(row["id"])
    return ConversionDecision(
        "migrated", target_relation="cortex_core.content_items",
        target_id=target_id, scope_id=mapping.scope_id,
        principal_id=mapping.principal_id, content_class=content_class,
        body=body, payload=payload,
    )


def target_content_hash(decision: ConversionDecision) -> bytes:
    if (
        decision.body is None
        or decision.payload is None
        or decision.content_class is None
    ):
        raise ValueError("only canonical content has a content hash")
    return content_hash(decision.content_class, decision.payload, decision.body)


def validate_snapshot_sha(value: str) -> str:
    if not _HEX_SHA.fullmatch(value):
        raise ValueError("snapshot SHA-256 must be lowercase hex")
    return value


async def ensure_ledger_schema(connection: Any) -> None:
    """Apply converter-owned SQL once, or verify the immutable schema checksum."""
    sql = Path(__file__).with_name("converter_ledger_v1.sql").read_bytes()
    checksum = hashlib.sha256(sql).hexdigest()
    async with connection.transaction():
        exists = await connection.fetchval(
            "SELECT to_regclass('cortex_conversion.schema_versions') IS NOT NULL"
        )
        if exists:
            recorded = await connection.fetchval(
                "SELECT sha256 FROM cortex_conversion.schema_versions "
                "WHERE version = 'ledger_v1'"
            )
            if recorded != checksum:
                raise RuntimeError("converter ledger schema checksum mismatch")
            return
        if await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace "
            "WHERE nspname='cortex_conversion')"
        ):
            raise RuntimeError("partial converter schema requires owner inspection")
        await connection.execute(sql.decode("utf-8"))
        await connection.execute(
            "INSERT INTO cortex_conversion.schema_versions(version, sha256) "
            "VALUES ('ledger_v1', $1)",
            checksum,
        )
