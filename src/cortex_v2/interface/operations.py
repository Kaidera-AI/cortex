"""OPERATIONS published by the Context & worker interface module.

Same integration contract as every module: a list of dicts with
``operation_id``, ``method``, ``path``, ``kind``, ``request_model``,
``handler``, ``summary`` and ``usage``. The integrator mounts these routes
generically; nothing here mounts itself into ``app.py``.
"""

from __future__ import annotations

from typing import Any

from ..agent_boot_models import BootAgentBindRequest, BootEntryBindRequest, BootPublicationRequest
from .agent_boot_enact import enact_boot_agent, enact_boot_entry, publish_boot_catalogue

from .context import (
    bind_skills,
    enact_persona,
    enact_rule,
    enact_skill,
    get_bindings,
    get_skill,
    prepare_context,
)
from .discovery import capability_card, discover_capabilities
from .harness import apply_mirror, drift_check, preview_mirror, rollback_mirror
from .models import (
    ContextPrepareRequest,
    HarnessApplyRequest,
    HarnessDriftRequest,
    HarnessPreviewRequest,
    HarnessRollbackRequest,
    PersonaEnactRequest,
    RuleEnactRequest,
    SkillBindRequest,
    SkillEnactRequest,
    SkillGetQuery,
)
from .profiles import compatibility_profiles

_IDEMPOTENCY_NOTE = (
    "Replay returns the stored receipt (Idempotent-Replay: true); reusing the "
    "key with a different body is a 409 idempotency_key_reused conflict."
)


async def _compatibility_profiles(
    connection: Any, context: Any, payload: Any, path_params: dict[str, Any]
) -> dict[str, Any]:
    return compatibility_profiles()


