from __future__ import annotations

import hashlib
import json

import pytest

from cortex_v2.conversion import (
    OUTCOME_MIGRATED,
    OUTCOME_QUARANTINED,
    OUTCOME_REJECTED,
    OUTCOME_RETAINED,
    REASON_DUPLICATE_IDENTITY,
    REASON_NULL_ACTOR_MAPPING,
    REASON_NULL_SCOPE_MAPPING,
    REASON_UNKNOWN_SOURCE_KEY,
    REASON_UNSUPPORTED_RECORD_KIND,
    SUPPORTED_CONTENT_CLASSES,
    convert_content,
    convert_identities,
)

SCOPE_MAP = {"proj-alpha": "11111111-1111-4111-8111-111111111111"}
ACTOR_MAP = {"agent-kai": "22222222-2222-4222-8222-222222222222"}


def identity_row(
    source_key: str,
    *,
    identity_type: str = "principal",
    identity_id: str = "33333333-3333-4333-8333-333333333333",
    scope: str | None = "proj-alpha",
    actor: str | None = "agent-kai",
    retain: bool = False,
) -> dict[str, object]:
    row: dict[str, object] = {
        "source_namespace": "legacy-registry",
        "source_key": source_key,
        "identity_type": identity_type,
        "identity_id": identity_id,
        "display_name": f"name-{source_key}",
        "retain_in_place": retain,
    }
    if scope is not None:
        row["scope"] = scope
    if actor is not None:
        row["actor"] = actor
    return row


def content_row(
    source_key: str,
    *,
    record_kind: str = "decision",
    scope: str | None = "proj-alpha",
    actor: str | None = "agent-kai",
    body: str = "original body",
) -> dict[str, object]:
    row: dict[str, object] = {
        "source_namespace": "legacy-memory",
        "source_key": source_key,
        "record_kind": record_kind,
        "body": body,
        "payload": {"statement": body},
    }
    if scope is not None:
        row["scope"] = scope
    if actor is not None:
        row["actor"] = actor
    return row


def test_valid_identity_rows_migrate_with_target_references() -> None:
    result = convert_identities(
        [identity_row("id-1"), identity_row("id-2", identity_id="44444444-4444-4444-8444-444444444444")],
        scope_map=SCOPE_MAP,
        actor_map=ACTOR_MAP,
    )

    assert result.counts == {OUTCOME_MIGRATED: 2}
    assert result.accounts_for(2)
    assert all(entry.target_reference for entry in result.entries)
    assert {entry.reason for entry in result.entries} == {None}
    assert {entry.disposition for entry in result.entries} == {"converted"}


def test_missing_source_key_is_quarantined_with_original_payload_and_hash() -> None:
    row = identity_row("")
    result = convert_identities([row], scope_map=SCOPE_MAP, actor_map=ACTOR_MAP)

    assert result.counts == {OUTCOME_QUARANTINED: 1}
    entry = result.entries[0]
    assert entry.reason == REASON_UNKNOWN_SOURCE_KEY
    assert entry.original_payload == row
    assert entry.payload_sha256 == hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert entry.disposition == "quarantine_review"
    assert entry.target_reference is None


def test_unmapped_scope_and_actor_are_quarantined_not_promoted_to_global() -> None:
    result = convert_identities(
        [
            identity_row("id-null-scope", scope="unknown-project"),
            identity_row("id-null-actor", actor="unknown-agent"),
            identity_row("id-absent-scope", scope=None),
        ],
        scope_map=SCOPE_MAP,
        actor_map=ACTOR_MAP,
    )

    reasons = [entry.reason for entry in result.entries]
    assert reasons == [
        REASON_NULL_SCOPE_MAPPING,
        REASON_NULL_ACTOR_MAPPING,
        REASON_NULL_SCOPE_MAPPING,
    ]
    assert result.counts == {OUTCOME_QUARANTINED: 3}
    assert all(entry.owner is None for entry in result.entries)


def test_duplicate_natural_identity_key_quarantines_second_row_only() -> None:
    first = identity_row("id-dup")
    second = identity_row("id-dup", identity_id="55555555-5555-4555-8555-555555555555")
    result = convert_identities([first, second], scope_map=SCOPE_MAP, actor_map=ACTOR_MAP)

    assert [entry.outcome for entry in result.entries] == [
        OUTCOME_MIGRATED,
        OUTCOME_QUARANTINED,
    ]
    assert result.entries[1].reason == REASON_DUPLICATE_IDENTITY
    assert result.entries[1].owner == ACTOR_MAP["agent-kai"]


