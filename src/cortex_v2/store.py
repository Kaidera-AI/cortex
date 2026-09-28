from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Principal:
    principal_id: uuid.UUID
    installation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Scope:
    alias: str
    scope_id: uuid.UUID
    kind: str
    can_read: bool
    can_write: bool
    can_publish: bool


@dataclass(frozen=True, slots=True)
class ScopeContext:
    principal: Principal
    selected: Scope
    read_scopes: tuple[Scope, ...]


def token_digest(token: str, pepper: bytes) -> bytes:
    return hmac.new(pepper, token.encode("utf-8"), hashlib.sha256).digest()


async def authenticate(connection: asyncpg.Connection, digest: bytes) -> Principal:
    row = await connection.fetchrow(
        "SELECT principal_id, installation_id "
        "FROM cortex_auth.authenticate($1)",
        digest,
    )
    if row is None:
        raise ApiProblem(
            401, "invalid_credential", "A valid bearer credential is required."
        )
    principal = Principal(row["principal_id"], row["installation_id"])
    await connection.execute(
        "SELECT set_config('cortex.principal_id', $1, true)",
        str(principal.principal_id),
    )
    return principal


async def resolve_scopes(
    connection: asyncpg.Connection,
    principal: Principal,
    selected_alias: str | None,
    requested_aliases: list[str],
    *,
    write: bool,
) -> ScopeContext:
    aliases = list(dict.fromkeys(requested_aliases))
    if (
        not aliases
        or len(aliases) > 8
        or any(not alias for alias in aliases)
    ):
        raise ApiProblem(
            422,
            "invalid_scope_selection",
            "Select one to eight readable scopes.",
        )
    if selected_alias and selected_alias not in aliases:
        raise ApiProblem(
            422,
            "invalid_scope_selection",
            "The selected scope must be readable.",
        )

    rows = await connection.fetch(
        """
        SELECT a.alias, s.scope_id, s.scope_kind,
               g.can_read, g.can_write, g.can_publish
          FROM cortex_core.scope_aliases AS a
          JOIN cortex_core.scopes AS s ON s.scope_id = a.scope_id
          JOIN cortex_auth.scope_grants AS g ON g.scope_id = s.scope_id
         WHERE g.principal_id = $1
           AND g.revoked_at IS NULL
           AND s.is_active
           AND a.retired_at IS NULL
           AND a.alias = ANY($2::text[])
        """,
        principal.principal_id,
        aliases,
    )
    if len(rows) != len(aliases):
        raise ApiProblem(
            404, "scope_not_found", "One or more requested scopes are unavailable."
        )

    scopes_by_alias = {
        row["alias"]: Scope(
            alias=row["alias"],
            scope_id=row["scope_id"],
            kind=row["scope_kind"],
            can_read=row["can_read"],
            can_write=row["can_write"],
            can_publish=row["can_publish"],
        )
        for row in rows
    }
    read_scopes = tuple(scopes_by_alias[alias] for alias in aliases)
    if any(not scope.can_read for scope in read_scopes):
        raise ApiProblem(
            404, "scope_not_found", "One or more requested scopes are unavailable."
        )

    selected = scopes_by_alias.get(selected_alias) if selected_alias else None
    if selected is None:
        raise ApiProblem(
            422, "selected_scope_required", "Select a primary Cortex scope."
        )
    if write and (
        not selected.can_write
        or (selected.kind == "shared" and not selected.can_publish)
    ):
        raise ApiProblem(
            403, "scope_write_denied", "Write access is not granted for this scope."
        )

    await connection.execute(
        "SELECT set_config('cortex.read_scope_ids', $1, true)",
        ",".join(sorted(str(scope.scope_id) for scope in read_scopes)),
    )
    write_scope_id = str(selected.scope_id) if write else ""
    await connection.execute(
        "SELECT set_config('cortex.write_scope_id', $1, true)", write_scope_id
    )
    return ScopeContext(principal, selected, read_scopes)


def _json_object(value: str | dict[str, Any]) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else value


