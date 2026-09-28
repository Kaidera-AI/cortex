"""W1 ServiceAuth lifecycle and registry commands.

Every operation runs inside the caller's authenticated transaction. Owner-only
state changes execute through the SECURITY DEFINER registry functions from
migration 0002, which verify installation ownership, preserve credential
generation semantics and append to the privileged-action audit ledger. Tokens
are generated server-side, returned exactly once, and stored only as
HMAC-SHA256 digests.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from .models import (
    BindScopeRequest,
    EnactRosterRequest,
    EnactWriterPolicyRequest,
    EnrollPrincipalRequest,
    RecoverOwnerRequest,
    RegisterConnectorRequest,
    RenameScopeRequest,
    RevokeCredentialRequest,
    RevokeGrantRequest,
    RevokePrincipalRequest,
    RotateCredentialRequest,
)
from .receipts import begin_command, commit_receipt, request_digest, token_fingerprint
from .store import ApiProblem, Principal, token_digest

TOKEN_BYTES = 32
COMMON_FAILURES: dict[str, tuple[int, str]] = {
    "42501": (403, "owner_authority_required"),
    "P0002": (404, "registry_target_not_found"),
    "23514": (422, "invalid_registry_request"),
    "23505": (409, "registry_conflict"),
}
FAILURE_MESSAGES = {
    "owner_authority_required": "This operation requires installation owner authority.",
    "registry_target_not_found": "The registry target is unavailable.",
    "invalid_registry_request": "The registry request is invalid.",
    "registry_conflict": "The registry change conflicts with reserved state.",
    "alias_reserved": "The alias is reserved by another identity.",
    "policy_revision_conflict": "The writer-policy revision moved; recheck and retry.",
    "connector_namespace_taken": "The connector namespace already exists.",
    "recovery_credential_invalid": "The recovery credential is invalid or consumed.",
    "scope_not_found": "One or more requested scopes are unavailable.",
}


def _problem(code: str) -> ApiProblem:
    status_by_code = {
        **{code: status for status, code in COMMON_FAILURES.values()},
        "alias_reserved": 409,
        "policy_revision_conflict": 409,
        "connector_namespace_taken": 409,
        "recovery_credential_invalid": 401,
        "scope_not_found": 404,
    }
    return ApiProblem(status_by_code[code], code, FAILURE_MESSAGES[code])


def _translate(
    exc: asyncpg.PostgresError, extra: dict[str, tuple[int, str]] | None = None
) -> ApiProblem | None:
    mapping = {**COMMON_FAILURES, **(extra or {})}
    entry = mapping.get(exc.sqlstate or "")
    if entry is None:
        return None
    return _problem(entry[1])


def _issue_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _expiry(expires_in_seconds: int | None) -> datetime | None:
    if expires_in_seconds is None:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)


async def resolve_alias(connection: asyncpg.Connection, alias: str) -> uuid.UUID:
    scope_id = await connection.fetchval(
        """
        SELECT s.scope_id
          FROM cortex_core.scope_aliases AS a
          JOIN cortex_core.scopes AS s ON s.scope_id = a.scope_id
          JOIN cortex_auth.scope_grants AS g ON g.scope_id = s.scope_id
         WHERE a.alias = $1
           AND a.retired_at IS NULL
           AND s.is_active
           AND g.principal_id = NULLIF(
                   current_setting('cortex.principal_id', true), ''
               )::uuid
           AND g.revoked_at IS NULL
           AND (g.can_read OR g.can_write)
        """,
        alias,
    )
    if scope_id is None:
        raise _problem("scope_not_found")
    return scope_id


async def self_profile(
    connection: asyncpg.Connection, principal: Principal
) -> dict[str, Any]:
    rows = await connection.fetch(
        """
        SELECT g.scope_id, s.scope_kind, g.can_read, g.can_write, g.can_publish,
               (
                   SELECT a.alias
                     FROM cortex_core.scope_aliases AS a
                    WHERE a.scope_id = g.scope_id
                      AND a.is_primary
                      AND a.retired_at IS NULL
                    LIMIT 1
               ) AS primary_alias
          FROM cortex_auth.scope_grants AS g
          JOIN cortex_core.scopes AS s ON s.scope_id = g.scope_id
         WHERE g.principal_id = $1
           AND g.revoked_at IS NULL
           AND s.is_active
        """,
        principal.principal_id,
    )
    return {
        "principal_id": str(principal.principal_id),
        "installation_id": str(principal.installation_id),
        "scopes": [
            {
                "scope_id": str(row["scope_id"]),
                "primary_alias": row["primary_alias"],
                "scope_kind": row["scope_kind"],
                "can_read": row["can_read"],
                "can_write": row["can_write"],
                "can_publish": row["can_publish"],
            }
            for row in rows
        ],
    }


async def enroll_principal(
    connection: asyncpg.Connection,
    principal: Principal,
    pepper: bytes,
    payload: EnrollPrincipalRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "auth.enroll_principal"
    digest = request_digest(
        {
            "operation": operation,
            "principal_name": payload.principal_name,
            "actor_kind": payload.actor_kind,
            "expires_in_seconds": payload.expires_in_seconds,
            "scopes": [scope.model_dump(mode="json") for scope in payload.scopes],
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        installation_id=principal.installation_id,
    )
    if replayed and previous is not None:
        return 200, previous, True

    token = _issue_token()
    expires_at = _expiry(payload.expires_in_seconds)
    try:
        row = await connection.fetchrow(
            """
            SELECT principal_id, actor_id, credential_id, generation
              FROM cortex_auth.enroll_principal($1, $2, $3, $4, $5)
            """,
            principal.principal_id,
            payload.principal_name,
            payload.actor_kind,
            token_digest(token, pepper),
            expires_at,
        )
        bound_scopes = []
        for scope_request in payload.scopes:
            scope_id = await resolve_alias(connection, scope_request.alias)
            await connection.execute(
                "SELECT cortex_auth.bind_scope($1, $2, $3, $4, $5, $6)",
                principal.principal_id,
                row["principal_id"],
                scope_id,
                scope_request.can_read,
                scope_request.can_write,
                scope_request.can_publish,
            )
            bound_scopes.append(scope_request.model_dump(mode="json"))
    except asyncpg.PostgresError as exc:
        problem = _translate(exc)
        if problem is not None:
            raise problem from exc
        raise

    receipt = {
        "state": "committed",
        "operation": operation,
        "principal_id": str(row["principal_id"]),
        "actor_id": str(row["actor_id"]),
        "credential_id": str(row["credential_id"]),
        "generation": row["generation"],
        "expires_at": expires_at.isoformat() if expires_at else None,
        "token_fingerprint": token_fingerprint(token),
        "scopes": bound_scopes,
    }
    await commit_receipt(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        installation_id=principal.installation_id,
    )
    return 201, {**receipt, "token": token}, False


async def rotate_credential(
    connection: asyncpg.Connection,
    principal: Principal,
    pepper: bytes,
    payload: RotateCredentialRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "auth.rotate_credential"
    target = payload.principal_id or principal.principal_id
    digest = request_digest(
        {
            "operation": operation,
            "principal_id": str(target),
            "expires_in_seconds": payload.expires_in_seconds,
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        installation_id=principal.installation_id,
    )
    if replayed and previous is not None:
        return 200, previous, True

    token = _issue_token()
    expires_at = _expiry(payload.expires_in_seconds)
    try:
        row = await connection.fetchrow(
            """
            SELECT credential_id, generation, revoked_count
              FROM cortex_auth.rotate_credential($1, $2, $3, $4)
            """,
            principal.principal_id,
            target,
            token_digest(token, pepper),
            expires_at,
        )
    except asyncpg.PostgresError as exc:
        problem = _translate(exc)
        if problem is not None:
            raise problem from exc
        raise

    receipt = {
        "state": "committed",
        "operation": operation,
        "principal_id": str(target),
        "credential_id": str(row["credential_id"]),
        "generation": row["generation"],
        "revoked_count": row["revoked_count"],
        "expires_at": expires_at.isoformat() if expires_at else None,
        "token_fingerprint": token_fingerprint(token),
    }
    await commit_receipt(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        installation_id=principal.installation_id,
    )
    return 201, {**receipt, "token": token}, False


async def revoke_credential(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: RevokeCredentialRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    return await _void_registry_command(
        connection,
        principal,
        operation="auth.revoke_credential",
        idempotency_key=idempotency_key,
        digest_payload={"credential_id": str(payload.credential_id)},
        sql="SELECT cortex_auth.revoke_credential($1, $2)",
        args=(principal.principal_id, payload.credential_id),
        receipt_fields={"credential_id": str(payload.credential_id)},
    )


async def revoke_principal(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: RevokePrincipalRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    return await _void_registry_command(
        connection,
        principal,
        operation="auth.revoke_principal",
        idempotency_key=idempotency_key,
        digest_payload={"principal_id": str(payload.principal_id)},
        sql="SELECT cortex_auth.revoke_principal($1, $2)",
        args=(principal.principal_id, payload.principal_id),
        receipt_fields={"principal_id": str(payload.principal_id)},
    )


async def bind_scope(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: BindScopeRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    scope_id = await resolve_alias(connection, payload.alias)
    return await _void_registry_command(
        connection,
        principal,
        operation="auth.bind_scope",
        idempotency_key=idempotency_key,
        digest_payload={
            "principal_id": str(payload.principal_id),
            "alias": payload.alias,
            "can_read": payload.can_read,
            "can_write": payload.can_write,
            "can_publish": payload.can_publish,
        },
        sql="SELECT cortex_auth.bind_scope($1, $2, $3, $4, $5, $6)",
        args=(
            principal.principal_id,
            payload.principal_id,
            scope_id,
            payload.can_read,
            payload.can_write,
            payload.can_publish,
        ),
        receipt_fields={
            "principal_id": str(payload.principal_id),
            "scope_id": str(scope_id),
            "alias": payload.alias,
        },
    )


async def revoke_grant(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: RevokeGrantRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    scope_id = await resolve_alias(connection, payload.alias)
    return await _void_registry_command(
        connection,
        principal,
        operation="auth.revoke_scope_grant",
        idempotency_key=idempotency_key,
        digest_payload={
            "principal_id": str(payload.principal_id),
            "alias": payload.alias,
        },
        sql="SELECT cortex_auth.revoke_scope_grant($1, $2, $3)",
        args=(principal.principal_id, payload.principal_id, scope_id),
        receipt_fields={
            "principal_id": str(payload.principal_id),
            "scope_id": str(scope_id),
        },
    )


async def _void_registry_command(
    connection: asyncpg.Connection,
    principal: Principal,
    *,
    operation: str,
    idempotency_key: str,
    digest_payload: dict[str, Any],
    sql: str,
    args: tuple[Any, ...],
    receipt_fields: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    digest = request_digest({"operation": operation, **digest_payload})
    previous, replayed = await begin_command(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        installation_id=principal.installation_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    try:
        await connection.execute(sql, *args)
    except asyncpg.PostgresError as exc:
        problem = _translate(exc)
        if problem is not None:
            raise problem from exc
        raise
    receipt = {"state": "committed", "operation": operation, **receipt_fields}
    await commit_receipt(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        installation_id=principal.installation_id,
    )
    return 200, receipt, False


async def recover_owner(
    connection: asyncpg.Connection,
    pepper: bytes,
    payload: RecoverOwnerRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "auth.recover_owner"
    owner_token = _issue_token()
    recovery_token = _issue_token()
    expires_at = _expiry(payload.expires_in_seconds)
    try:
        row = await connection.fetchrow(
            """
            SELECT installation_id, principal_id, credential_id, generation,
                   recovery_generation
              FROM cortex_auth.recover_owner($1, $2, $3, $4)
            """,
            token_digest(payload.recovery_token, pepper),
            token_digest(owner_token, pepper),
            token_digest(recovery_token, pepper),
            expires_at,
        )
    except asyncpg.PostgresError as exc:
        problem = _translate(exc, {"42501": (401, "recovery_credential_invalid")})
        if problem is not None:
            raise problem from exc
        raise

    # The recovered owner is the receipt principal; publish its identity to the
    # transaction context so the receipt insert passes its RLS predicate.
    await connection.execute(
        "SELECT set_config('cortex.principal_id', $1, true)",
        str(row["principal_id"]),
    )
    digest = request_digest(
        {
            "operation": operation,
            "expires_in_seconds": payload.expires_in_seconds,
            "recovery_fingerprint": token_fingerprint(payload.recovery_token),
        }
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "installation_id": str(row["installation_id"]),
        "principal_id": str(row["principal_id"]),
        "credential_id": str(row["credential_id"]),
        "generation": row["generation"],
        "recovery_generation": row["recovery_generation"],
        "expires_at": expires_at.isoformat() if expires_at else None,
        "owner_token_fingerprint": token_fingerprint(owner_token),
        "recovery_token_fingerprint": token_fingerprint(recovery_token),
    }
    try:
        await commit_receipt(
            connection,
            principal_id=row["principal_id"],
            operation=operation,
            idempotency_key=idempotency_key,
            digest=digest,
            receipt_kind="committed",
            receipt=receipt,
            installation_id=row["installation_id"],
        )
    except asyncpg.UniqueViolationError as exc:
        raise ApiProblem(
            409,
            "idempotency_key_reused",
            "Use a new key for a different recovery attempt.",
        ) from exc
    return 201, {**receipt, "owner_token": owner_token, "recovery_token": recovery_token}, False


async def rename_scope(
    connection: asyncpg.Connection,
    principal: Principal,
    alias: str,
    payload: RenameScopeRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "registry.rename_scope"
    scope_id = await resolve_alias(connection, alias)
    digest = request_digest(
        {"operation": operation, "scope_id": str(scope_id), "new_alias": payload.new_alias}
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        installation_id=principal.installation_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    try:
        row = await connection.fetchrow(
            """
            SELECT retained_alias, new_primary_alias
              FROM cortex_auth.rename_scope_alias($1, $2, $3)
            """,
            principal.principal_id,
            scope_id,
            payload.new_alias,
        )
    except asyncpg.PostgresError as exc:
        problem = _translate(exc, {"23505": (409, "alias_reserved")})
        if problem is not None:
            raise problem from exc
        raise

    verified_new = await resolve_alias(connection, row["new_primary_alias"])
    verified_retained = await resolve_alias(connection, row["retained_alias"])
    if verified_new != scope_id or verified_retained != scope_id:
        raise ApiProblem(
            500,
            "rename_verification_failed",
            "The rename effect could not be verified; the transaction rolled back.",
        )
    receipt = {
        "state": "verified_effect",
        "operation": operation,
        "scope_id": str(scope_id),
        "retained_alias": row["retained_alias"],
        "new_primary_alias": row["new_primary_alias"],
        "verified": {
            "new_alias_resolves_to_scope": str(verified_new),
            "retained_alias_resolves_to_scope": str(verified_retained),
        },
    }
    await commit_receipt(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="verified_effect",
        receipt=receipt,
        installation_id=principal.installation_id,
    )
    return 200, receipt, False


async def enact_roster(
    connection: asyncpg.Connection,
    principal: Principal,
    alias: str,
    payload: EnactRosterRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "registry.enact_roster"
    scope_id = await resolve_alias(connection, alias)
    entries = [entry.model_dump(mode="json") for entry in payload.entries]
    digest = request_digest(
        {"operation": operation, "scope_id": str(scope_id), "entries": entries}
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        installation_id=principal.installation_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    try:
        revision = await connection.fetchval(
            "SELECT cortex_auth.enact_roster($1, $2, $3::jsonb)",
            principal.principal_id,
            scope_id,
            json.dumps(
                [
                    {
                        "principal_id": str(entry["principal_id"]),
                        "role": entry["role"],
                        "responsibility": entry.get("responsibility"),
                    }
                    for entry in entries
                ]
            ),
        )
    except asyncpg.PostgresError as exc:
        problem = _translate(exc)
        if problem is not None:
            raise problem from exc
        raise
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(scope_id),
        "roster_revision": revision,
    }
    await commit_receipt(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        installation_id=principal.installation_id,
    )
    return 201, receipt, False


async def enact_writer_policy(
    connection: asyncpg.Connection,
    principal: Principal,
    alias: str,
    payload: EnactWriterPolicyRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "registry.enact_writer_policy"
    scope_id = await resolve_alias(connection, alias)
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(scope_id),
            "allowed_roles": payload.allowed_roles,
            "expected_revision": payload.expected_revision,
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        installation_id=principal.installation_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    try:
        revision = await connection.fetchval(
            "SELECT cortex_auth.enact_writer_policy($1, $2, $3::text[], $4)",
            principal.principal_id,
            scope_id,
            list(payload.allowed_roles),
            payload.expected_revision,
        )
    except asyncpg.PostgresError as exc:
        problem = _translate(exc, {"40001": (409, "policy_revision_conflict")})
        if problem is not None:
            raise problem from exc
        raise
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(scope_id),
        "policy_revision": revision,
        "allowed_roles": list(payload.allowed_roles),
    }
    await commit_receipt(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        installation_id=principal.installation_id,
    )
    return 201, receipt, False


async def read_roster(
    connection: asyncpg.Connection, principal: Principal, alias: str
) -> dict[str, Any]:
    scope_id = await resolve_alias(connection, alias)
    entries = await connection.fetch(
        """
        SELECT m.membership_role, m.responsibility, m.status,
               a.actor_id, a.actor_kind, a.display_name,
               b.principal_id
          FROM cortex_auth.memberships AS m
          JOIN cortex_auth.actors AS a ON a.actor_id = m.actor_id
          JOIN cortex_auth.actor_bindings AS b ON b.actor_id = m.actor_id
         WHERE m.scope_id = $1
         ORDER BY m.membership_role, a.display_name
        """,
        scope_id,
    )
    roster_revision = await connection.fetchval(
        "SELECT COALESCE(max(revision), 0) FROM cortex_auth.roster_revisions "
        "WHERE scope_id = $1",
        scope_id,
    )
    policy = await connection.fetchrow(
        "SELECT revision, allowed_roles FROM cortex_auth.writer_policies "
        "WHERE scope_id = $1 ORDER BY revision DESC LIMIT 1",
        scope_id,
    )
    return {
        "scope_id": str(scope_id),
        "roster_revision": roster_revision,
        "entries": [
            {
                "actor_id": str(entry["actor_id"]),
                "actor_kind": entry["actor_kind"],
                "display_name": entry["display_name"],
                "principal_id": str(entry["principal_id"]),
                "role": entry["membership_role"],
                "responsibility": entry["responsibility"],
                "status": entry["status"],
            }
            for entry in entries
        ],
        "writer_policy": {
            "revision": policy["revision"] if policy else 0,
            "allowed_roles": list(policy["allowed_roles"]) if policy else [],
        },
    }


async def list_privileged_actions(
    connection: asyncpg.Connection, principal: Principal, limit: int
) -> list[dict[str, Any]]:
    try:
        rows = await connection.fetch(
            """
            SELECT action_id, action_type, caller_principal_id, target_principal_id,
                   target_scope_id, detail, created_at
              FROM cortex_auth.list_privileged_actions($1, $2)
            """,
            principal.principal_id,
            limit,
        )
    except asyncpg.PostgresError as exc:
        problem = _translate(exc)
        if problem is not None:
            raise problem from exc
        raise
    return [
        {
            "action_id": str(row["action_id"]),
            "action_type": row["action_type"],
            "caller_principal_id": (
                str(row["caller_principal_id"]) if row["caller_principal_id"] else None
            ),
            "target_principal_id": (
                str(row["target_principal_id"]) if row["target_principal_id"] else None
            ),
            "target_scope_id": (
                str(row["target_scope_id"]) if row["target_scope_id"] else None
            ),
            "detail": json.loads(row["detail"])
            if isinstance(row["detail"], str)
            else row["detail"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


async def register_connector(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: RegisterConnectorRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "registry.register_connector"
    digest = request_digest(
        {
            "operation": operation,
            "namespace": payload.namespace,
            "connector_kind": payload.connector_kind,
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        installation_id=principal.installation_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    try:
        connector_id = await connection.fetchval(
            "SELECT cortex_core.register_source_connector($1, $2, $3)",
            principal.principal_id,
            payload.namespace,
            payload.connector_kind,
        )
    except asyncpg.PostgresError as exc:
        problem = _translate(exc, {"23505": (409, "connector_namespace_taken")})
        if problem is not None:
            raise problem from exc
        raise
    receipt = {
        "state": "committed",
        "operation": operation,
        "connector_id": str(connector_id),
        "namespace": payload.namespace,
        "connector_kind": payload.connector_kind,
    }
    await commit_receipt(
        connection,
        principal_id=principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        installation_id=principal.installation_id,
    )
    return 201, receipt, False
