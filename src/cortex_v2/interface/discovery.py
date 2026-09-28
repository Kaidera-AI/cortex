"""Authenticated, permission-filtered capability discovery and usage cards
(R27, W5, F13).

Discovery never grants authority: execution rechecks permission and current
policy. Authorization-denied operations are omitted entirely (denial is not
presented as a missing feature), while operations of modules that are not
deployed stay visible with an actionable ``unavailable`` reason for callers
entitled to know.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from ..identity import list_privileged_actions
from ..store import ApiProblem, Principal, ScopeContext
from .registry import (
    NormalizedOperation,
    OperationRegistry,
    get_registry,
    mounted_modules,
)

UNMOUNTED_REASON = (
    "module is registered but its routes are not mounted in this deployment"
)
from .roles import activated_media_roles, load_doc_formats, resolve_worker_roles


async def probe_owner(connection: asyncpg.Connection, principal: Principal) -> bool:
    """Installation-owner probe through the frozen W1 use case.

    Runs inside a savepoint so a non-owner denial does not abort the caller's
    transaction.
    """
    try:
        async with connection.transaction():
            await list_privileged_actions(connection, principal, 1)
        return True
    except ApiProblem as exc:
        if exc.code == "owner_authority_required":
            return False
        raise
    except asyncpg.PostgresError as exc:
        if (exc.sqlstate or "") == "42501":
            return False
        raise


def _visible(authority: str, *, can_write: bool, is_owner: bool) -> bool:
    if authority == "scope_write":
        return can_write
    if authority == "installation_owner":
        return is_owner
    return True


def operation_summary(
    operation: NormalizedOperation, state: str, reason: str | None
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "operation_id": operation.operation_id,
        "module": operation.module,
        "state": state,
        "method": operation.method,
        "path": operation.path,
        "kind": operation.kind,
        "authority": operation.authority,
        "effects": operation.usage["effects"],
        "cost_class": operation.usage["cost_class"],
        "summary": operation.summary,
        "card": f"/v1/capabilities/{operation.operation_id}",
    }
    if reason:
        summary["reason"] = reason
    return summary


def effective_module_state(
    registry: OperationRegistry,
    module_name: str,
    *,
    mounted: frozenset[str] | None = None,
) -> tuple[str, str | None]:
    """Serveability state: importable AND actually mounted, else unavailable
    with an actionable reason."""
    module = registry.module_state(module_name)
    mounted_set = mounted_modules() if mounted is None else mounted
    if module.state == "ready" and module_name not in mounted_set:
        return "unavailable", UNMOUNTED_REASON
    return module.state, module.reason


def visible_operations(
    registry: OperationRegistry,
    *,
    can_write: bool,
    is_owner: bool,
    mounted: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    summaries = []
    for operation in registry.operations_sorted():
        if not _visible(operation.authority, can_write=can_write,
                        is_owner=is_owner):
            continue
        state, reason = effective_module_state(
            registry, operation.module, mounted=mounted
        )
        summaries.append(operation_summary(operation, state, reason))
    return summaries


def render_usage_card(
    operation: NormalizedOperation,
    state: str,
    reason: str | None,
    *,
    worker_roles: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usage = dict(operation.usage)
    for key in ("prerequisites", "failure_codes", "recovery_actions",
                "examples", "counterexamples"):
        usage[key] = list(usage.get(key) or ())
    declared_roles = list(usage.pop("worker_roles", ()) or ())
    card: dict[str, Any] = {
        "operation_id": operation.operation_id,
        "module": operation.module,
        "state": state,
        "method": operation.method,
        "path": operation.path,
        "kind": operation.kind,
        "authority": operation.authority,
        "requires_scope": operation.requires_scope,
        "requires_idempotency_key": operation.requires_idempotency_key,
        "summary": operation.summary,
        "request_schema": (
            operation.request_model.model_json_schema()
            if operation.request_model is not None
            else None
        ),
        "required_worker_roles": declared_roles,
        "worker_roles": dict(worker_roles or {}),
        **usage,
    }
    if reason:
        card["reason"] = reason
    return card


async def discover_capabilities(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    registry = get_registry()
    mounted = mounted_modules()
    is_owner = await probe_owner(connection, context.principal)
    can_write = context.selected.can_write and (
        context.selected.kind != "shared" or context.selected.can_publish
    )
    formats_result = await load_doc_formats(connection, context, registry)
    roles = resolve_worker_roles(
        registry,
        activated=activated_media_roles(),
        formats_result=formats_result,
    )
    module_reports = []
    for module in registry.modules:
        state, reason = effective_module_state(
            registry, module.module, mounted=mounted
        )
        module_reports.append(
            {
                "module": module.module,
                "state": state,
                "reason": reason,
                "importable": module.state == "ready",
                "operations": list(module.operation_ids),
            }
        )
    return {
        "registry_version": registry.version,
        "registry_digest": registry.digest,
        "modules": module_reports,
        "worker_roles": roles,
        "operations": visible_operations(
            registry, can_write=can_write, is_owner=is_owner, mounted=mounted
        ),
    }


async def capability_card(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    registry = get_registry()
    operation_id = str(path_params["operation_id"])
    try:
        operation = registry.get(operation_id)
    except KeyError:
        raise ApiProblem(
            404, "operation_not_found",
            "The operation is unknown to this installation.",
        ) from None
    is_owner = False
    can_write = context.selected.can_write and (
        context.selected.kind != "shared" or context.selected.can_publish
    )
    if operation.authority == "installation_owner":
        is_owner = await probe_owner(connection, context.principal)
    if not _visible(operation.authority, can_write=can_write,
                    is_owner=is_owner):
        # Authorization denial is not presented as a missing feature, and an
        # entitled-but-unauthorized caller learns nothing about the operation.
        raise ApiProblem(
            404, "operation_not_found",
            "The operation is unknown to this installation.",
        )
    state, reason = effective_module_state(registry, operation.module)
    formats_result = None
    if operation.usage.get("worker_roles"):
        formats_result = await load_doc_formats(connection, context, registry)
    roles = resolve_worker_roles(
        registry,
        activated=activated_media_roles(),
        formats_result=formats_result,
    )
    relevant_roles = {
        name: roles[name]
        for name in operation.usage.get("worker_roles") or ()
        if name in roles
    } or roles
    return render_usage_card(
        operation,
        state,
        reason,
        worker_roles=relevant_roles,
    )