async def create_record(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    record_type: str,
    body: str,
) -> tuple[int, dict[str, Any], bool]:
    request_digest = hashlib.sha256(
        json.dumps(
            {"record_type": record_type, "body": body},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).digest()
    lock_key = ":".join(
        (
            str(context.principal.principal_id),
            str(context.selected.scope_id),
            "memory.create",
            idempotency_key,
        )
    )
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        lock_key,
    )

    previous = await connection.fetchrow(
        """
        SELECT request_hash, response_body
          FROM cortex_core.idempotency_receipts
         WHERE principal_id = $1
           AND scope_id = $2
           AND operation = 'memory.create'
           AND idempotency_key = $3
        """,
        context.principal.principal_id,
        context.selected.scope_id,
        idempotency_key,
    )
    if previous:
        if not hmac.compare_digest(previous["request_hash"], request_digest):
            raise ApiProblem(
                409,
                "idempotency_key_reused",
                "Use a new key for a different request.",
            )
        return 200, _json_object(previous["response_body"]), True

    record_id = uuid.uuid4()
    row = await connection.fetchrow(
        """
        INSERT INTO cortex_core.memory_records
            (scope_id, record_id, logical_record_id, record_type, revision,
             author_principal_id, body)
        VALUES ($1, $2, $2, $3, 1, $4, $5)
        RETURNING created_at
        """,
        context.selected.scope_id,
        record_id,
        record_type,
        context.principal.principal_id,
        body,
    )
    await connection.execute(
        """
        INSERT INTO cortex_core.lexical_documents
            (scope_id, record_id, revision, source_text)
        VALUES ($1, $2, 1, $3)
        """,
        context.selected.scope_id,
        record_id,
        body,
    )
    await connection.execute(
        """
        INSERT INTO cortex_core.outbox_events
            (event_id, scope_id, aggregate_id, event_type, payload)
        VALUES ($1, $2, $3, 'memory.recorded', $4::jsonb)
        """,
        uuid.uuid4(),
        context.selected.scope_id,
        record_id,
        json.dumps({"record_id": str(record_id), "record_type": record_type}),
    )
    receipt = {
        "state": "committed",
        "record_id": str(record_id),
        "record_type": record_type,
        "body": body,
        "scope_id": str(context.selected.scope_id),
        "revision": 1,
        "created_at": row["created_at"].isoformat(),
    }
    await connection.execute(
        """
        INSERT INTO cortex_core.idempotency_receipts
            (
                principal_id,
                scope_id,
                operation,
                idempotency_key,
                request_hash,
                response_body
            )
        VALUES ($1, $2, 'memory.create', $3, $4, $5::jsonb)
        """,
        context.principal.principal_id,
        context.selected.scope_id,
        idempotency_key,
        request_digest,
        json.dumps(receipt),
    )
    return 201, receipt, False


async def search_records(
    connection: asyncpg.Connection, query: str, limit: int
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT r.record_id, r.logical_record_id, r.record_type, r.body, r.scope_id,
               r.created_at, r.revision,
               ts_rank_cd(d.search_document, plainto_tsquery('simple', $1)) AS score
          FROM cortex_core.lexical_documents AS d
          JOIN cortex_core.memory_records AS r
            ON r.scope_id = d.scope_id
           AND r.record_id = d.record_id
           AND r.revision = d.revision
         WHERE d.search_document @@ plainto_tsquery('simple', $1)
         ORDER BY score DESC, r.created_at DESC, r.record_id
         LIMIT $2
        """,
        query,
        limit,
    )
    return [
        {
            "record_id": str(row["record_id"]),
            "record_type": row["record_type"],
            "body": row["body"],
            "scope_id": str(row["scope_id"]),
            "created_at": row["created_at"].isoformat(),
            "citation": {
                "scope_id": str(row["scope_id"]),
                "record_id": str(row["record_id"]),
                "logical_record_id": str(row["logical_record_id"]),
                "revision": row["revision"],
            },
            "evidence": {
                "source_revision": row["revision"],
                "retrieval_stage": "lexical",
                "projection_generation": "lexical-v1",
            },
        }
        for row in rows
    ]
