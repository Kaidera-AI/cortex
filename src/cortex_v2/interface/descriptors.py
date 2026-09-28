"""Descriptor list for W1's already-mounted routes in ``cortex_v2.app``.

These entries describe existing routes so every transport (HTTP schema, CLI,
MCP) shares one operation id/semantics table. Their handlers are ``None``:
they are served by the routes already mounted in ``app.py`` and must not be
re-mounted by the generic integrator. Paths, methods and request models are
read from ``app.py``/``cortex_v2.models`` as frozen by W1; this file does not
modify them.
"""

from __future__ import annotations

from typing import Any

from ..models import (
    BindScopeRequest,
    ContentSearchRequest,
    ContentStatusRequest,
    CreateContentRequest,
    CreateMemoryRecord,
    EnactRosterRequest,
    EnactWriterPolicyRequest,
    EnrollPrincipalRequest,
    IngestContentRequest,
    RecoverOwnerRequest,
    RegisterConnectorRequest,
    RenameScopeRequest,
    ReviseContentRequest,
    RevokeCredentialRequest,
    RevokeGrantRequest,
    RevokePrincipalRequest,
    RotateCredentialRequest,
    SearchRequest,
)

W1_MODULE = "identity_memory"

_IDEMPOTENCY_NOTE = (
    "Replay returns the stored receipt (Idempotent-Replay: true); reusing the "
    "key with a different body is a 409 idempotency_key_reused conflict."
)


def _descriptor(
    operation_id: str,
    method: str,
    path: str,
    *,
    kind: str,
    request_model: type | None = None,
    summary: str = "",
    authority: str | None = None,
    requires_scope: bool | None = None,
    requires_idempotency_key: bool | None = None,
    query_params: tuple[dict[str, Any], ...] = (),
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "operation_id": operation_id,
        "method": method,
        "path": path,
        "kind": kind,
        "request_model": request_model,
        "handler": None,
        "summary": summary,
        "usage": usage or {},
    }
    if authority is not None:
        entry["authority"] = authority
    if requires_scope is not None:
        entry["requires_scope"] = requires_scope
    if requires_idempotency_key is not None:
        entry["requires_idempotency_key"] = requires_idempotency_key
    if query_params:
        entry["query_params"] = query_params
    return entry


