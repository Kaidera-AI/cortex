"""Worker-role availability reporting for discovery and usage cards.

Role names follow the agreed container naming plan
(``Program/Cortex/Release_v0.1.003/CORTEX-001_STANDALONE_DELIVERY/
CONTAINER_NAMING_PLAN.md``): ``doc`` (Document Processor, replaces the old
PDF worker, multi-format), ``embed``, ``graph`` and the optional media roles
``audio``/``video``/``vision``, which are listed only when explicitly
activated. Role state comes from the Processing module's published
``WORKER_ROLES`` metadata; without it a role is honestly unavailable.
"""

from __future__ import annotations

import os
from typing import Any

from .registry import OperationRegistry

CORE_WORKER_ROLES = ("doc", "embed", "graph")
MEDIA_WORKER_ROLES = ("audio", "video", "vision")
ACTIVATION_ENV = "CORTEX_V2_ACTIVATED_MEDIA_ROLES"
DOC_FORMATS_OPERATION = "processing.doc.formats"


def activated_media_roles(env: os._Environ[str] | dict[str, str] = os.environ
                          ) -> tuple[str, ...]:
    raw = env.get(ACTIVATION_ENV, "")
    if not raw.strip():
        return ()
    roles = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = [role for role in roles if role not in MEDIA_WORKER_ROLES]
    if unknown:
        raise ValueError(
            f"{ACTIVATION_ENV} may only activate {MEDIA_WORKER_ROLES}; "
            f"got {unknown}"
        )
    return tuple(dict.fromkeys(roles))


def resolve_worker_roles(
    registry: OperationRegistry,
    *,
    activated: tuple[str, ...] = (),
    formats_result: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    processing = registry.module_state("processing")
    declared: dict[str, dict[str, Any]] = {}
    if processing.state == "ready":
        for role in processing.worker_roles:
            declared[str(role["role"])] = dict(role)

    roles: dict[str, dict[str, Any]] = {}
    for name in (*CORE_WORKER_ROLES, *activated):
        if name in declared:
            entry = dict(declared[name])
            entry.setdefault("role", name)
        else:
            entry = {
                "role": name,
                "state": "unavailable",
                "reason": (
                    processing.reason
                    or "the processing module does not declare this role"
                ),
            }
        if name == "doc":
            if formats_result is not None and entry["state"] == "ready":
                entry["formats"] = formats_result.get("formats", [])
                entry["limits"] = formats_result.get("limits", {})
            else:
                entry["formats"] = {
                    "state": "unavailable",
                    "reason": (
                        f"{DOC_FORMATS_OPERATION} is not ready"
                        if entry["state"] == "ready"
                        else entry.get("reason", "doc role unavailable")
                    ),
                }
        roles[name] = entry
    return roles


async def load_doc_formats(
    connection: Any, context: Any, registry: OperationRegistry
) -> dict[str, Any] | None:
    """Call the Processing module's ``processing.doc.formats`` read operation
    through its published handler when it is ready; honest ``None`` otherwise.
    """
    try:
        operation = registry.get(DOC_FORMATS_OPERATION)
    except KeyError:
        return None
    if registry.module_state(operation.module).state != "ready":
        return None
    from ..store import ApiProblem

    try:
        return await operation.handler(connection, context, None, {})
    except ApiProblem:
        return None
