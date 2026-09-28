"""Budgeted boot / ``context.prepare`` and context-input enactments (R05).

Composition is deterministic: same authorized identity, same pinned inputs,
same budget → same context bytes. Mandatory policy material (identity,
effective writer policy, mandatory rules, required evidence and honest
missing-prerequisite state) always survives; a budget too small for it is an
explicit ``budget_exhausted`` error, never a misleading partial persona
(F05: loading failures fail explicitly, they are never swallowed into empty
lists). Optional sections truncate in priority order with explicit records.

Persona/rule/skill revisions are stored with provenance in
``cortex_context`` (migration 0006). Skill bodies stay out of boot: the
prepared context carries references and a ``skill.get`` fetch hint
(on-demand skills).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from ..content import search_content
from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem, ScopeContext
from .evaluation import Recommendation, recommend_operations
from .models import (
    ContextPrepareRequest,
    PersonaEnactRequest,
    RuleEnactRequest,
    SkillBindRequest,
    SkillEnactRequest,
    SkillGetQuery,
)
from .registry import OperationRegistry, get_registry

CONTEXT_COMPOSER_VERSION = "cortex.context.v1"

MAX_PAYLOAD_JSON_BYTES = 65_536

# Capability ids (worker-api §4) → registry operation candidates, best first.
CAPABILITY_OPERATIONS: dict[str, tuple[str, ...]] = {
    "memory.search": ("memory.search", "memory.search.lexical",
                      "content.search.lexical"),
    "code.assess-change": ("code.assess-change",),
    "graph.explore": ("graph.explore",),
    "evidence.inspect": ("evidence.inspect", "content.inspect"),
    "memory.record": ("memory.record",),
    "coordination.return": ("coordination.return", "coordination.handoff."
                            "return"),
}


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    data: Any
    mandatory: bool
    priority: int


def section_size(data: Any) -> int:
    return len(json.dumps(
        data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8"))


def _list_key(data: Any) -> str | None:
    if isinstance(data, dict):
        for key in ("items", "hits", "entries"):
            if isinstance(data.get(key), list):
                return key
    return None


def compose_sections(
    sections: list[Section], budget_bytes: int
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Fit sections into the budget; mandatory material is preserved whole."""
    mandatory = [s for s in sections if s.mandatory]
    optional = sorted(
        (s for s in sections if not s.mandatory),
        key=lambda s: (-s.priority, s.name),
    )
    mandatory_context = {s.name: s.data for s in mandatory}
    mandatory_bytes = section_size(mandatory_context)
    if mandatory_bytes > budget_bytes:
        raise ApiProblem(
            422,
            "budget_exhausted",
            (
                "Mandatory identity/policy material requires "
                f"{mandatory_bytes} bytes; the budget is {budget_bytes} "
                "bytes. Increase budget_bytes or reduce mandatory "
                "rules/persona material; mandatory policy is never trimmed."
            ),
        )
    composed: dict[str, Any] = dict(mandatory_context)
    truncations: list[dict[str, Any]] = []
    for section in optional:
        candidate = {**composed, section.name: section.data}
        if section_size(candidate) <= budget_bytes:
            composed[section.name] = section.data
            continue
        key = _list_key(section.data)
        if key is None:
            truncations.append(
                {"section": section.name, "kept": 0, "dropped": 1}
            )
            continue
        items = section.data[key]
        kept: list[Any] = []
        for item in items:
            candidate_data = {**section.data, key: [*kept, item]}
            if section_size({
                **composed, section.name: candidate_data
            }) <= budget_bytes:
                kept.append(item)
            else:
                break
        empty_container = {**section.data, key: []}
        if kept or section_size({
            **composed, section.name: empty_container
        }) <= budget_bytes:
            composed[section.name] = {**section.data, key: kept}
        truncations.append(
            {
                "section": section.name,
                "kept": len(kept),
                "dropped": len(items) - len(kept),
            }
        )
    composed_bytes = section_size(composed)
    report = {
        "budget_bytes": budget_bytes,
        "mandatory_bytes": mandatory_bytes,
        "composed_bytes": composed_bytes,
        "truncations": truncations,
    }
    return composed, report, truncations


