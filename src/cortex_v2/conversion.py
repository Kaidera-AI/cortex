"""Row-accountable conversion interface for identities and canonical content.

Pure classification used before any processing projection: every source row
maps to exactly one durable ledger outcome (migrated, quarantined, retained or
rejected) carrying the original payload, its hash, the source reference, a
reason, an owner and a disposition. No row is silently discarded and no legacy
system is contacted from this module; the W6 converter binds these entries to
the durable ``cortex_core.conversion_quarantine`` ledger and run manifests.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

OUTCOME_MIGRATED = "migrated"
OUTCOME_QUARANTINED = "quarantined"
OUTCOME_RETAINED = "retained"
OUTCOME_REJECTED = "rejected"

REASON_UNKNOWN_SOURCE_KEY = "unknown_source_key"
REASON_NULL_SCOPE_MAPPING = "null_scope_mapping"
REASON_NULL_ACTOR_MAPPING = "null_actor_mapping"
REASON_DUPLICATE_IDENTITY = "duplicate_identity"
REASON_DUPLICATE_SOURCE_KEY = "duplicate_source_key"
REASON_UNSUPPORTED_RECORD_KIND = "unsupported_record_kind"
REASON_MALFORMED_IDENTITY_ID = "malformed_identity_id"
REASON_EMPTY_ORIGINAL_BODY = "empty_original_body"
REASON_PAYLOAD_SCHEMA_VIOLATION = "payload_schema_violation"

DISPOSITION_CONVERTED = "converted"
DISPOSITION_QUARANTINE_REVIEW = "quarantine_review"
DISPOSITION_RETAIN_IN_PLACE = "retain_in_place"
DISPOSITION_REJECT_REVIEW = "reject_review"

SUPPORTED_IDENTITY_TYPES = frozenset(
    {"principal", "actor", "scope", "alias", "membership"}
)
SUPPORTED_CONTENT_CLASSES = frozenset(
    {
        "decision",
        "lesson",
        "knowledge",
        "progress",
        "diary",
        "message",
        "session",
        "artifact",
        "work_product",
    }
)

CONTENT_PAYLOAD_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "decision": ("statement",),
    "lesson": ("lesson",),
    "knowledge": ("content",),
    "progress": ("summary",),
    "diary": ("entry",),
    "message": ("role", "text"),
    "session": ("source", "message_count"),
    "artifact": ("media_type", "bytes_sha256", "location"),
    "work_product": ("kind", "summary", "references"),
}


@dataclass(frozen=True, slots=True)
class ConversionLedgerEntry:
    outcome: str
    source_namespace: str
    source_reference: str
    payload_sha256: str
    original_payload: dict[str, Any]
    reason: str | None
    owner: str | None
    disposition: str
    target_reference: str | None
    content_class: str | None = None


@dataclass(frozen=True, slots=True)
class ConversionResult:
    entries: tuple[ConversionLedgerEntry, ...]

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(entry.outcome for entry in self.entries))

    def accounts_for(self, source_row_count: int) -> bool:
        return len(self.entries) == source_row_count


def canonical_payload_sha256(row: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _entry(
    row: dict[str, Any],
    outcome: str,
    disposition: str,
    *,
    reason: str | None = None,
    owner: str | None = None,
    target_reference: str | None = None,
    content_class: str | None = None,
) -> ConversionLedgerEntry:
    namespace = str(row.get("source_namespace") or "")
    source_key = str(row.get("source_key") or "")
    return ConversionLedgerEntry(
        outcome=outcome,
        source_namespace=namespace,
        source_reference=f"{namespace}:{source_key}",
        payload_sha256=canonical_payload_sha256(row),
        original_payload=row,
        reason=reason,
        owner=owner,
        disposition=disposition,
        target_reference=target_reference,
        content_class=content_class,
    )


def _quarantine(row: dict[str, Any], reason: str, owner: str | None = None):
    return _entry(
        row, OUTCOME_QUARANTINED, DISPOSITION_QUARANTINE_REVIEW, reason=reason, owner=owner
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _common_row_checks(
    row: dict[str, Any],
    scope_map: dict[str, str],
    actor_map: dict[str, str],
) -> tuple[str | None, str | None, str | None]:
    """Return (quarantine_reason, mapped_scope, mapped_actor)."""
    if _text(row.get("source_key")) is None:
        return REASON_UNKNOWN_SOURCE_KEY, None, None
    scope = row.get("scope")
    mapped_scope = scope_map.get(scope) if isinstance(scope, str) else None
    if mapped_scope is None:
        return REASON_NULL_SCOPE_MAPPING, None, None
    actor = row.get("actor")
    mapped_actor = actor_map.get(actor) if isinstance(actor, str) else None
    if mapped_actor is None:
        return REASON_NULL_ACTOR_MAPPING, None, None
    return None, mapped_scope, mapped_actor


def convert_identities(
    rows: list[dict[str, Any]],
    *,
    scope_map: dict[str, str],
    actor_map: dict[str, str],
) -> ConversionResult:
    entries: list[ConversionLedgerEntry] = []
    seen_natural_keys: set[tuple[str, str]] = set()
    for row in rows:
        namespace = str(row.get("source_namespace") or "")
        source_key = _text(row.get("source_key"))
        if source_key is None:
            entries.append(_quarantine(row, REASON_UNKNOWN_SOURCE_KEY))
            continue
        if row.get("retain_in_place") is True:
            entries.append(
                _entry(row, OUTCOME_RETAINED, DISPOSITION_RETAIN_IN_PLACE)
            )
            continue
        identity_type = row.get("identity_type")
        if identity_type not in SUPPORTED_IDENTITY_TYPES:
            entries.append(_quarantine(row, REASON_UNSUPPORTED_RECORD_KIND))
            continue
        if not _is_uuid(row.get("identity_id")):
            entries.append(
                _entry(
                    row,
                    OUTCOME_REJECTED,
                    DISPOSITION_REJECT_REVIEW,
                    reason=REASON_MALFORMED_IDENTITY_ID,
                )
            )
            continue
        reason, mapped_scope, mapped_actor = _common_row_checks(row, scope_map, actor_map)
        if reason is not None:
            entries.append(_quarantine(row, reason))
            continue
        natural_key = (namespace, source_key)
        if natural_key in seen_natural_keys:
            entries.append(
                _quarantine(row, REASON_DUPLICATE_IDENTITY, owner=mapped_actor)
            )
            continue
        seen_natural_keys.add(natural_key)
        entries.append(
            _entry(
                row,
                OUTCOME_MIGRATED,
                DISPOSITION_CONVERTED,
                owner=mapped_actor,
                target_reference=(
                    f"cortex-v2:{identity_type}:{mapped_scope}:{row['identity_id']}"
                ),
            )
        )
    return ConversionResult(tuple(entries))


def convert_content(
    rows: list[dict[str, Any]],
    *,
    scope_map: dict[str, str],
    actor_map: dict[str, str],
) -> ConversionResult:
    entries: list[ConversionLedgerEntry] = []
    seen_natural_keys: set[tuple[str, str]] = set()
    for row in rows:
        namespace = str(row.get("source_namespace") or "")
        source_key = _text(row.get("source_key"))
        if source_key is None:
            entries.append(_quarantine(row, REASON_UNKNOWN_SOURCE_KEY))
            continue
        record_kind = row.get("record_kind")
        if record_kind not in SUPPORTED_CONTENT_CLASSES:
            entries.append(_quarantine(row, REASON_UNSUPPORTED_RECORD_KIND))
            continue
        reason, mapped_scope, mapped_actor = _common_row_checks(row, scope_map, actor_map)
        if reason is not None:
            entries.append(_quarantine(row, reason))
            continue
        body = row.get("body")
        if not isinstance(body, str) or not body.strip() or "\x00" in body:
            entries.append(_quarantine(row, REASON_EMPTY_ORIGINAL_BODY))
            continue
        payload = row.get("payload")
        required = CONTENT_PAYLOAD_REQUIRED_FIELDS[str(record_kind)]
        if not isinstance(payload, dict) or any(
            field not in payload or payload[field] in (None, "", {})
            for field in required
        ):
            entries.append(_quarantine(row, REASON_PAYLOAD_SCHEMA_VIOLATION))
            continue
        natural_key = (namespace, source_key)
        if natural_key in seen_natural_keys:
            entries.append(
                _quarantine(row, REASON_DUPLICATE_SOURCE_KEY, owner=mapped_actor)
            )
            continue
        seen_natural_keys.add(natural_key)
        entries.append(
            _entry(
                row,
                OUTCOME_MIGRATED,
                DISPOSITION_CONVERTED,
                owner=mapped_actor,
                target_reference=f"cortex-v2:content:{mapped_scope}:{namespace}:{source_key}",
                content_class=str(record_kind),
            )
        )
    return ConversionResult(tuple(entries))
