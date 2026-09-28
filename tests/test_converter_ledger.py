"""Synthetic v1 rows matching public.handoffs/decisions DDL fields."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest

from cortex_v2.converter import ConversionPolicy, classify_row
from cortex_v2.converter_cli import _socket_dsn

SCOPE = uuid.UUID("a96570de-c066-452b-9696-e2377e8e6551")
PRINCIPAL = uuid.UUID("5dd378bd-e1de-465e-8240-a02b95426446")
INSTALLATION = uuid.UUID("247c715c-a845-4d83-8504-be0c3b536b47")
SOURCE_PROJECT = uuid.UUID("fb9fa792-5fd0-4a39-8e27-0eb4de7e844d")
SOURCE_OWNER = uuid.UUID("cad15a18-b3a9-49c9-8130-2d81e9d21d0c")


def policy_bytes(
    *, handoff_status: dict[str, str] | None = None, provenance: bool = True,
) -> bytes:
    return json.dumps(
        {
            "version": "synthetic-v1",
            "installation_id": str(INSTALLATION),
            "projects": {"fixture-project": {
                "scope_id": str(SCOPE),
                "principal_id": str(PRINCIPAL),
                "source_provenance": {
                    "project_id": str(SOURCE_PROJECT),
                    "owner_id": str(SOURCE_OWNER),
                    "installation_id": "synthetic-source-installation",
                } if provenance else None,
            }},
            "handoff_status": handoff_status or {"pending": "open"},
            "entities": {
                "public.decisions": "convert",
                "public.handoffs": "convert",
                "public.messages": "convert",
                "public.agent_diaries": "convert",
                "public.agent_sessions": "convert",
                "public.artifacts": "quarantine:missing_original",
                "cortex_auth.tokens": "skip:retained_legacy_credentials",
            },
        },
        sort_keys=True,
    ).encode()


def test_policy_fingerprint_binds_lifecycle_mapping() -> None:
    first = ConversionPolicy.from_bytes(policy_bytes())
    second = ConversionPolicy.from_bytes(policy_bytes(handoff_status={
        "pending": "open", "released": "open"
    }))
    assert first.sha256 != second.sha256


def test_literal_project_label_without_source_provenance_is_quarantined() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    decision = classify_row("public.decisions", {
        "id": uuid.uuid4(), "project": "fixture-project",
        "summary": "Synthetic scoped decision",
    }, policy)
    assert (decision.outcome, decision.reason) == (
        "quarantined", "unverified_scope_provenance",
    )


def test_matching_label_and_id_without_owner_proof_are_quarantined() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes(provenance=False))
    decision = classify_row("public.decisions", {
        "id": uuid.uuid4(), "project": "fixture-project",
        "project_id": SOURCE_PROJECT, "summary": "Unverified owner",
    }, policy)
    assert (decision.outcome, decision.reason) == (
        "quarantined", "unverified_scope_provenance",
    )


def test_canonical_memory_and_pending_handoff_keep_source_identity() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    decision_id = uuid.uuid4()
    handoff_id = uuid.uuid4()
    decision = classify_row("public.decisions", {
        "id": decision_id, "project": "fixture-project",
        "project_id": SOURCE_PROJECT, "summary": "Synthetic choice",
        "agent_name": "fixture-writer",
    }, policy)
    handoff = classify_row("public.handoffs", {
        "id": handoff_id, "project": "fixture-project",
        "project_id": SOURCE_PROJECT, "summary": "Synthetic relay",
        "from_agent": "fixture-writer", "to_role": "implementer", "status": "pending",
    }, policy)
    assert (
        decision.outcome, decision.target_id, decision.target_relation, decision.body
    ) == (
        "migrated", decision_id, "cortex_core.content_items", "Synthetic choice"
    )
    assert (handoff.outcome, handoff.target_id, handoff.status) == (
        "migrated", handoff_id, "open"
    )
    assert handoff.scope_id == decision.scope_id == SCOPE


def test_ambiguous_claim_and_unknown_scope_are_quarantined_not_rewritten() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    claim = classify_row("public.handoffs", {
        "id": uuid.uuid4(), "project": "fixture-project",
        "project_id": SOURCE_PROJECT, "summary": "Pending claim",
        "from_agent": "fixture", "to_role": "implementer", "status": "claimed",
    }, policy)
    unknown = classify_row("public.decisions", {
        "id": uuid.uuid4(), "project": "other-project", "summary": "No owner",
    }, policy)
    assert (claim.outcome, claim.reason, claim.target_id) == (
        "quarantined", "ambiguous_lifecycle", None
    )
    assert (unknown.outcome, unknown.reason, unknown.target_id) == (
        "quarantined", "unmapped_scope", None
    )


def test_secret_namespace_never_returns_canonical_body() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    outcome = classify_row("cortex_auth.tokens", {
        "id": uuid.uuid4(), "token_hash": "synthetic-sentinel"
    }, policy)
    assert (outcome.outcome, outcome.reason, outcome.body) == (
        "skipped", "retained_legacy_credentials", None
    )
    assert "synthetic-sentinel" not in repr(outcome)


def test_policy_rejects_claimed_mapping_without_expiring_lease() -> None:
    with pytest.raises(ValueError, match="unsafe handoff mapping"):
        ConversionPolicy.from_bytes(policy_bytes(handoff_status={"claimed": "claimed"}))


def test_legacy_message_and_diary_become_typed_content_without_fake_authors() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    message = classify_row("public.messages", {
        "id": 17, "project": "fixture-project", "project_id": SOURCE_PROJECT,
        "role": "human", "content": "Synthetic message",
    }, policy)
    diary = classify_row("public.agent_diaries", {
        "id": uuid.uuid4(), "project": "fixture-project",
        "project_id": SOURCE_PROJECT, "agent_name": "synthetic-agent",
        "summary": "Synthetic diary",
    }, policy)
    assert (message.outcome, message.content_class, message.payload["role"]) == (
        "migrated", "message", "user",
    )
    assert (diary.outcome, diary.content_class, diary.payload["entry"]) == (
        "migrated", "diary", "Synthetic diary",
    )


def test_artifact_without_original_is_quarantined_not_hashed_from_path() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    artifact = classify_row("public.artifacts", {
        "id": uuid.uuid4(), "project": "fixture-project",
        "source_file": "a-fixture-file", "raw_content": None,
        "content_hash": "legacy-unverified-file-hash",
    }, policy)
    assert (artifact.outcome, artifact.reason) == (
        "quarantined", "missing_original",
    )


def test_converter_cli_rejects_tcp_and_other_database_sockets(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    socket = private / "sockets"
    socket.mkdir(mode=0o700)
    valid = (
        f"postgresql:///legacy_restore?host={quote(str(socket), safe='')}"
        "&port=55441&user=source_read"
    )
    assert _socket_dsn(valid.encode(), "legacy_restore", private) == {
        "host": str(socket),
        "port": 55441,
        "user": "source_read",
        "database": "legacy_restore",
    }
    for dsn in (
        "postgresql://example.invalid/legacy_restore?port=5432&user=source_read",
        valid.replace("legacy_restore", "production"),
        valid.replace(quote(str(socket), safe=""), quote(str(private.parent), safe="")),
    ):
        with pytest.raises(ValueError):
            _socket_dsn(dsn.encode(), "legacy_restore", private)


def test_unapproved_source_entity_fails_closed_before_quarantine() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    with pytest.raises(ValueError, match="missing.*entity"):
        classify_row("public.new_source_table", {
            "id": uuid.uuid4(), "project": "fixture-project", "summary": "new",
        }, policy)


def test_handoff_detail_preserved_but_unmapped_reply_is_not_fabricated() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    handoff = {
        "id": uuid.uuid4(), "project": "fixture-project",
        "project_id": SOURCE_PROJECT, "summary": "\nSynthetic handoff title",
        "next_steps": "Inspect output",
        "context": "Synthetic context", "to_role": "implementer",
        "status": "pending",
    }
    mapped = classify_row("public.handoffs", handoff, policy)
    assert mapped.payload["title"] == "Synthetic handoff title"
    assert mapped.body == (
        "\nSynthetic handoff title\n\nNext steps:\nInspect output"
        "\n\nContext:\nSynthetic context"
    )
    reply = classify_row(
        "public.handoffs",
        {**handoff, "reply_to_handoff_id": uuid.uuid4()}, policy,
    )
    assert (reply.outcome, reply.reason) == (
        "quarantined", "unmapped_handoff_link",
    )


def test_session_notes_are_quarantined_until_classified() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    session = classify_row("public.agent_sessions", {
        "id": uuid.uuid4(), "project": "fixture-project",
        "project_id": SOURCE_PROJECT, "task": "Synthetic task",
        "message_count": 2,
        "notes": {"unreviewed": "synthetic-text"},
    }, policy)
    assert (session.outcome, session.reason) == (
        "quarantined", "unreviewed_session_notes",
    )


def test_memory_rationale_survives_conversion_in_read_projection_body() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    decision = classify_row("public.decisions", {
        "id": uuid.uuid4(), "project": "fixture-project",
        "project_id": SOURCE_PROJECT, "summary": "Synthetic decision",
        "rationale": "Synthetic reasoning",
    }, policy)
    assert decision.body == "Synthetic decision\n\nRationale:\nSynthetic reasoning"
    assert decision.payload["statement"] == decision.body