async def declare_policy_revision(
    connection: asyncpg.Connection, context: ScopeContext
) -> tuple[int, dict[str, Any] | None]:
    """Load the effective writer policy for the selected scope.

    Mirrors the W1 content-module semantics (frozen contract): an enacted
    policy binds writers to allowed roster roles; loading failures propagate
    and are never swallowed into an empty policy (F05).
    """
    policy = await connection.fetchrow(
        "SELECT revision, allowed_roles FROM cortex_auth.writer_policies "
        "WHERE scope_id = $1 ORDER BY revision DESC LIMIT 1",
        context.selected.scope_id,
    )
    if policy is None:
        await connection.execute(
            "SELECT set_config('cortex.policy_revision', '0', true)"
        )
        return 0, None
    role = await connection.fetchval(
        """
        SELECT m.membership_role
          FROM cortex_auth.memberships AS m
          JOIN cortex_auth.actor_bindings AS b ON b.actor_id = m.actor_id
         WHERE m.scope_id = $1
           AND b.principal_id = $2
           AND m.status = 'active'
        """,
        context.selected.scope_id,
        context.principal.principal_id,
    )
    if role is None or role not in policy["allowed_roles"]:
        raise ApiProblem(
            403,
            "writer_policy_denied",
            "The active writer policy does not allow this principal to write.",
        )
    await connection.execute(
        "SELECT set_config('cortex.policy_revision', $1, true)",
        str(policy["revision"]),
    )
    return policy["revision"], {
        "revision": policy["revision"],
        "allowed_roles": list(policy["allowed_roles"]),
    }


def _coerce(model: type, payload: Any) -> Any:
    return payload if isinstance(payload, model) else model.model_validate(payload)


async def _next_revision(
    connection: asyncpg.Connection, table: str, scope_id: uuid.UUID,
    entity_id: uuid.UUID,
) -> int:
    current = await connection.fetchval(
        f"SELECT COALESCE(MAX(revision), 0) FROM cortex_context.{table} "
        "WHERE scope_id = $1 AND "
        + ("persona_id" if table == "persona_revisions"
           else "rule_id" if table == "rule_revisions" else "skill_id")
        + " = $2",
        scope_id,
        entity_id,
    )
    return int(current) + 1


async def select_persona_revision(
    connection: asyncpg.Connection,
    scope_id: uuid.UUID,
    revision: int | None = None,
) -> asyncpg.Record | None:
    return await connection.fetchrow(
        """
        SELECT persona_id, revision, template_version, payload, body
          FROM cortex_context.persona_revisions
         WHERE scope_id = $1 AND ($2::integer IS NULL OR revision = $2)
         ORDER BY created_at DESC, revision DESC, body DESC,
                  template_version DESC, payload::text DESC, ctid DESC
         LIMIT 1
        """,
        scope_id,
        revision,
    )


async def _slug_conflict(
    connection: asyncpg.Connection, table: str, scope_id: uuid.UUID,
    slug: str, entity_id: uuid.UUID,
) -> None:
    id_column = {
        "rule_revisions": "rule_id",
        "skill_revisions": "skill_id",
    }[table]
    other = await connection.fetchval(
        f"SELECT 1 FROM cortex_context.{table} WHERE scope_id = $1 "
        f"AND slug = $2 AND {id_column} <> $3 LIMIT 1",
        scope_id,
        slug,
        entity_id,
    )
    if other:
        raise ApiProblem(
            409,
            "slug_conflict",
            "Another identity already owns this slug in the scope.",
        )


