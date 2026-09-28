"""W1 typed canonical content commands and reads.

Content items and revisions are append-only; status transitions live in an
append-only log with enforced legal transitions. Writes re-declare the current
writer-policy revision inside the same transaction, and the database rechecks
that declaration at insert time. Every mutation returns a typed receipt
(committed / pending_processing) stored with its canonical request digest;
ingest additionally deduplicates on the connector-namespaced source key.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import asyncpg

from .models import (
    CreateContentRequest,
    IngestContentRequest,
    ReviseContentRequest,
)
from .receipts import begin_command, commit_receipt, request_digest
from .store import ApiProblem, ScopeContext

PENDING_PROCESSING_CLASSES = frozenset({"artifact"})
CONTENT_FAILURES: dict[str, tuple[int, str]] = {
    "23514": (422, "invalid_content_payload"),
    "55000": (409, "policy_revision_stale"),
    "23503": (409, "content_lineage_conflict"),
}
CONTENT_FAILURE_MESSAGES = {
    "invalid_content_payload": "The typed payload does not match the content class.",
    "policy_revision_stale": "The writer-policy revision changed; retry the command.",
    "content_lineage_conflict": "The content lineage rejected this write.",
    "content_not_found": "The content is unavailable in the selected scopes.",
    "revision_conflict": "The expected revision does not match the current revision.",
    "content_tombstoned": "Tombstoned content cannot change.",
    "content_superseded": "Superseded content is historical and cannot change.",
    "invalid_status_transition": "The requested status transition is not allowed.",
    "successor_not_found": "The successor content is unavailable in this scope.",
    "connector_not_found": "The source connector namespace is unavailable.",
    "source_key_conflict": "Another revision already owns this connector source key.",
}


def _problem(code: str, status: int) -> ApiProblem:
    return ApiProblem(status, code, CONTENT_FAILURE_MESSAGES[code])


def _translate(exc: asyncpg.PostgresError) -> ApiProblem | None:
    entry = CONTENT_FAILURES.get(exc.sqlstate or "")
    if entry is None:
        return None
    return _problem(entry[1], entry[0])


def content_hash(content_class: str, payload: dict[str, Any], body: str) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {"content_class": content_class, "payload": payload, "body": body},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).digest()


def ingest_idempotency_key(namespace: str, source_key: str) -> str:
    return hashlib.sha256(f"{namespace}:{source_key}".encode("utf-8")).hexdigest()[:64]


async def _declare_policy_revision(
    connection: asyncpg.Connection,
    context: ScopeContext,
) -> int:
    policy = await connection.fetchrow(
        "SELECT revision, allowed_roles FROM cortex_auth.writer_policies "
        "WHERE scope_id = $1 ORDER BY revision DESC LIMIT 1",
        context.selected.scope_id,
    )
    revision = policy["revision"] if policy else 0
    if policy is not None:
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
        "SELECT set_config('cortex.policy_revision', $1, true)", str(revision)
    )
    return revision


async def _load_item(
    connection: asyncpg.Connection, context: ScopeContext, content_id: uuid.UUID
) -> dict[str, Any]:
    row = await connection.fetchrow(
        """
        SELECT i.scope_id, i.content_class, i.created_at,
               cortex_core.content_current_status(i.scope_id, i.content_id) AS status
          FROM cortex_core.content_items AS i
         WHERE i.content_id = $1
           AND i.scope_id = $2
        """,
        content_id,
        context.selected.scope_id,
    )
    if row is None:
        raise _problem("content_not_found", 404)
    return dict(row)


async def create_content(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CreateContentRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "content.create"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "content_class": payload.content_class,
            "payload": payload.payload,
            "body": payload.body,
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

    policy_revision = await _declare_policy_revision(connection, context)
    content_id = uuid.uuid4()
    receipt_kind = (
        "pending_processing"
        if payload.content_class in PENDING_PROCESSING_CLASSES
        else "committed"
    )
    try:
        created_at = await connection.fetchval(
            """
            INSERT INTO cortex_core.content_items
                (scope_id, content_id, content_class, created_by_principal)
            VALUES ($1, $2, $3, $4)
            RETURNING created_at
            """,
            context.selected.scope_id,
            content_id,
            payload.content_class,
            context.principal.principal_id,
        )
        await connection.execute(
            """
            INSERT INTO cortex_core.content_revisions
                (scope_id, content_id, revision, payload, body_text, content_hash,
                 author_principal_id)
            VALUES ($1, $2, 1, $3::jsonb, $4, $5, $6)
            """,
            context.selected.scope_id,
            content_id,
            json.dumps(payload.payload, ensure_ascii=False, sort_keys=True),
            payload.body,
            content_hash(payload.content_class, payload.payload, payload.body),
            context.principal.principal_id,
        )
        await connection.execute(
            """
            INSERT INTO cortex_core.content_lexical_documents
                (scope_id, content_id, revision, source_text)
            VALUES ($1, $2, 1, $3)
            """,
            context.selected.scope_id,
            content_id,
            payload.body,
        )
        await connection.execute(
            """
            INSERT INTO cortex_core.content_outbox_events
                (event_id, scope_id, content_id, event_type, payload)
            VALUES ($1, $2, $3, 'content.recorded', $4::jsonb)
            """,
            uuid.uuid4(),
            context.selected.scope_id,
            content_id,
            json.dumps(
                {
                    "content_id": str(content_id),
                    "content_class": payload.content_class,
                    "revision": 1,
                    "policy_revision": policy_revision,
                }
            ),
        )
    except asyncpg.PostgresError as exc:
        problem = _translate(exc)
        if problem is not None:
            raise problem from exc
        raise

    receipt = {
        "state": receipt_kind,
        "operation": operation,
        "content_id": str(content_id),
        "content_class": payload.content_class,
        "revision": 1,
        "content_hash": content_hash(
            payload.content_class, payload.payload, payload.body
        ).hex(),
        "scope_id": str(context.selected.scope_id),
        "policy_revision": policy_revision,
        "created_at": created_at.isoformat(),
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind=receipt_kind,
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


async def revise_content(
    connection: asyncpg.Connection,
    context: ScopeContext,
    content_id: uuid.UUID,
    payload: ReviseContentRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "content.revise"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "content_id": str(content_id),
            "payload": payload.payload,
            "body": payload.body,
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

    item = await _load_item(connection, context, content_id)
    if item["status"] == "tombstoned":
        raise _problem("content_tombstoned", 409)
    if item["status"] == "superseded":
        raise _problem("content_superseded", 409)
    current_revision = await connection.fetchval(
        "SELECT max(revision) FROM cortex_core.content_revisions "
        "WHERE scope_id = $1 AND content_id = $2",
        context.selected.scope_id,
        content_id,
    )
    if current_revision != payload.expected_revision:
        raise _problem("revision_conflict", 409)

    policy_revision = await _declare_policy_revision(connection, context)
    new_revision = payload.expected_revision + 1
    new_hash = content_hash(item["content_class"], payload.payload, payload.body)
    try:
        await connection.execute(
            """
            INSERT INTO cortex_core.content_revisions
                (scope_id, content_id, revision, payload, body_text, content_hash,
                 author_principal_id, supersedes_revision)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
            """,
            context.selected.scope_id,
            content_id,
            new_revision,
            json.dumps(payload.payload, ensure_ascii=False, sort_keys=True),
            payload.body,
            new_hash,
            context.principal.principal_id,
            payload.expected_revision,
        )
        await connection.execute(
            """
            INSERT INTO cortex_core.content_lexical_documents
                (scope_id, content_id, revision, source_text)
            VALUES ($1, $2, $3, $4)
            """,
            context.selected.scope_id,
            content_id,
            new_revision,
            payload.body,
        )
        await connection.execute(
            """
            INSERT INTO cortex_core.content_outbox_events
                (event_id, scope_id, content_id, event_type, payload)
            VALUES ($1, $2, $3, 'content.revised', $4::jsonb)
            """,
            uuid.uuid4(),
            context.selected.scope_id,
            content_id,
            json.dumps(
                {
                    "content_id": str(content_id),
                    "revision": new_revision,
                    "policy_revision": policy_revision,
                }
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
        "content_id": str(content_id),
        "content_class": item["content_class"],
        "revision": new_revision,
        "content_hash": new_hash.hex(),
        "scope_id": str(context.selected.scope_id),
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


async def change_status(
    connection: asyncpg.Connection,
    context: ScopeContext,
    content_id: uuid.UUID,
    action: str,
    reason: str | None,
    successor_content_id: uuid.UUID | None,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = f"content.{action}"
    new_status = {
        "invalidate": "invalidated",
        "restore": "current",
        "supersede": "superseded",
        "tombstone": "tombstoned",
    }[action]
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "content_id": str(content_id),
            "reason": reason,
            "successor_content_id": (
                str(successor_content_id) if successor_content_id else None
            ),
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

    await _load_item(connection, context, content_id)
    if new_status == "superseded":
        if successor_content_id is None:
            raise ApiProblem(
                422,
                "successor_required",
                "Supersession requires successor_content_id.",
            )
        successor = await connection.fetchval(
            "SELECT content_id FROM cortex_core.content_items "
            "WHERE scope_id = $1 AND content_id = $2",
            context.selected.scope_id,
            successor_content_id,
        )
        if successor is None:
            raise _problem("successor_not_found", 422)

    policy_revision = await _declare_policy_revision(connection, context)
    try:
        await connection.execute(
            """
            INSERT INTO cortex_core.content_status_log
                (scope_id, content_id, status_seq, status, prior_status,
                 superseded_by_content_id, reason, changed_by_principal)
            VALUES ($1, $2, NULL, $3, NULL, $4, $5, $6)
            """,
            context.selected.scope_id,
            content_id,
            new_status,
            successor_content_id,
            reason,
            context.principal.principal_id,
        )
        await connection.execute(
            """
            INSERT INTO cortex_core.content_outbox_events
                (event_id, scope_id, content_id, event_type, payload)
            VALUES ($1, $2, $3, 'content.status_changed', $4::jsonb)
            """,
            uuid.uuid4(),
            context.selected.scope_id,
            content_id,
            json.dumps(
                {
                    "content_id": str(content_id),
                    "status": new_status,
                    "policy_revision": policy_revision,
                }
            ),
        )
    except asyncpg.PostgresError as exc:
        if exc.sqlstate == "55000" and "tombstoned" in str(exc):
            raise _problem("content_tombstoned", 409) from exc
        if exc.sqlstate == "23514":
            raise _problem("invalid_status_transition", 409) from exc
        problem = _translate(exc)
        if problem is not None:
            raise problem from exc
        raise

    receipt = {
        "state": "committed",
        "operation": operation,
        "content_id": str(content_id),
        "status": new_status,
        "reason": reason,
        "superseded_by_content_id": (
            str(successor_content_id) if successor_content_id else None
        ),
        "scope_id": str(context.selected.scope_id),
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
    return 200, receipt, False


async def ingest_content(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: IngestContentRequest,
) -> tuple[int, dict[str, Any], bool]:
    operation = "content.ingest"
    connector = await connection.fetchrow(
        "SELECT connector_id FROM cortex_core.source_connectors WHERE namespace = $1",
        payload.connector_namespace,
    )
    if connector is None:
        raise _problem("connector_not_found", 404)

    idempotency_key = ingest_idempotency_key(
        payload.connector_namespace, payload.source_key
    )
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "connector_namespace": payload.connector_namespace,
            "source_key": payload.source_key,
            "content_class": payload.content_class,
            "payload": payload.payload,
            "body": payload.body,
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

    policy_revision = await _declare_policy_revision(connection, context)
    content_id = uuid.uuid4()
    receipt_kind = (
        "pending_processing"
        if payload.content_class in PENDING_PROCESSING_CLASSES
        else "committed"
    )
    new_hash = content_hash(payload.content_class, payload.payload, payload.body)
    try:
        await connection.execute(
            """
            INSERT INTO cortex_core.content_items
                (scope_id, content_id, content_class, created_by_principal)
            VALUES ($1, $2, $3, $4)
            """,
            context.selected.scope_id,
            content_id,
            payload.content_class,
            context.principal.principal_id,
        )
        await connection.execute(
            """
            INSERT INTO cortex_core.content_revisions
                (scope_id, content_id, revision, payload, body_text, content_hash,
                 author_principal_id, source_connector_id, source_key,
                 source_observed_at)
            VALUES ($1, $2, 1, $3::jsonb, $4, $5, $6, $7, $8, $9)
            """,
            context.selected.scope_id,
            content_id,
            json.dumps(payload.payload, ensure_ascii=False, sort_keys=True),
            payload.body,
            new_hash,
            context.principal.principal_id,
            connector["connector_id"],
            payload.source_key,
            payload.source_observed_at,
        )
        await connection.execute(
            """
            INSERT INTO cortex_core.content_lexical_documents
                (scope_id, content_id, revision, source_text)
            VALUES ($1, $2, 1, $3)
            """,
            context.selected.scope_id,
            content_id,
            payload.body,
        )
        await connection.execute(
            """
            INSERT INTO cortex_core.content_outbox_events
                (event_id, scope_id, content_id, event_type, payload)
            VALUES ($1, $2, $3, 'content.ingested', $4::jsonb)
            """,
            uuid.uuid4(),
            context.selected.scope_id,
            content_id,
            json.dumps(
                {
                    "content_id": str(content_id),
                    "content_class": payload.content_class,
                    "connector_namespace": payload.connector_namespace,
                    "source_key": payload.source_key,
                    "revision": 1,
                    "policy_revision": policy_revision,
                }
            ),
        )
    except asyncpg.UniqueViolationError as exc:
        raise _problem("source_key_conflict", 409) from exc
    except asyncpg.PostgresError as exc:
        problem = _translate(exc)
        if problem is not None:
            raise problem from exc
        raise

    receipt = {
        "state": receipt_kind,
        "operation": operation,
        "content_id": str(content_id),
        "content_class": payload.content_class,
        "revision": 1,
        "content_hash": new_hash.hex(),
        "scope_id": str(context.selected.scope_id),
        "policy_revision": policy_revision,
        "provenance": {
            "connector_namespace": payload.connector_namespace,
            "source_key": payload.source_key,
            "source_observed_at": (
                payload.source_observed_at.isoformat()
                if payload.source_observed_at
                else None
            ),
        },
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind=receipt_kind,
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


def _revision_row(row: Any) -> dict[str, Any]:
    payload = row["payload"]
    return {
        "revision": row["revision"],
        "payload": json.loads(payload) if isinstance(payload, str) else payload,
        "body": row["body_text"],
        "content_hash": row["content_hash"].hex(),
        "author_principal_id": str(row["author_principal_id"]),
        "authored_at": row["authored_at"].isoformat(),
        "supersedes_revision": row["supersedes_revision"],
        "provenance": {
            "source_connector_id": (
                str(row["source_connector_id"]) if row["source_connector_id"] else None
            ),
            "source_key": row["source_key"],
            "source_observed_at": (
                row["source_observed_at"].isoformat()
                if row["source_observed_at"]
                else None
            ),
        },
    }


async def get_content(
    connection: asyncpg.Connection,
    content_id: uuid.UUID,
    revision: int | None,
    history: bool,
) -> dict[str, Any]:
    item = await connection.fetchrow(
        """
        SELECT i.scope_id, i.content_class, i.created_at, i.created_by_principal,
               cortex_core.content_current_status(i.scope_id, i.content_id) AS status
          FROM cortex_core.content_items AS i
         WHERE i.content_id = $1
        """,
        content_id,
    )
    if item is None:
        raise _problem("content_not_found", 404)

    if history:
        revisions = await connection.fetch(
            """
            SELECT revision, payload, body_text, content_hash, author_principal_id,
                   authored_at, supersedes_revision, source_connector_id, source_key,
                   source_observed_at
              FROM cortex_core.content_revisions
             WHERE scope_id = $1 AND content_id = $2
             ORDER BY revision
            """,
            item["scope_id"],
            content_id,
        )
        status_log = await connection.fetch(
            """
            SELECT status_seq, status, prior_status, superseded_by_content_id,
                   reason, changed_by_principal, changed_at
              FROM cortex_core.content_status_log
             WHERE scope_id = $1 AND content_id = $2
             ORDER BY status_seq
            """,
            item["scope_id"],
            content_id,
        )
        return {
            "content_id": str(content_id),
            "content_class": item["content_class"],
            "scope_id": str(item["scope_id"]),
            "status": item["status"],
            "created_at": item["created_at"].isoformat(),
            "created_by_principal": str(item["created_by_principal"]),
            "revisions": [_revision_row(row) for row in revisions],
            "status_history": [
                {
                    "status_seq": row["status_seq"],
                    "status": row["status"],
                    "prior_status": row["prior_status"],
                    "superseded_by_content_id": (
                        str(row["superseded_by_content_id"])
                        if row["superseded_by_content_id"]
                        else None
                    ),
                    "reason": row["reason"],
                    "changed_by_principal": str(row["changed_by_principal"]),
                    "changed_at": row["changed_at"].isoformat(),
                }
                for row in status_log
            ],
        }

    row = await connection.fetchrow(
        """
        SELECT revision, payload, body_text, content_hash, author_principal_id,
               authored_at, supersedes_revision, source_connector_id, source_key,
               source_observed_at
          FROM cortex_core.content_revisions
         WHERE scope_id = $1
           AND content_id = $2
           AND revision = COALESCE($3, (
                   SELECT max(revision)
                     FROM cortex_core.content_revisions AS latest
                    WHERE latest.scope_id = $1 AND latest.content_id = $2
               ))
        """,
        item["scope_id"],
        content_id,
        revision,
    )
    if row is None:
        raise _problem("content_not_found", 404)
    return {
        "content_id": str(content_id),
        "content_class": item["content_class"],
        "scope_id": str(item["scope_id"]),
        "status": item["status"],
        "created_at": item["created_at"].isoformat(),
        "created_by_principal": str(item["created_by_principal"]),
        "citation": {
            "scope_id": str(item["scope_id"]),
            "content_id": str(content_id),
            "revision": row["revision"],
        },
        **_revision_row(row),
    }


async def search_content(
    connection: asyncpg.Connection,
    query: str,
    limit: int,
    include_historical: bool,
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT r.scope_id, r.content_id, r.revision, i.content_class, r.body_text,
               r.content_hash, r.authored_at,
               cortex_core.content_current_status(r.scope_id, r.content_id) AS status,
               ts_rank_cd(d.search_document, plainto_tsquery('simple', $1)) AS score
          FROM cortex_core.content_lexical_documents AS d
          JOIN cortex_core.content_revisions AS r
            ON r.scope_id = d.scope_id
           AND r.content_id = d.content_id
           AND r.revision = d.revision
          JOIN cortex_core.content_items AS i
            ON i.scope_id = r.scope_id
           AND i.content_id = r.content_id
         WHERE d.search_document @@ plainto_tsquery('simple', $1)
           AND r.revision = (
               SELECT max(latest.revision)
                 FROM cortex_core.content_revisions AS latest
                WHERE latest.scope_id = r.scope_id
                  AND latest.content_id = r.content_id
           )
           AND ($2
                OR cortex_core.content_current_status(r.scope_id, r.content_id)
                   = 'current')
         ORDER BY score DESC, r.authored_at DESC, r.content_id
         LIMIT $3
        """,
        query,
        include_historical,
        limit,
    )
    return [
        {
            "content_id": str(row["content_id"]),
            "content_class": row["content_class"],
            "body": row["body_text"],
            "content_hash": row["content_hash"].hex(),
            "status": row["status"],
            "scope_id": str(row["scope_id"]),
            "authored_at": row["authored_at"].isoformat(),
            "citation": {
                "scope_id": str(row["scope_id"]),
                "content_id": str(row["content_id"]),
                "revision": row["revision"],
            },
            "evidence": {
                "source_revision": row["revision"],
                "retrieval_stage": "lexical",
                "projection_generation": "content-lexical-v1",
                "content_status": row["status"],
            },
        }
        for row in rows
    ]


async def declare_policy_revision(
    connection: asyncpg.Connection,
    context: ScopeContext,
) -> int:
    """Public alias for the write-path writer-policy revision declaration.

    Enforces the active roster/writer policy against the caller's membership
    and pins ``cortex.policy_revision`` transaction-locally so the database
    rechecks the same revision at canonical insert time. Returns the declared
    revision. Other modules must call this instead of mirroring its semantics.
    """
    return await _declare_policy_revision(connection, context)
