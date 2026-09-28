"""Typed command receipts with idempotency for W1 operations.

Every mutation stores its canonical request digest and typed receipt in the
same transaction as its effects. The same key with the same payload replays
the stored receipt; the same key with a different payload is a typed conflict.
Token-bearing receipts store only a fingerprint; plaintext tokens are shown
exactly once by the initial response.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import asyncpg

from .store import ApiProblem


def request_digest(payload: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).digest()


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _receipt_json(value: str | dict[str, Any]) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else value


async def begin_command(
    connection: asyncpg.Connection,
    *,
    principal_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    digest: bytes,
    scope_id: uuid.UUID | None = None,
    installation_id: uuid.UUID | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    if (scope_id is None) == (installation_id is None):
        raise ApiProblem(500, "internal_error", "Receipt binding is ambiguous.")
    lock_key = ":".join(
        (
            str(principal_id),
            str(scope_id or installation_id),
            operation,
            idempotency_key,
        )
    )
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lock_key
    )
    previous = await connection.fetchrow(
        """
        SELECT request_hash, receipt
          FROM cortex_core.command_receipts
         WHERE principal_id = $1
           AND operation = $2
           AND idempotency_key = $3
        """,
        principal_id,
        operation,
        idempotency_key,
    )
    if previous is None:
        return None, False
    if not hmac.compare_digest(previous["request_hash"], digest):
        raise ApiProblem(
            409,
            "idempotency_key_reused",
            "Use a new key for a different request.",
        )
    return _receipt_json(previous["receipt"]), True


async def commit_receipt(
    connection: asyncpg.Connection,
    *,
    principal_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    digest: bytes,
    receipt_kind: str,
    receipt: dict[str, Any],
    scope_id: uuid.UUID | None = None,
    installation_id: uuid.UUID | None = None,
) -> None:
    await connection.execute(
        """
        INSERT INTO cortex_core.command_receipts
            (principal_id, installation_id, scope_id, operation, idempotency_key,
             request_hash, receipt_kind, receipt)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        """,
        principal_id,
        installation_id,
        scope_id,
        operation,
        idempotency_key,
        digest,
        receipt_kind,
        json.dumps(receipt, ensure_ascii=False, sort_keys=True),
    )