async def enact_persona(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    payload = _coerce(PersonaEnactRequest, payload)
    operation = "persona.enact"
    if len(json.dumps(payload.payload, sort_keys=True).encode()) > (
        MAX_PAYLOAD_JSON_BYTES
    ):
        raise ApiProblem(422, "payload_too_large",
                         "The persona payload exceeds 64 KiB serialized.")
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "persona_id": str(payload.persona_id) if payload.persona_id else None,
            "template_version": payload.template_version,
            "payload": payload.payload,
            "body_sha256": _sha_hex(payload.body),
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    policy_revision, _ = await declare_policy_revision(connection, context)
    persona_id = payload.persona_id or uuid.uuid4()
    revision = await _next_revision(
        connection, "persona_revisions", context.selected.scope_id, persona_id
    )
    await connection.execute(
        """
        INSERT INTO cortex_context.persona_revisions
            (scope_id, persona_id, revision, template_version, payload, body,
             created_by_principal)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        """,
        context.selected.scope_id,
        persona_id,
        revision,
        payload.template_version,
        json.dumps(payload.payload, ensure_ascii=False, sort_keys=True),
        payload.body,
        context.principal.principal_id,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "persona_id": str(persona_id),
        "revision": revision,
        "scope_id": str(context.selected.scope_id),
        "policy_revision": policy_revision,
        "body_sha256": _sha_hex(payload.body),
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


def _sha_hex(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def enact_rule(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    payload = _coerce(RuleEnactRequest, payload)
    operation = "rule.enact"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "rule_id": str(payload.rule_id) if payload.rule_id else None,
            "slug": payload.slug,
            "obligation": payload.obligation,
            "body_sha256": _sha_hex(payload.body),
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    policy_revision, _ = await declare_policy_revision(connection, context)
    rule_id = payload.rule_id or uuid.uuid4()
    await _slug_conflict(
        connection, "rule_revisions", context.selected.scope_id,
        payload.slug, rule_id,
    )
    revision = await _next_revision(
        connection, "rule_revisions", context.selected.scope_id, rule_id
    )
    await connection.execute(
        """
        INSERT INTO cortex_context.rule_revisions
            (scope_id, rule_id, revision, slug, obligation, state, body,
             created_by_principal)
        VALUES ($1, $2, $3, $4, $5, 'active', $6, $7)
        """,
        context.selected.scope_id,
        rule_id,
        revision,
        payload.slug,
        payload.obligation,
        payload.body,
        context.principal.principal_id,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "rule_id": str(rule_id),
        "revision": revision,
        "slug": payload.slug,
        "obligation": payload.obligation,
        "scope_id": str(context.selected.scope_id),
        "policy_revision": policy_revision,
        "body_sha256": _sha_hex(payload.body),
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


async def enact_skill(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    payload = _coerce(SkillEnactRequest, payload)
    operation = "skill.enact"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "skill_id": str(payload.skill_id) if payload.skill_id else None,
            "slug": payload.slug,
            "when_to_use_sha256": _sha_hex(payload.when_to_use),
            "body_sha256": _sha_hex(payload.body),
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    policy_revision, _ = await declare_policy_revision(connection, context)
    skill_id = payload.skill_id or uuid.uuid4()
    await _slug_conflict(
        connection, "skill_revisions", context.selected.scope_id,
        payload.slug, skill_id,
    )
    revision = await _next_revision(
        connection, "skill_revisions", context.selected.scope_id, skill_id
    )
    await connection.execute(
        """
        INSERT INTO cortex_context.skill_revisions
            (scope_id, skill_id, revision, slug, when_to_use, body,
             created_by_principal)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        context.selected.scope_id,
        skill_id,
        revision,
        payload.slug,
        payload.when_to_use,
        payload.body,
        context.principal.principal_id,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "skill_id": str(skill_id),
        "revision": revision,
        "slug": payload.slug,
        "scope_id": str(context.selected.scope_id),
        "policy_revision": policy_revision,
        "body_sha256": _sha_hex(payload.body),
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


async def bind_skills(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    payload = _coerce(SkillBindRequest, payload)
    operation = "skill.bind"
    precedences = [entry.precedence for entry in payload.bindings]
    if len(set(precedences)) != len(precedences):
        raise ApiProblem(
            422, "duplicate_precedence",
            "Binding precedences must be unique within a scope.",
        )
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "bindings": [
                entry.model_dump(mode="json") for entry in payload.bindings
            ],
            "expected_revision": payload.expected_revision,
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    policy_revision, _ = await declare_policy_revision(connection, context)
    await connection.execute(
        """
        INSERT INTO cortex_context.skill_binding_sets (scope_id, revision)
        VALUES ($1, 0)
        ON CONFLICT (scope_id) DO NOTHING
        """,
        context.selected.scope_id,
    )
    current = await connection.fetchval(
        "SELECT revision FROM cortex_context.skill_binding_sets "
        "WHERE scope_id = $1",
        context.selected.scope_id,
    )
    if int(current) != payload.expected_revision:
        raise ApiProblem(
            409,
            "binding_revision_conflict",
            "The binding set moved; re-read it and retry with the current "
            "expected_revision.",
        )
    resolved: list[dict[str, Any]] = []
    try:
        await connection.execute(
            "DELETE FROM cortex_context.skill_bindings WHERE scope_id = $1",
            context.selected.scope_id,
        )
        for entry in sorted(payload.bindings, key=lambda e: e.precedence):
            revision = entry.revision
            if revision is None:
                revision = await connection.fetchval(
                    "SELECT MAX(revision) FROM cortex_context.skill_revisions "
                    "WHERE scope_id = $1 AND skill_id = $2",
                    context.selected.scope_id,
                    entry.skill_id,
                )
                if revision is None:
                    raise ApiProblem(
                        404, "skill_not_found",
                        "The skill is unavailable in this scope.",
                    )
            await connection.execute(
                """
                INSERT INTO cortex_context.skill_bindings
                    (scope_id, skill_id, bound_revision, precedence)
                VALUES ($1, $2, $3, $4)
                """,
                context.selected.scope_id,
                entry.skill_id,
                revision,
                entry.precedence,
            )
            resolved.append(
                {
                    "skill_id": str(entry.skill_id),
                    "revision": int(revision),
                    "precedence": entry.precedence,
                }
            )
    except asyncpg.ForeignKeyViolationError as exc:
        raise ApiProblem(
            404, "skill_revision_not_found",
            "A bound skill revision is unavailable in this scope.",
        ) from exc
    new_revision = int(current) + 1
    await connection.execute(
        "UPDATE cortex_context.skill_binding_sets SET revision = $2, "
        "updated_at = now() WHERE scope_id = $1",
        context.selected.scope_id,
        new_revision,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(context.selected.scope_id),
        "binding_revision": new_revision,
        "bindings": resolved,
        "policy_revision": policy_revision,
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


async def get_bindings(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    """Read the current skill-binding set and its optimistic-lock revision."""
    revision = await connection.fetchval(
        "SELECT revision FROM cortex_context.skill_binding_sets "
        "WHERE scope_id = $1",
        context.selected.scope_id,
    )
    rows = await connection.fetch(
        """
        SELECT b.skill_id, b.bound_revision, b.precedence, r.slug
          FROM cortex_context.skill_bindings AS b
          JOIN cortex_context.skill_revisions AS r
            ON r.scope_id = b.scope_id
           AND r.skill_id = b.skill_id
           AND r.revision = b.bound_revision
         WHERE b.scope_id = $1
         ORDER BY b.precedence, r.slug
        """,
        context.selected.scope_id,
    )
    return {
        "revision": int(revision) if revision is not None else 0,
        "bindings": [
            {
                "skill_id": str(row["skill_id"]),
                "revision": row["bound_revision"],
                "precedence": row["precedence"],
                "slug": row["slug"],
            }
            for row in rows
        ],
        "scope_id": str(context.selected.scope_id),
    }


async def get_skill(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    query = _coerce(SkillGetQuery, payload or {})
    skill_id = uuid.UUID(str(path_params["skill_id"]))
    if query.revision is None:
        row = await connection.fetchrow(
            """
            SELECT skill_id, revision, slug, when_to_use, body, created_at
              FROM cortex_context.skill_revisions
             WHERE scope_id = $1 AND skill_id = $2
             ORDER BY revision DESC LIMIT 1
            """,
            context.selected.scope_id,
            skill_id,
        )
    else:
        row = await connection.fetchrow(
            """
            SELECT skill_id, revision, slug, when_to_use, body, created_at
              FROM cortex_context.skill_revisions
             WHERE scope_id = $1 AND skill_id = $2 AND revision = $3
            """,
            context.selected.scope_id,
            skill_id,
            query.revision,
        )
    if row is None:
        raise ApiProblem(
            404, "skill_not_found",
            "The skill revision is unavailable in the selected scope.",
        )
    return {
        "skill_id": str(row["skill_id"]),
        "revision": row["revision"],
        "slug": row["slug"],
        "when_to_use": row["when_to_use"],
        "body": row["body"],
        "created_at": row["created_at"].isoformat(),
        "scope_id": str(context.selected.scope_id),
    }


def _capability_availability(
    registry: OperationRegistry, capability: str
) -> tuple[bool, dict[str, Any]]:
    for candidate in CAPABILITY_OPERATIONS.get(capability, (capability,)):
        try:
            operation = registry.get(candidate)
        except KeyError:
            continue
        state = registry.module_state(operation.module)
        if state.state == "ready":
            return True, {"operation_id": candidate, "state": "ready"}
        return False, {
            "operation_id": candidate,
            "state": "unavailable",
            "reason": state.reason,
        }
    return False, {
        "operation_id": capability,
        "state": "unavailable",
        "reason": "no module publishes this capability yet",
    }


async def prepare_context(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
    *,
    registry: OperationRegistry | None = None,
) -> tuple[int, dict[str, Any], bool]:
    payload = _coerce(ContextPrepareRequest, payload)
    registry = registry or get_registry()
    operation = "context.prepare"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "intent": payload.intent,
            "task_ref": payload.task_ref,
            "task_text": payload.task_text,
            "repository_ref": payload.repository_ref,
            "change_ref": payload.change_ref,
            "budget_bytes": payload.budget_bytes,
            "recall_limit": payload.recall_limit,
            "registry_digest": registry.digest,
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision, policy = await declare_policy_revision(connection, context)

    persona_row = await select_persona_revision(
        connection, context.selected.scope_id
    )
    rule_rows = await connection.fetch(
        """
        SELECT rule_id, revision, slug, obligation, body
          FROM (
              SELECT DISTINCT ON (rule_id)
                     rule_id, revision, slug, obligation, body, state
                FROM cortex_context.rule_revisions
               WHERE scope_id = $1
               ORDER BY rule_id, revision DESC
          ) AS latest
         WHERE latest.state = 'active'
        """,
        context.selected.scope_id,
    )
    skill_rows = await connection.fetch(
        """
        SELECT b.skill_id, b.bound_revision AS revision, b.precedence,
               r.slug, r.when_to_use
          FROM cortex_context.skill_bindings AS b
          JOIN cortex_context.skill_revisions AS r
            ON r.scope_id = b.scope_id
           AND r.skill_id = b.skill_id
           AND r.revision = b.bound_revision
         WHERE b.scope_id = $1
         ORDER BY b.precedence, r.slug
        """,
        context.selected.scope_id,
    )

    mandatory_rules = [
        {
            "rule_id": str(row["rule_id"]),
            "slug": row["slug"],
            "revision": row["revision"],
            "body": row["body"],
        }
        for row in sorted(rule_rows, key=lambda r: r["slug"])
        if row["obligation"] == "mandatory"
    ]
    optional_rules = [
        {
            "rule_id": str(row["rule_id"]),
            "slug": row["slug"],
            "revision": row["revision"],
            "body": row["body"],
        }
        for row in sorted(rule_rows, key=lambda r: r["slug"])
        if row["obligation"] == "optional"
    ]

    recall: dict[str, Any] = {"hits": [], "note": "no task text supplied"}
    if payload.task_text and payload.recall_limit > 0:
        hits = await search_content(
            connection, payload.task_text, payload.recall_limit, False
        )
        recall = {
            "hits": hits,
            "note": "bounded lexical recall over the selected scope",
        }

    has_repository_change = bool(payload.repository_ref and payload.change_ref)
    graph_ready, _graph_detail = _capability_availability(
        registry, "code.assess-change"
    )
    recommendations: tuple[Recommendation, ...] = recommend_operations(
        payload.intent,
        has_repository_change=has_repository_change,
        graph_state="fresh" if graph_ready else "missing",
    )

    missing: list[dict[str, Any]] = []
    for capability in sorted(
        {item.operation_id for item in recommendations}
    ):
        available, detail = _capability_availability(registry, capability)
        if not available:
            missing.append(
                {
                    "prerequisite": capability,
                    "state": detail["state"],
                    "reason": detail.get("reason"),
                    "recovery": [
                        "wait for the owning module to deploy",
                        "attach an explicit waiver where policy allows",
                    ],
                }
            )

    required_evidence = _required_evidence(payload.intent, has_repository_change)
    next_work = [
        {
            "operation_id": item.operation_id,
            "reason": item.reason,
            "arguments_hint": item.arguments_hint,
        }
        for item in recommendations
    ]
    for entry in missing:
        next_work.append(
            {
                "operation_id": entry["prerequisite"],
                "reason": f"unavailable: {entry['reason']}",
                "arguments_hint": {},
            }
        )

    identity_section = {
        "principal_id": str(context.principal.principal_id),
        "installation_id": str(context.principal.installation_id),
        "scope_id": str(context.selected.scope_id),
        "scope_alias": context.selected.alias,
        "scope_kind": context.selected.kind,
    }
    policy_section = policy or {
        "revision": 0,
        "state": "no writer policy enacted for this scope",
    }
    persona_section = None
    if persona_row is not None:
        persona_payload = persona_row["payload"]
        if isinstance(persona_payload, str):
            persona_payload = json.loads(persona_payload)
        persona_section = {
            "persona_id": str(persona_row["persona_id"]),
            "revision": persona_row["revision"],
            "template_version": persona_row["template_version"],
            "body": persona_row["body"],
            "acceptance_criteria": persona_payload.get(
                "acceptance_criteria", []
            ),
        }
    skills_section = {
        "items": [
            {
                "skill_id": str(row["skill_id"]),
                "slug": row["slug"],
                "revision": row["revision"],
                "precedence": row["precedence"],
                "when_to_use": row["when_to_use"],
                "fetch": {
                    "operation_id": "skill.get",
                    "arguments_hint": {
                        "skill_id": str(row["skill_id"]),
                        "revision": row["revision"],
                    },
                },
            }
            for row in skill_rows
        ]
    }

    sections = [
        Section("identity", identity_section, True, 100),
        Section("policy", policy_section, True, 100),
        Section("mandatory_rules", {"items": mandatory_rules}, True, 100),
        Section("required_evidence", {"items": required_evidence}, True, 100),
        Section("missing_prerequisites", {"items": missing}, True, 100),
        Section(
            "recommendations",
            {"items": [item.as_dict() for item in recommendations]},
            False,
            60,
        ),
    ]
    if persona_section is not None:
        sections.append(Section("persona", persona_section, False, 50))
    sections.append(Section("skills", skills_section, False, 40))
    if optional_rules:
        sections.append(
            Section("optional_rules", {"items": optional_rules}, False, 35)
        )
    sections.append(Section("recall", recall, False, 30))

    composed, budget_report, _truncations = compose_sections(
        sections, payload.budget_bytes
    )

    provenance = {
        "composer_version": CONTEXT_COMPOSER_VERSION,
        "registry_version": registry.version,
        "registry_digest": registry.digest,
        "writer_policy": policy_section,
        "persona": (
            {
                "persona_id": persona_section["persona_id"],
                "revision": persona_section["revision"],
                "template_version": persona_section["template_version"],
            }
            if persona_section
            else None
        ),
        "rules": [
            {"rule_id": item["rule_id"], "slug": item["slug"],
             "revision": item["revision"]}
            for item in [*mandatory_rules, *optional_rules]
        ],
        "skills": [
            {"skill_id": item["skill_id"], "slug": item["slug"],
             "revision": item["revision"]}
            for item in skills_section["items"]
        ],
    }

    preparation_id = uuid.uuid4()
    await connection.execute(
        """
        INSERT INTO cortex_context.context_preparations
            (scope_id, preparation_id, prepared_by_principal, intent,
             budget_bytes, mandatory_bytes, composed_bytes, policy_revision,
             sections, provenance)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb)
        """,
        context.selected.scope_id,
        preparation_id,
        context.principal.principal_id,
        payload.intent,
        payload.budget_bytes,
        budget_report["mandatory_bytes"],
        budget_report["composed_bytes"],
        policy_revision,
        json.dumps(composed, ensure_ascii=False, sort_keys=True),
        json.dumps(provenance, ensure_ascii=False, sort_keys=True),
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "preparation_id": str(preparation_id),
        "scope_id": str(context.selected.scope_id),
        "policy_revision": policy_revision,
        "context": composed,
        "budget": budget_report,
        "next_work": next_work,
        "provenance": provenance,
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


def _required_evidence(intent: str, has_repository_change: bool) -> list[str]:
    evidence = [
        "cite source revisions for every load-bearing claim",
    ]
    if intent == "code_change" and has_repository_change:
        evidence.append(
            "a fresh code.assess-change assessment bound to the change "
            "digest, or an authorized waiver, before return"
        )
    if intent == "record_or_return":
        evidence.append(
            "return through the normal return/review lifecycle with the "
            "work-product receipt attached"
        )
    if intent in ("architecture", "research"):
        evidence.append(
            "inspect at least the load-bearing original behind each cited hit"
        )
    return evidence