def test_unsupported_identity_type_is_quarantined() -> None:
    result = convert_identities(
        [identity_row("id-x", identity_type="oauth-app")],
        scope_map=SCOPE_MAP,
        actor_map=ACTOR_MAP,
    )

    assert result.counts == {OUTCOME_QUARANTINED: 1}
    assert result.entries[0].reason == REASON_UNSUPPORTED_RECORD_KIND


def test_retained_rows_are_explicitly_retained() -> None:
    result = convert_identities(
        [identity_row("id-keep", retain=True)], scope_map=SCOPE_MAP, actor_map=ACTOR_MAP
    )

    assert result.counts == {OUTCOME_RETAINED: 1}
    assert result.entries[0].disposition == "retain_in_place"
    assert result.entries[0].target_reference is None


def test_rejected_rows_never_disappear_silently() -> None:
    result = convert_identities(
        [identity_row("id-bad", identity_id="not-a-uuid")],
        scope_map=SCOPE_MAP,
        actor_map=ACTOR_MAP,
    )

    assert result.counts == {OUTCOME_REJECTED: 1}
    assert result.entries[0].reason == "malformed_identity_id"
    assert result.entries[0].disposition == "reject_review"
    assert result.entries[0].original_payload["source_key"] == "id-bad"


@pytest.mark.parametrize("content_class", sorted(SUPPORTED_CONTENT_CLASSES))
def test_every_supported_content_class_converts(content_class: str) -> None:
    payload = {
        "decision": {"statement": "s"},
        "lesson": {"lesson": "l"},
        "knowledge": {"content": "k"},
        "progress": {"summary": "p"},
        "diary": {"entry": "d"},
        "message": {"role": "user", "text": "m"},
        "session": {"source": "cli", "message_count": 1},
        "artifact": {
            "media_type": "text/plain",
            "bytes_sha256": "ab" * 32,
            "location": "/tmp/x",
        },
        "work_product": {"kind": "patch", "summary": "w", "references": []},
    }[content_class]
    row = {
        "source_namespace": "legacy-memory",
        "source_key": f"c-{content_class}",
        "record_kind": content_class,
        "scope": "proj-alpha",
        "actor": "agent-kai",
        "body": "text",
        "payload": payload,
    }
    result = convert_content([row], scope_map=SCOPE_MAP, actor_map=ACTOR_MAP)

    assert result.counts == {OUTCOME_MIGRATED: 1}
    assert result.entries[0].content_class == content_class


def test_unsupported_content_kind_is_quarantined() -> None:
    result = convert_content(
        [content_row("c-weird", record_kind="mood-ring")],
        scope_map=SCOPE_MAP,
        actor_map=ACTOR_MAP,
    )

    assert result.counts == {OUTCOME_QUARANTINED: 1}
    assert result.entries[0].reason == REASON_UNSUPPORTED_RECORD_KIND


def test_content_payload_missing_required_typed_fields_is_quarantined() -> None:
    row = content_row("c-bad-payload")
    row["payload"] = {"unexpected": True}
    result = convert_content([row], scope_map=SCOPE_MAP, actor_map=ACTOR_MAP)

    assert result.counts == {OUTCOME_QUARANTINED: 1}
    assert result.entries[0].reason == "payload_schema_violation"


def test_empty_body_content_is_quarantined() -> None:
    result = convert_content(
        [content_row("c-empty", body="")], scope_map=SCOPE_MAP, actor_map=ACTOR_MAP
    )

    assert result.counts == {OUTCOME_QUARANTINED: 1}
    assert result.entries[0].reason == "empty_original_body"


def test_content_rows_account_for_every_input_row() -> None:
    rows = [
        content_row("c-1"),
        content_row("c-1"),
        content_row("c-2", record_kind="nope"),
        content_row("c-3", scope=None),
        content_row("c-4"),
    ]
    result = convert_content(rows, scope_map=SCOPE_MAP, actor_map=ACTOR_MAP)

    assert result.accounts_for(len(rows))
    assert result.counts == {
        OUTCOME_MIGRATED: 2,
        OUTCOME_QUARANTINED: 3,
    }
    assert [entry.source_reference for entry in result.entries] == [
        "legacy-memory:c-1",
        "legacy-memory:c-1",
        "legacy-memory:c-2",
        "legacy-memory:c-3",
        "legacy-memory:c-4",
    ]