OPERATIONS: list[dict[str, Any]] = [
    {
        "operation_id": "capability.discover",
        "method": "GET",
        "path": "/v1/capabilities",
        "kind": "scoped_read",
        "request_model": None,
        "handler": discover_capabilities,
        "summary": "Permission-filtered capability discovery with module and "
        "worker-role availability.",
        "usage": {
            "purpose": "Learn which operations actually work here, with "
            "state, cost and links to full usage cards.",
            "use_when": "At task start, and whenever a previously ready "
            "capability reported unavailable/degraded.",
            "avoid_when": "You already hold a fresh card for the exact "
            "operation you need.",
            "effects": "read-only",
            "prerequisites": ("authenticated v2 principal", "selected scope"),
            "cost_class": "cheap",
            "freshness": "live; cache by registry digest",
            "evidence_contract": "registry_version/registry_digest pin the "
            "exact operation set; unavailable modules carry reasons.",
            "failure_codes": ("invalid_credential", "scope_required",
                              "scope_not_found"),
            "recovery_actions": ("re-authenticate", "select a readable scope"),
            "examples": ({"headers": {"X-Cortex-Scope": "proj-a"}},),
            "counterexamples": (
                "Loading all 100+ card details at boot instead of the few "
                "relevant summaries.",
            ),
            "worker_roles": ("doc", "embed", "graph"),
        },
    },
    {
        "operation_id": "capability.card",
        "method": "GET",
        "path": "/v1/capabilities/{operation_id}",
        "kind": "scoped_read",
        "request_model": None,
        "handler": capability_card,
        "summary": "One full usage card: when to use/avoid, cost, freshness "
        "and evidence contract.",
        "usage": {
            "purpose": "Fetch the complete contract of one operation on "
            "demand.",
            "use_when": "Before first use of a specialized operation.",
            "avoid_when": "The discovery summary already answers the "
            "question.",
            "effects": "read-only",
            "prerequisites": ("entitlement to the operation",),
            "cost_class": "cheap",
            "freshness": "live",
            "evidence_contract": "Card fields are versioned product "
            "metadata, not model-generated instructions.",
            "failure_codes": ("operation_not_found",),
            "recovery_actions": ("run capability.discover first",),
            "examples": ({"path_params": {"operation_id": "context.prepare"}},),
            "counterexamples": (
                "Treating tool annotations as authorization.",
            ),
        },
    },
    {
        "operation_id": "context.prepare",
        "method": "POST",
        "path": "/v1/context/prepare",
        "kind": "scoped_write",
        "request_model": ContextPrepareRequest,
        "handler": prepare_context,
        "summary": "Budgeted task context: identity, mandatory policy, "
        "provenance, recall, recommendations and next work.",
        "usage": {
            "purpose": "One budgeted boot/prepare giving a worker who it is, "
            "the mandatory rules that always survive, relevant memory, "
            "recommended capabilities and actionable next work.",
            "use_when": "At task start (boot) and after a material change of "
            "task or scope.",
            "avoid_when": "You only need one specific record; inspect or "
            "search directly.",
            "effects": "writes a preparation receipt (append-only)",
            "prerequisites": (
                "selected scope",
                "persona/rule/skill revisions enacted for full value",
            ),
            "cost_class": "moderate",
            "freshness": "live at preparation time; the receipt pins "
            "registry/policy/rule/skill revisions",
            "evidence_contract": "Provenance names persona/rule/skill "
            "revisions, writer-policy revision and registry digest; "
            "truncations are explicit; mandatory policy is never trimmed.",
            "failure_codes": ("budget_exhausted", "writer_policy_denied",
                              "scope_write_denied",
                              "idempotency_key_reused"),
            "recovery_actions": (
                "increase budget_bytes or reduce optional recall",
                "check the active writer policy",
            ),
            "examples": (
                {"payload": {"intent": "code_change",
                             "repository_ref": "helix",
                             "change_ref": "diff-123",
                             "budget_bytes": 32768}},
            ),
            "counterexamples": (
                "Passing free-form task text and treating it as an "
                "instruction that can override policy.",
                "Reducing the budget until mandatory rules disappear — that "
                "is an explicit error, not a smaller persona.",
            ),
            "worker_roles": (),
        },
    },
    {
        "operation_id": "persona.enact",
        "method": "POST",
        "path": "/v1/context/personas",
        "kind": "scoped_write",
        "request_model": PersonaEnactRequest,
        "handler": enact_persona,
        "summary": "Enact a new persona revision with provenance.",
        "usage": {
            "purpose": "Pin the persona template used by context preparation "
            "and harness mirrors.",
            "use_when": "Persona text or template version changes.",
            "avoid_when": "A current revision already says this; revisions "
            "are append-only history.",
            "effects": "writes context input state",
            "prerequisites": ("write access to the scope",),
            "cost_class": "cheap",
            "freshness": "committed",
            "evidence_contract": "Receipt pins persona_id/revision and body "
            "hash. " + _IDEMPOTENCY_NOTE,
            "failure_codes": ("writer_policy_denied", "payload_too_large"),
            "recovery_actions": (),
            "examples": (),
            "counterexamples": (),
        },
    },
    {
        "operation_id": "rule.enact",
        "method": "POST",
        "path": "/v1/context/rules",
        "kind": "scoped_write",
        "request_model": RuleEnactRequest,
        "handler": enact_rule,
        "summary": "Enact a rule revision (mandatory or optional).",
        "usage": {
            "purpose": "Publish rules that context preparation must preserve; "
            "mandatory rules survive every budget that fits at all.",
            "use_when": "A safety or acceptance rule changes.",
            "avoid_when": "The material is task memory, not policy; record "
            "it as memory instead.",
            "effects": "writes context input state",
            "prerequisites": ("write access to the scope",),
            "cost_class": "cheap",
            "freshness": "committed",
            "evidence_contract": "Slug identity is immutable across "
            "revisions. " + _IDEMPOTENCY_NOTE,
            "failure_codes": ("slug_conflict", "writer_policy_denied"),
            "recovery_actions": (),
            "examples": ({"payload": {"slug": "no-force-push",
                                      "obligation": "mandatory",
                                      "body": "Never force-push."}},),
            "counterexamples": (
                "Demoting a mandatory safety rule to optional to fit a "
                "budget.",
            ),
        },
    },
    {
        "operation_id": "skill.enact",
        "method": "POST",
        "path": "/v1/context/skills",
        "kind": "scoped_write",
        "request_model": SkillEnactRequest,
        "handler": enact_skill,
        "summary": "Enact a skill revision (body fetched on demand).",
        "usage": {
            "purpose": "Publish a skill whose body workers fetch on demand "
            "via skill.get.",
            "use_when": "Skill content changes or a new skill lands.",
            "avoid_when": "The skill is only relevant to one task; put it in "
            "memory instead.",
            "effects": "writes context input state",
            "prerequisites": ("write access to the scope",),
            "cost_class": "cheap",
            "freshness": "committed",
            "evidence_contract": _IDEMPOTENCY_NOTE,
            "failure_codes": ("slug_conflict", "writer_policy_denied"),
            "recovery_actions": (),
            "examples": (),
            "counterexamples": (
                "Stuffing every skill body into boot context instead of "
                "references.",
            ),
        },
    },
    {
        "operation_id": "skill.bind",
        "method": "POST",
        "path": "/v1/context/skill-bindings",
        "kind": "scoped_write",
        "request_model": SkillBindRequest,
        "handler": bind_skills,
        "summary": "Replace the scope's skill bindings with optimistic "
        "locking.",
        "usage": {
            "purpose": "Choose which skill revisions the scope offers, with "
            "precedence.",
            "use_when": "On-demand skill selection changes.",
            "avoid_when": "You need a new skill body; enact it first.",
            "effects": "writes context input state",
            "prerequisites": ("enacted skill revisions",),
            "cost_class": "cheap",
            "freshness": "committed",
            "evidence_contract": "expected_revision guards concurrent "
            "rebinding. " + _IDEMPOTENCY_NOTE,
            "failure_codes": ("binding_revision_conflict",
                              "duplicate_precedence", "skill_not_found",
                              "skill_revision_not_found"),
            "recovery_actions": ("re-read the binding revision and retry",),
            "examples": (),
            "counterexamples": (),
        },
    },
    {
        "operation_id": "skill.get",
        "method": "GET",
        "path": "/v1/context/skills/{skill_id}",
        "kind": "scoped_read",
        "request_model": SkillGetQuery,
        "handler": get_skill,
        "summary": "Fetch one skill body on demand (optional revision).",
        "usage": {
            "purpose": "Load a skill body exactly when the task needs it.",
            "use_when": "The prepared context referenced a skill whose "
            "when_to_use matches the task.",
            "avoid_when": "The when_to_use does not match; do not preload "
            "every skill.",
            "effects": "read-only",
            "prerequisites": ("a bound or enacted skill revision",),
            "cost_class": "cheap",
            "freshness": "live",
            "evidence_contract": "Returns the exact revision body with its "
            "slug and provenance.",
            "failure_codes": ("skill_not_found",),
            "recovery_actions": ("use the revision pinned in the context "
                                 "provenance",),
            "examples": (),
            "counterexamples": (),
        },
    },
    {
        "operation_id": "skill.bindings",
        "method": "GET",
        "path": "/v1/context/skill-bindings",
        "kind": "scoped_read",
        "request_model": None,
        "handler": get_bindings,
        "summary": "Read the current skill-binding set and its optimistic "
        "lock revision.",
        "usage": {
            "purpose": "Expose the binding revision so skill.bind callers can "
            "re-read and retry after a binding_revision_conflict.",
            "use_when": "Before rebinding skills, and after any 409 "
            "binding_revision_conflict.",
            "avoid_when": "You only need one skill body; use skill.get.",
            "effects": "read-only",
            "prerequisites": ("selected scope",),
            "cost_class": "cheap",
            "freshness": "live",
            "evidence_contract": "Returns the exact revision to pass as "
            "expected_revision, with the bound skill revisions.",
            "failure_codes": ("scope_not_found",),
            "recovery_actions": (),
            "examples": (),
            "counterexamples": (
                "Blindly retrying skill.bind with a guessed revision.",
            ),
        },
    },
    {
        "operation_id": "harness.preview",
        "method": "POST",
        "path": "/v1/harness/previews",
        "kind": "scoped_read",
        "request_model": HarnessPreviewRequest,
        "handler": preview_mirror,
        "summary": "Deterministic preview of mirror bytes for pinned "
        "revisions.",
        "usage": {
            "purpose": "Compute the exact harness mirror bytes and manifest "
            "without committing a generation.",
            "use_when": "Before applying a mirror, or to verify "
            "reproducibility from pinned revisions.",
            "avoid_when": "You need the committed generation record; use "
            "harness.apply.",
            "effects": "read-only",
            "prerequisites": ("enacted persona/rule/skill revisions for "
                              "non-empty output",),
            "cost_class": "cheap",
            "freshness": "computed from current (or pinned) revisions",
            "evidence_contract": "Same pins → identical bytes and "
            "manifest_sha256; provenance lists every input revision.",
            "failure_codes": ("pinned_revision_not_found", "mirror_too_large"),
            "recovery_actions": ("drop or correct the pin",),
            "examples": ({"payload": {"mirror_label": "kos-worktree",
                                      "pins": {}}},),
            "counterexamples": (
                "Hand-editing generated files afterwards; the next apply "
                "will refuse.",
            ),
        },
    },
    {
        "operation_id": "harness.apply",
        "method": "POST",
        "path": "/v1/harness/mirrors",
        "kind": "scoped_write",
        "request_model": HarnessApplyRequest,
        "handler": apply_mirror,
        "summary": "Commit a mirror generation; refuses hand-edited mirrors.",
        "usage": {
            "purpose": "Persist a new mirror generation with provenance and "
            "advance the active pointer.",
            "use_when": "Regenerating a harness mirror from pinned inputs.",
            "avoid_when": "The host copy has drifted; roll back or "
            "reconcile first.",
            "effects": "writes harness state",
            "prerequisites": (
                "observed manifest from the host when a mirror exists",
            ),
            "cost_class": "moderate",
            "freshness": "committed",
            "evidence_contract": "Receipt carries generation, manifest hash, "
            "file hashes and input provenance. " + _IDEMPOTENCY_NOTE,
            "failure_codes": ("harness_drift_detected",
                              "observed_manifest_required",
                              "writer_policy_denied"),
            "recovery_actions": ("harness.rollback",
                                 "re-run harness.preview and compare"),
            "examples": (),
            "counterexamples": (
                "Applying over a hand-edited mirror without acknowledging "
                "the edit — it is a typed 409, never a silent overwrite.",
            ),
        },
    },
    {
        "operation_id": "harness.drift",
        "method": "POST",
        "path": "/v1/harness/drift-checks",
        "kind": "scoped_read",
        "request_model": HarnessDriftRequest,
        "handler": drift_check,
        "summary": "Compare host-observed file hashes with the active "
        "generation.",
        "usage": {
            "purpose": "Detect hand edits, missing and untracked files "
            "against the active mirror generation.",
            "use_when": "Before applying, and periodically on managed hosts.",
            "avoid_when": "No generation was ever applied.",
            "effects": "read-only",
            "prerequisites": ("an applied mirror generation",),
            "cost_class": "cheap",
            "freshness": "live classification of the supplied manifest",
            "evidence_contract": "Per-file states: match, hand_edited, "
            "missing, untracked.",
            "failure_codes": ("mirror_not_found",),
            "recovery_actions": ("harness.rollback to restore generated "
                                 "bytes",),
            "examples": (),
            "counterexamples": (),
        },
    },
    {
        "operation_id": "harness.rollback",
        "method": "POST",
        "path": "/v1/harness/rollbacks",
        "kind": "scoped_write",
        "request_model": HarnessRollbackRequest,
        "handler": rollback_mirror,
        "summary": "Roll a mirror back to an earlier generation, including "
        "removal of newly introduced files.",
        "usage": {
            "purpose": "Restore an earlier generation as a new forward "
            "generation; the plan lists files to restore and files "
            "introduced since the target that must be removed.",
            "use_when": "After drift, a bad pin, or a failed host sync.",
            "avoid_when": "The active generation is the one you want.",
            "effects": "writes harness state",
            "prerequisites": ("an earlier applied generation",),
            "cost_class": "moderate",
            "freshness": "committed",
            "evidence_contract": "Receipt carries restore/remove plan with "
            "bodies and hashes; rolled_back_from/to are recorded. "
            + _IDEMPOTENCY_NOTE,
            "failure_codes": ("mirror_not_found", "no_rollback_target",
                              "generation_not_found"),
            "recovery_actions": ("choose an existing target_generation",),
            "examples": (),
            "counterexamples": (
                "Deleting host files by pattern instead of executing the "
                "rollback plan.",
            ),
        },
    },
    {
        "operation_id": "compatibility.profiles",
        "method": "GET",
        "path": "/v1/compatibility-profiles",
        "kind": "scoped_read",
        "request_model": None,
        "handler": _compatibility_profiles,
        "summary": "Versioned legacy-vs-v2 compatibility profiles and "
        "consumer adapter contracts.",
        "usage": {
            "purpose": "Describe, per consumer, which authentication profile, "
            "headers, idempotency semantics and adapter guarantees apply.",
            "use_when": "Migrating KOS CLI/Beat/console/MCP, kos-infra, "
            "connect, Kaidera, second-brain or OpenKai onto the v2 profile.",
            "avoid_when": "You need live capability states; use "
            "capability.discover.",
            "effects": "read-only",
            "prerequisites": ("selected scope",),
            "cost_class": "trivial",
            "freshness": "versioned with the release",
            "evidence_contract": "The legacy profile is documentation only; "
            "the v2 API never emulates or infers it.",
            "failure_codes": ("scope_required",),
            "recovery_actions": (),
            "examples": (),
            "counterexamples": (
                "Sending X-Project/X-Agent-Name to a v2 endpoint and "
                "expecting legacy behavior.",
            ),
        },
    },
]