def w1_descriptors() -> list[dict[str, Any]]:
    return [
        _descriptor(
            "health.live",
            "GET",
            "/health/live",
            kind="scoped_read",
            authority="unauthenticated",
            requires_scope=False,
            summary="Liveness probe.",
            usage={
                "purpose": "Report that the API process is alive.",
                "use_when": "Container/orchestrator health checks.",
                "avoid_when": "You need database readiness; use health.ready.",
                "effects": "read-only",
                "cost_class": "trivial",
                "freshness": "live",
                "evidence_contract": "Proves process liveness only, not data.",
                "failure_codes": ("storage_unavailable",),
                "recovery_actions": ("restart the api process",),
                "examples": ({"request": {}, "response": {"status": "live"}},),
                "counterexamples": (
                    "Treating a live process as a ready database.",
                ),
            },
        ),
        _descriptor(
            "health.ready",
            "GET",
            "/health/ready",
            kind="scoped_read",
            authority="unauthenticated",
            requires_scope=False,
            summary="Readiness probe including the database.",
            usage={
                "purpose": "Report API readiness including a database check.",
                "use_when": "Before routing traffic to a candidate.",
                "avoid_when": "You only need process liveness.",
                "effects": "read-only",
                "cost_class": "trivial",
                "freshness": "live",
                "evidence_contract": "503 means not ready; never green-wash a "
                "database failure.",
                "failure_codes": ("storage_unavailable",),
                "recovery_actions": ("check the database container",),
                "examples": ({"response": {"status": "ready"}},),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "protocol.descriptor",
            "GET",
            "/v1/protocol",
            kind="scoped_read",
            authority="unauthenticated",
            requires_scope=False,
            summary="Minimal unauthenticated protocol descriptor.",
            usage={
                "purpose": "Report product/API version and deployment class "
                "without tenant details.",
                "use_when": "A client must verify compatibility before use.",
                "avoid_when": "You need capability details; use "
                "capability.discover.",
                "effects": "read-only",
                "cost_class": "trivial",
                "freshness": "live",
                "evidence_contract": "api_version pins the wire contract.",
                "failure_codes": (),
                "recovery_actions": ("upgrade the client to a supported "
                                     "api_version",),
                "examples": ({"response": {"api_version": "v1"}},),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "auth.principal",
            "GET",
            "/v1/auth/principal",
            kind="scoped_read",
            authority="authenticated",
            requires_scope=False,
            summary="Self profile of the authenticated principal.",
            usage={
                "purpose": "Show the caller's principal, installation and "
                "effective scope grants.",
                "use_when": "A worker must confirm which identity and scopes "
                "its credential actually carries.",
                "avoid_when": "You need another principal's grants; that is "
                "owner territory.",
                "effects": "read-only",
                "cost_class": "cheap",
                "freshness": "live",
                "evidence_contract": "Grants listed here are the authority "
                "for scope selection.",
                "failure_codes": ("invalid_credential",),
                "recovery_actions": ("rotate or re-enroll the credential",),
                "examples": ({"headers": {"Authorization": "Bearer <token>"}},),
                "counterexamples": (
                    "Assuming a project name from the alias alone grants "
                    "write access.",
                ),
            },
        ),
        _descriptor(
            "auth.enroll_principal",
            "POST",
            "/v1/auth/principals:enroll",
            kind="scoped_write",
            request_model=EnrollPrincipalRequest,
            authority="installation_owner",
            requires_scope=False,
            summary="Enroll a new principal and issue its credential once.",
            usage={
                "purpose": "Create an independent principal per worker/"
                "installation; the plaintext token is returned exactly once.",
                "use_when": "Onboarding KOS, kos-infra, connect, Kaidera, "
                "second-brain, OpenKai or any worker with its own identity.",
                "avoid_when": "A usable principal already exists; rotate its "
                "credential instead. Never share one administrator credential.",
                "effects": "writes identity state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "Typed receipt with token fingerprint; "
                + _IDEMPOTENCY_NOTE,
                "failure_codes": ("owner_authority_required",
                                  "idempotency_key_reused"),
                "recovery_actions": ("retry with the owner credential",
                                     "use a new idempotency key"),
                "examples": ({"payload": {"principal_name": "kos-infra",
                                          "actor_kind": "service",
                                          "scopes": []}},),
                "counterexamples": (
                    "Enrolling one principal and handing its token to every "
                    "worker.",
                ),
            },
        ),
        _descriptor(
            "auth.rotate_credential",
            "POST",
            "/v1/auth/credentials:rotate",
            kind="scoped_write",
            request_model=RotateCredentialRequest,
            authority="authenticated",
            requires_scope=False,
            summary="Rotate a credential; the new token is shown once.",
            usage={
                "purpose": "Replace a credential without changing identity.",
                "use_when": "On schedule, after leakage suspicion, or when a "
                "worker's token nears expiry.",
                "avoid_when": "The principal itself should be revoked.",
                "effects": "writes identity state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "Old credential stops working at commit; "
                + _IDEMPOTENCY_NOTE,
                "failure_codes": ("owner_authority_required",
                                  "invalid_credential"),
                "recovery_actions": ("self-rotate with the current token",),
                "examples": ({"payload": {"principal_id": None}},),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "auth.revoke_credential",
            "POST",
            "/v1/auth/credentials:revoke",
            kind="scoped_write",
            request_model=RevokeCredentialRequest,
            authority="authenticated",
            requires_scope=False,
            summary="Revoke one credential of a principal.",
            usage={
                "purpose": "Kill a single credential while the principal "
                "survives.",
                "use_when": "A specific token leaked or a device was retired.",
                "avoid_when": "The whole principal must go; use "
                "auth.revoke_principal.",
                "effects": "writes identity state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "In-flight calls with the revoked token "
                "fail from commit onward. " + _IDEMPOTENCY_NOTE,
                "failure_codes": ("owner_authority_required",
                                  "registry_target_not_found"),
                "recovery_actions": ("rotate instead if recovery is needed",),
                "examples": ({"payload": {"credential_id": "<uuid>"}},),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "auth.revoke_principal",
            "POST",
            "/v1/auth/principals:revoke",
            kind="scoped_write",
            request_model=RevokePrincipalRequest,
            authority="installation_owner",
            requires_scope=False,
            summary="Revoke a principal and all of its credentials.",
            usage={
                "purpose": "Retire a worker identity completely.",
                "use_when": "A worker is decommissioned.",
                "avoid_when": "Only one credential is compromised.",
                "effects": "writes identity state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": _IDEMPOTENCY_NOTE,
                "failure_codes": ("owner_authority_required",),
                "recovery_actions": ("re-enroll a new principal",),
                "examples": ({"payload": {"principal_id": "<uuid>"}},),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "auth.bind_scope",
            "POST",
            "/v1/auth/scopes:bind",
            kind="scoped_write",
            request_model=BindScopeRequest,
            authority="installation_owner",
            requires_scope=False,
            summary="Grant a principal read/write/publish on a scope.",
            usage={
                "purpose": "Bind scoped authority to an immutable principal.",
                "use_when": "A worker needs a project/shared/local scope.",
                "avoid_when": "The grant should be removed; use "
                "auth.revoke_scope_grant.",
                "effects": "writes grant state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "Grants are recorded with revision audit. "
                + _IDEMPOTENCY_NOTE,
                "failure_codes": ("owner_authority_required",
                                  "scope_not_found"),
                "recovery_actions": ("verify the scope alias first",),
                "examples": ({"payload": {"principal_id": "<uuid>",
                                          "alias": "proj-a",
                                          "can_read": True,
                                          "can_write": True}},),
                "counterexamples": (
                    "Granting can_publish to an ordinary reader of a shared "
                    "library.",
                ),
            },
        ),
        _descriptor(
            "auth.revoke_scope_grant",
            "POST",
            "/v1/auth/scopes:revoke",
            kind="scoped_write",
            request_model=RevokeGrantRequest,
            authority="installation_owner",
            requires_scope=False,
            summary="Revoke a principal's grant on one scope.",
            usage={
                "purpose": "Remove scoped authority without deleting identity.",
                "use_when": "A worker leaves a project.",
                "avoid_when": "The principal itself is being retired.",
                "effects": "writes grant state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": _IDEMPOTENCY_NOTE,
                "failure_codes": ("owner_authority_required",),
                "recovery_actions": (),
                "examples": ({"payload": {"principal_id": "<uuid>",
                                          "alias": "proj-a"}},),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "auth.recover_owner",
            "POST",
            "/v1/auth/owner:recover",
            kind="scoped_write",
            request_model=RecoverOwnerRequest,
            authority="recovery_token",
            requires_scope=False,
            requires_idempotency_key=True,
            summary="Recover installation ownership with the recovery token.",
            usage={
                "purpose": "Regain owner authority when owner credentials are "
                "lost; consumes the one-time recovery token.",
                "use_when": "Disaster recovery only.",
                "avoid_when": "An owner credential still works.",
                "effects": "writes identity state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": _IDEMPOTENCY_NOTE,
                "failure_codes": ("recovery_credential_invalid",),
                "recovery_actions": ("re-issue a recovery token from a "
                                     "prepared offline secret",),
                "examples": ({"payload": {"recovery_token": "<token>"}},),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "auth.privileged_actions",
            "GET",
            "/v1/auth/privileged-actions",
            kind="scoped_read",
            authority="installation_owner",
            requires_scope=False,
            summary="Audit ledger of privileged identity/registry actions.",
            query_params=(
                {"name": "limit", "schema": {"type": "integer", "minimum": 1,
                                             "maximum": 200},
                 "required": False},
            ),
            usage={
                "purpose": "Review who changed identity/grant state.",
                "use_when": "Auditing enrollments, revocations, renames.",
                "avoid_when": "You need content history; use content.inspect.",
                "effects": "read-only",
                "cost_class": "cheap",
                "freshness": "live",
                "evidence_contract": "Append-only ledger rows with actor and "
                "detail.",
                "failure_codes": ("owner_authority_required",),
                "recovery_actions": (),
                "examples": (),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "registry.rename_scope",
            "POST",
            "/v1/scopes/{alias}:rename",
            kind="scoped_write",
            request_model=RenameScopeRequest,
            authority="installation_owner",
            requires_scope=False,
            summary="Rename a scope alias without changing identity.",
            usage={
                "purpose": "Metadata-only rename; scope_id and all memory rows "
                "keep their identity.",
                "use_when": "A project is renamed.",
                "avoid_when": "You intend to merge or move data between "
                "scopes; that is a separate authorized migration.",
                "effects": "writes registry state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "Old alias resolution is preserved per "
                "the rename receipt. " + _IDEMPOTENCY_NOTE,
                "failure_codes": ("alias_reserved", "registry_conflict",
                                  "owner_authority_required"),
                "recovery_actions": ("pick an unreserved alias",),
                "examples": ({"path_params": {"alias": "proj-a"},
                              "payload": {"new_alias": "proj-b"}},),
                "counterexamples": (
                    "Rewriting historical observed labels after a rename.",
                ),
            },
        ),
        _descriptor(
            "registry.enact_roster",
            "POST",
            "/v1/scopes/{alias}/roster",
            kind="scoped_write",
            request_model=EnactRosterRequest,
            authority="installation_owner",
            requires_scope=False,
            summary="Enact a scope roster revision (roles/responsibilities).",
            usage={
                "purpose": "Publish the membership roster used by writer "
                "policy and responsibility routing.",
                "use_when": "Membership or responsibilities change.",
                "avoid_when": "You only need to read it; use "
                "registry.read_roster.",
                "effects": "writes registry state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "Roster revisions are append-only. "
                + _IDEMPOTENCY_NOTE,
                "failure_codes": ("owner_authority_required",
                                  "invalid_registry_request"),
                "recovery_actions": (),
                "examples": (),
                "counterexamples": (
                    "Zero or multiple responsibility owners for one "
                    "responsibility must fail, not be auto-fixed.",
                ),
            },
        ),
        _descriptor(
            "registry.enact_writer_policy",
            "POST",
            "/v1/scopes/{alias}/writer-policy",
            kind="scoped_write",
            request_model=EnactWriterPolicyRequest,
            authority="installation_owner",
            requires_scope=False,
            summary="Enact a writer-policy revision with optimistic locking.",
            usage={
                "purpose": "Control which roster roles may write to a scope.",
                "use_when": "Write policy must tighten or loosen.",
                "avoid_when": "You are granting scope access itself; use "
                "auth.bind_scope.",
                "effects": "writes policy state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "expected_revision guards concurrent "
                "changes. " + _IDEMPOTENCY_NOTE,
                "failure_codes": ("policy_revision_conflict",
                                  "owner_authority_required"),
                "recovery_actions": ("re-read the current revision and retry",),
                "examples": (),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "registry.read_roster",
            "GET",
            "/v1/scopes/{alias}/roster",
            kind="scoped_read",
            authority="scope_read",
            summary="Read the current roster of a scope.",
            usage={
                "purpose": "Show memberships, roles and responsibilities.",
                "use_when": "Routing work or checking write eligibility.",
                "avoid_when": "You need grant details of other principals "
                "beyond the roster.",
                "effects": "read-only",
                "cost_class": "cheap",
                "freshness": "live",
                "evidence_contract": "Roster revision identifies the exact "
                "enactment.",
                "failure_codes": ("scope_not_found",),
                "recovery_actions": (),
                "examples": (),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "registry.register_connector",
            "POST",
            "/v1/connectors",
            kind="scoped_write",
            request_model=RegisterConnectorRequest,
            authority="installation_owner",
            requires_scope=False,
            summary="Register a source connector namespace for ingestion.",
            usage={
                "purpose": "Give an ingestion source a stable identity "
                "independent of pathnames.",
                "use_when": "Before first use of an ingest connector "
                "namespace.",
                "avoid_when": "The namespace already exists.",
                "effects": "writes registry state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": _IDEMPOTENCY_NOTE,
                "failure_codes": ("connector_namespace_taken",
                                  "owner_authority_required"),
                "recovery_actions": ("reuse the registered namespace",),
                "examples": ({"payload": {"namespace": "openkai-sessions",
                                          "display_name": "OpenKai sessions",
                                          "connector_kind":
                                              "session_ingest"}},),
                "counterexamples": (
                    "Deriving writer identity from a file path instead of a "
                    "registered connector.",
                ),
            },
        ),
        _descriptor(
            "content.create",
            "POST",
            "/v1/content",
            kind="scoped_write",
            request_model=CreateContentRequest,
            authority="scope_write",
            summary="Create canonical content in the selected scope.",
            usage={
                "purpose": "Persist authored decision/lesson/knowledge/"
                "progress/diary/message/session/artifact/work-product "
                "content.",
                "use_when": "A worker authors new canonical content.",
                "avoid_when": "The content originates from an external "
                "source; use content.ingest for connector provenance.",
                "effects": "writes memory",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "Receipt carries content_id, revision 1 "
                "and content hash. " + _IDEMPOTENCY_NOTE,
                "failure_codes": ("scope_write_denied",
                                  "writer_policy_denied",
                                  "idempotency_key_reused"),
                "recovery_actions": ("check the active writer policy",),
                "examples": ({"payload": {"content_class": "decision",
                                          "payload": {},
                                          "body": "We chose X because Y."}},),
                "counterexamples": (
                    "Editing by re-creating; use content.revise to keep "
                    "lineage.",
                ),
            },
        ),
        _descriptor(
            "content.revise",
            "POST",
            "/v1/content/{content_id}/revisions",
            kind="scoped_write",
            request_model=ReviseContentRequest,
            authority="scope_write",
            summary="Append a new revision to existing content.",
            usage={
                "purpose": "Revise content without rewriting history; "
                "expected_revision gives optimistic locking.",
                "use_when": "Correcting or extending authored content.",
                "avoid_when": "The content is superseded/invalidated; "
                "restore it first.",
                "effects": "writes memory",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": _IDEMPOTENCY_NOTE,
                "failure_codes": ("revision_conflict", "content_not_found"),
                "recovery_actions": ("re-read the current revision and retry",),
                "examples": (),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "content.ingest",
            "POST",
            "/v1/content/ingest",
            kind="scoped_write",
            request_model=IngestContentRequest,
            authority="scope_write",
            requires_idempotency_key=False,
            summary="Ingest one source item with connector provenance.",
            usage={
                "purpose": "Preserve an external source item under a "
                "registered connector namespace and stable source key.",
                "use_when": "Single-item connector ingestion.",
                "avoid_when": "Ingesting whole sessions/transcripts; use the "
                "ingest.* connector operations for atomic replacement.",
                "effects": "writes memory",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "Idempotency is derived from "
                "(connector namespace, source key): the same source replays, "
                "a changed body under the same key is a 409 "
                "source_key_conflict.",
                "failure_codes": ("source_key_conflict",
                                  "connector_not_found"),
                "recovery_actions": ("register the connector first",
                                     "use a new source key or generation"),
                "examples": (),
                "counterexamples": (
                    "Re-uploading a changed file under the same source key "
                    "and expecting a silent overwrite.",
                ),
            },
        ),
        _descriptor(
            "content.invalidate",
            "POST",
            "/v1/content/{content_id}:invalidate",
            kind="scoped_write",
            request_model=ContentStatusRequest,
            authority="scope_write",
            summary="Invalidate content; retrieval excludes it, history "
            "keeps it.",
            usage={
                "purpose": "Mark content invalid without deleting the "
                "original.",
                "use_when": "A decision/lesson is known to be wrong.",
                "avoid_when": "A successor exists; use content.supersede.",
                "effects": "transitions lifecycle state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": _IDEMPOTENCY_NOTE,
                "failure_codes": ("content_not_found",
                                  "invalid_status_transition"),
                "recovery_actions": ("content.restore reverses it",),
                "examples": (),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "content.restore",
            "POST",
            "/v1/content/{content_id}:restore",
            kind="scoped_write",
            request_model=ContentStatusRequest,
            authority="scope_write",
            summary="Restore invalidated content to current.",
            usage={
                "purpose": "Undo an invalidation explicitly.",
                "use_when": "The invalidation was mistaken.",
                "avoid_when": "The content was superseded by newer truth.",
                "effects": "transitions lifecycle state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": _IDEMPOTENCY_NOTE,
                "failure_codes": ("invalid_status_transition",),
                "recovery_actions": (),
                "examples": (),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "content.supersede",
            "POST",
            "/v1/content/{content_id}:supersede",
            kind="scoped_write",
            request_model=ContentStatusRequest,
            authority="scope_write",
            summary="Supersede content with an explicit successor.",
            usage={
                "purpose": "Replace content while preserving lineage to the "
                "successor.",
                "use_when": "A newer revision of the same fact exists.",
                "avoid_when": "No successor exists yet.",
                "effects": "transitions lifecycle state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": "successor_content_id is mandatory. "
                + _IDEMPOTENCY_NOTE,
                "failure_codes": ("successor_required", "content_not_found"),
                "recovery_actions": (),
                "examples": (),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "content.tombstone",
            "POST",
            "/v1/content/{content_id}:tombstone",
            kind="scoped_write",
            request_model=ContentStatusRequest,
            authority="scope_write",
            summary="Tombstone content; prevents resurrection.",
            usage={
                "purpose": "Terminal lifecycle state for retention/legal "
                "removal while keeping the tombstone.",
                "use_when": "Retention demands removal of the payload from "
                "active use.",
                "avoid_when": "Invalidation or supersession suffices.",
                "effects": "transitions lifecycle state",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": _IDEMPOTENCY_NOTE,
                "failure_codes": ("invalid_status_transition",),
                "recovery_actions": (),
                "examples": (),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "content.inspect",
            "GET",
            "/v1/content/{content_id}",
            kind="scoped_read",
            authority="scope_read",
            summary="Inspect canonical content, a revision, or its history.",
            query_params=(
                {"name": "revision",
                 "schema": {"type": "integer", "minimum": 1},
                 "required": False},
                {"name": "history", "schema": {"type": "boolean"},
                 "required": False},
            ),
            usage={
                "purpose": "Verify the original behind a citation: body, "
                "payload, hashes, provenance and status.",
                "use_when": "Before relying on any retrieved hit "
                "(evidence.inspect workflow).",
                "avoid_when": "You only need candidates; search first.",
                "effects": "read-only",
                "cost_class": "cheap",
                "freshness": "live",
                "evidence_contract": "Returns exact revision content with "
                "content hash and status.",
                "failure_codes": ("content_not_found", "scope_denied"),
                "recovery_actions": ("widen X-Cortex-Read-Scopes if "
                                     "authorized",),
                "examples": ({"query": {"revision": 2}},),
                "counterexamples": (
                    "Trusting a search snippet without inspecting the "
                    "original for load-bearing claims.",
                ),
            },
        ),
        _descriptor(
            "content.search.lexical",
            "POST",
            "/v1/content/searches",
            kind="scoped_read",
            request_model=ContentSearchRequest,
            authority="scope_read",
            summary="Lexical search over canonical content.",
            usage={
                "purpose": "Bounded lexical retrieval over content classes "
                "with explicit degradation.",
                "use_when": "Known-item, identifier-heavy or keyword recall.",
                "avoid_when": "You need semantic/hybrid ranking before the "
                "retrieval module is deployed; the response says so via "
                "degraded[].",
                "effects": "read-only",
                "cost_class": "moderate",
                "freshness": "live",
                "evidence_contract": "Hits cite content_id/revision; "
                "degraded[] lists unavailable stages.",
                "failure_codes": ("scope_not_found",),
                "recovery_actions": ("narrow the query", "reduce read scopes"),
                "examples": ({"payload": {"query": "handoff claim",
                                          "read_scopes": ["proj-a"],
                                          "limit": 10}},),
                "counterexamples": (
                    "Reading an empty hit list as proof of absence.",
                ),
            },
        ),
        _descriptor(
            "memory.record",
            "POST",
            "/v1/memory/records",
            kind="scoped_write",
            request_model=CreateMemoryRecord,
            authority="scope_write",
            summary="Record a typed memory (decision/lesson/knowledge/note).",
            usage={
                "purpose": "Durable structured memory with a typed receipt.",
                "use_when": "A worker records a known decision, lesson or "
                "knowledge item.",
                "avoid_when": "The material is external source content; use "
                "content.ingest or ingest.* connectors.",
                "effects": "writes memory",
                "cost_class": "cheap",
                "freshness": "committed",
                "evidence_contract": _IDEMPOTENCY_NOTE,
                "failure_codes": ("scope_write_denied",
                                  "idempotency_key_reused"),
                "recovery_actions": (),
                "examples": ({"payload": {"record_type": "decision",
                                          "body": "Chose asyncpg."}},),
                "counterexamples": (
                    "Re-running broad search just to look busy before "
                    "recording.",
                ),
            },
        ),
        _descriptor(
            "memory.inspect",
            "GET",
            "/v1/memory/records/{record_id}",
            kind="scoped_read",
            authority="scope_read",
            summary="Inspect the latest revision of a memory record.",
            usage={
                "purpose": "Read the canonical record body and revision.",
                "use_when": "Verifying a memory hit before citing it.",
                "avoid_when": "You need content-class lineage; use "
                "content.inspect.",
                "effects": "read-only",
                "cost_class": "cheap",
                "freshness": "live",
                "evidence_contract": "Latest revision only; older revisions "
                "stay in history.",
                "failure_codes": ("record_not_found",),
                "recovery_actions": (),
                "examples": (),
                "counterexamples": (),
            },
        ),
        _descriptor(
            "memory.search.lexical",
            "POST",
            "/v1/searches",
            kind="scoped_read",
            request_model=SearchRequest,
            authority="scope_read",
            summary="Lexical search over memory records.",
            usage={
                "purpose": "Bounded lexical recall over decision/lesson/"
                "knowledge/note records.",
                "use_when": "First-turn recall and known-item memory lookups.",
                "avoid_when": "Vector/hybrid retrieval is required; the "
                "retrieval module owns memory.search when deployed.",
                "effects": "read-only",
                "cost_class": "moderate",
                "freshness": "live",
                "evidence_contract": "degraded[] names every unavailable "
                "stage honestly.",
                "failure_codes": ("scope_not_found",),
                "recovery_actions": (),
                "examples": ({"payload": {"query": "retry policy",
                                          "limit": 10}},),
                "counterexamples": (
                    "Treating rank order as factual confidence.",
                ),
            },
        ),
    ]
