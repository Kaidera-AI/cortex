"""Versioned verification operation registry (integration contract)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import asyncpg

from ..store import ScopeContext
from . import records
from .models import RecordVerificationRequest
from .records import problem


def _read_filters(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    raise problem("invalid_filter")


def _path_uuid(path_params: dict[str, Any], name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(path_params[name]))
    except (KeyError, TypeError, ValueError) as exc:
        raise problem("invalid_filter") from exc


async def _verification_record(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: RecordVerificationRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await records.record_verification(
        connection, context, payload, idempotency_key
    )


async def _verification_record_get(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await records.get_verification(
        connection, context, _path_uuid(path_params, "verification_id")
    )


async def _verification_subject_list(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await records.list_for_subject(
        connection, context, _read_filters(payload)
    )


OPERATIONS: list[dict[str, Any]] = [
    {
        "operation_id": "verification.record",
        "method": "POST",
        "path": "/v1/verification/records",
        "kind": "scoped_write",
        "request_model": RecordVerificationRequest,
        "handler": _verification_record,
        "summary": "Record evidence-backed verification of a subject.",
        "usage": "Use when independently checking a work-product receipt, a "
        "handoff return or an explicit claim, citing exact source revisions "
        "and readback evidence; do not use to restate an agent's own report "
        "as verified - producers cannot verify their own receipts and "
        "unsupported claims must be recorded UNVERIFIABLE with a reason.",
    },
    {
        "operation_id": "verification.record.get",
        "method": "GET",
        "path": "/v1/verification/records/{verification_id}",
        "kind": "scoped_read",
        "request_model": None,
        "handler": _verification_record_get,
        "summary": "Read one verification record with its citations.",
        "usage": "Use when auditing how a verdict was reached and from which "
        "source revisions; do not treat the verdict text as a substitute for "
        "reading the cited evidence.",
    },
    {
        "operation_id": "verification.subject.list",
        "method": "GET",
        "path": "/v1/verification/subjects",
        "kind": "scoped_read",
        "request_model": None,
        "handler": _verification_subject_list,
        "summary": "List verification records for a subject, newest first.",
        "usage": "Use when checking whether independent verification exists "
        "for a receipt or handoff; do not assume absence of records means "
        "unverified-safe - it means not independently verified at all.",
    },
]