# Same typed write transport and receipt protocol as existing context operations.
for operation_id, path, model, handler, authority in (
    ('boot.agent.bind','/v1/boot/agent-bindings',BootAgentBindRequest,enact_boot_agent,'scope_write'),
    ('boot.entry.bind','/v1/boot/entry-bindings',BootEntryBindRequest,enact_boot_entry,'scope_write'),
    ('boot.catalogue.publish','/v1/boot/catalogue-publications',BootPublicationRequest,publish_boot_catalogue,'installation_owner'),
):
    OPERATIONS.append({
        'operation_id':operation_id,'method':'POST','path':path,'kind':'scoped_write',
        'authority':authority,'requires_scope':True,'requires_idempotency_key':True,
        'request_model':model,'handler':handler,'summary':'Append an authorized exact boot binding with an atomic receipt.',
        'usage':{'purpose':'Enact a consecutive boot revision without changing canonical bodies or grants.',
            'use_when':'A current lead/owner explicitly binds or withdraws an exact boot input.',
            'avoid_when':'The existing head already expresses the intent.',
            'effects':'one append and one committed receipt, or no effect',
            'prerequisites':('selected writable project','current lead/owner; publication requires installation owner and curator facts'),
            'cost_class':'cheap','freshness':'expected_revision checked under stream lock',
            'evidence_contract':'Exact stored receipt replays for the same canonical request and key; changed requests conflict.',
            'failure_codes':('boot_enact_denied','boot_revision_conflict','boot_fact_mismatch','idempotency_key_reused'),
            'recovery_actions':('inspect current head; use a new request/key for a new intent',),
            'examples':(),'counterexamples':('Treating a member write grant as boot enactment authority.',)},
    })
