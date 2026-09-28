"""Shared command scaffolding for coordination use cases.

One digest/begin/finish path for every scoped write: the idempotency receipt
is bound to principal + operation + key inside the selected scope, exactly
like the W1 content commands, and the receipt commits in the same
transaction as the canonical state change, audit row and outbox event.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ScopeContext


def begin_digest(
    operation: str, context: ScopeContext, parts: dict[str, Any]
) -> bytes:
    return request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            **parts,
        }
    )


async def begin(
    connection: asyncpg.Connection,
    context: ScopeContext,
    operation: str,
    idempotency_key: str,
    digest: bytes,
) -> tuple[dict[str, Any] | None, bool]:
    return await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )


async def finish(
    connection: asyncpg.Connection,
    context: ScopeContext,
    operation: str,
    idempotency_key: str,
    digest: bytes,
    receipt: dict[str, Any],
    status: int,
) -> tuple[int, dict[str, Any], bool]:
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
    return status, receipt, False


def committed_receipt(
    operation: str,
    context: ScopeContext,
    policy_revision: int,
    aggregate_kind: str,
    aggregate_id: uuid.UUID,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "state": "committed",
        "operation": operation,
        "aggregate_kind": aggregate_kind,
        "aggregate_id": str(aggregate_id),
        "scope_id": str(context.selected.scope_id),
        "policy_revision": policy_revision,
        **extra,
    }
