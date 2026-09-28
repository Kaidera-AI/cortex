"""One versioned operation registry for every Cortex v2 transport.

Aggregates the ``OPERATIONS`` lists published by each module package
(interface, ingest, coordination, processing, retrieval, feed, verification)
plus the descriptor list for W1's already-mounted routes. HTTP schemas, CLI
subcommands and MCP tools are all generated from this single registry, so the
three transports share operation ids and semantics (R23/F05).

A module whose ``operations`` module cannot be imported, or whose entries
violate the integration contract, is reported as ``unavailable`` with an
actionable reason — never as ready.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from .descriptors import W1_MODULE, w1_descriptors

REGISTRY_VERSION = "cortex.operations.v2-w2.1"

MODULE_SOURCES: tuple[str, ...] = (
    "interface",
    "ingest",
    "coordination",
    "processing",
    "retrieval",
    "feed",
    "verification",
    "ops",
)

REQUIRED_KEYS: tuple[str, ...] = (
    "operation_id",
    "method",
    "path",
    "kind",
    "request_model",
    "handler",
    "summary",
    "usage",
)
KINDS = ("scoped_write", "scoped_read")
METHODS = ("GET", "POST")
AUTHORITIES = (
    "unauthenticated",
    "recovery_token",
    "authenticated",
    "scope_read",
    "scope_write",
    "installation_owner",
)
OPERATION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_-]+)+$")
PATH_PARAM_PATTERN = re.compile(r"\{(\w+)\}")

USAGE_DEFAULTS: dict[str, Any] = {
    "purpose": "",
    "use_when": "not declared by the owning module",
    "avoid_when": "not declared by the owning module",
    "effects": None,
    "prerequisites": (),
    "cost_class": "unspecified",
    "freshness": "not declared",
    "evidence_contract": "not declared by the owning module",
    "failure_codes": (),
    "recovery_actions": (),
    "examples": (),
    "counterexamples": (),
    "worker_roles": (),
}


@dataclass(frozen=True, slots=True)
class NormalizedOperation:
    operation_id: str
    module: str
    method: str
    path: str
    path_params: tuple[str, ...]
    kind: str
    authority: str
    requires_scope: bool
    requires_idempotency_key: bool
    request_model: type | None
    handler: Callable[..., Any] | None
    summary: str
    usage: dict[str, Any] = field(compare=False)
    mounted: str = "registry"
    query_params: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ModuleAvailability:
    module: str
    state: str
    reason: str | None
    operation_ids: tuple[str, ...]
    worker_roles: tuple[dict[str, Any], ...] = ()


def normalize_usage(usage: object) -> dict[str, Any]:
    if isinstance(usage, str):
        source: dict[str, Any] = {"summary": usage, "purpose": usage}
    elif isinstance(usage, Mapping):
        source = dict(usage)
    else:
        raise ValueError("usage must be a dict or a string")
    normalized = dict(USAGE_DEFAULTS)
    normalized.update(source)
    if not normalized.get("summary"):
        normalized["summary"] = normalized.get("purpose") or ""
    if normalized["effects"] is None:
        normalized["effects"] = "not declared"
    for key in ("prerequisites", "failure_codes", "recovery_actions",
                "examples", "counterexamples", "worker_roles"):
        value = normalized[key]
        if isinstance(value, (list, tuple)):
            normalized[key] = tuple(value)
    return normalized


def _validate_entry(entry: Any, index: int) -> None:
    if not isinstance(entry, Mapping):
        raise ValueError(f"entry {index} is not a mapping")
    for key in REQUIRED_KEYS:
        if key not in entry:
            raise ValueError(f"entry {index} is missing required key {key!r}")
    operation_id = entry["operation_id"]
    if not isinstance(operation_id, str) or not OPERATION_ID_PATTERN.fullmatch(
        operation_id
    ):
        raise ValueError(f"entry {index} has an invalid operation_id")
    if entry["method"] not in METHODS:
        raise ValueError(f"{operation_id}: method must be one of {METHODS}")
    if entry["kind"] not in KINDS:
        raise ValueError(f"{operation_id}: kind must be one of {KINDS}")
    if not isinstance(entry["path"], str) or not entry["path"].startswith("/"):
        raise ValueError(f"{operation_id}: path must start with '/'")
    authority = entry.get("authority")
    if authority is not None and authority not in AUTHORITIES:
        raise ValueError(f"{operation_id}: unknown authority {authority!r}")
    model = entry["request_model"]
    if model is not None and not (
        isinstance(model, type) and hasattr(model, "model_json_schema")
    ):
        raise ValueError(f"{operation_id}: request_model must be a pydantic model")
    handler = entry["handler"]
    if handler is not None and not callable(handler):
        raise ValueError(f"{operation_id}: handler must be callable or None")
    try:
        normalize_usage(entry["usage"])
    except ValueError as exc:
        raise ValueError(f"{operation_id}: {exc}") from exc
    query_params = entry.get("query_params", ())
    if not isinstance(query_params, (list, tuple)):
        raise ValueError(f"{operation_id}: query_params must be a list")


def _normalize_entry(entry: Mapping[str, Any], module: str) -> NormalizedOperation:
    operation_id = entry["operation_id"]
    kind = entry["kind"]
    authority = entry.get("authority") or (
        "scope_write" if kind == "scoped_write" else "scope_read"
    )
    requires_scope = entry.get("requires_scope")
    if requires_scope is None:
        requires_scope = authority in ("scope_read", "scope_write")
    requires_key = entry.get("requires_idempotency_key")
    if requires_key is None:
        requires_key = kind == "scoped_write"
    handler = entry["handler"]
    return NormalizedOperation(
        operation_id=operation_id,
        module=module,
        method=entry["method"],
        path=entry["path"],
        path_params=tuple(PATH_PARAM_PATTERN.findall(entry["path"])),
        kind=kind,
        authority=authority,
        requires_scope=bool(requires_scope),
        requires_idempotency_key=bool(requires_key),
        request_model=entry["request_model"],
        handler=handler,
        summary=entry["summary"],
        usage=normalize_usage(entry["usage"]),
        mounted="app" if handler is None else "registry",
        query_params=tuple(entry.get("query_params", ()) or ()),
    )


def _request_schema(model: type | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return model.model_json_schema()


class OperationRegistry:
    """Immutable, versioned view of every known operation."""

    def __init__(
        self,
        operations: dict[str, NormalizedOperation],
        modules: tuple[ModuleAvailability, ...],
        digest: str,
    ) -> None:
        self.operations = dict(operations)
        self.modules = tuple(modules)
        self.digest = digest

    @property
    def version(self) -> str:
        return REGISTRY_VERSION

    def get(self, operation_id: str) -> NormalizedOperation:
        return self.operations[operation_id]

    def operations_sorted(self) -> list[NormalizedOperation]:
        return [self.operations[key] for key in sorted(self.operations)]

    def module_state(self, module: str) -> ModuleAvailability:
        for entry in self.modules:
            if entry.module == module:
                return entry
        raise KeyError(module)


def _normalize_worker_roles(raw: Any) -> tuple[dict[str, Any], ...]:
    """Optional module metadata; tolerated shapes never fail a module.

    ``[{role, state, ...}]`` declares states explicitly; ``("doc", ...)``
    declares role names whose operation surface ships with the ready module.
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    roles: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and "role" in item and "state" in item:
            roles.append(dict(item))
        elif isinstance(item, str) and item:
            roles.append({"role": item, "state": "ready"})
    return tuple(roles)


def _module_name(import_name: str) -> str:
    return import_name.removeprefix("cortex_v2.").removesuffix(".operations")


def _compute_digest(operations: Iterable[NormalizedOperation]) -> str:
    listing = [
        {
            "operation_id": op.operation_id,
            "module": op.module,
            "method": op.method,
            "path": op.path,
            "kind": op.kind,
            "authority": op.authority,
            "requires_scope": op.requires_scope,
            "requires_idempotency_key": op.requires_idempotency_key,
            "request_schema": _request_schema(op.request_model),
        }
        for op in sorted(operations, key=lambda item: item.operation_id)
    ]
    payload = json.dumps(
        {"version": REGISTRY_VERSION, "operations": listing},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_registry(
    *, importer: Callable[[str], ModuleType] = importlib.import_module
) -> OperationRegistry:
    operations: dict[str, NormalizedOperation] = {}
    modules: list[ModuleAvailability] = []

    def register(module: str, entries: Iterable[Mapping[str, Any]],
                 worker_roles: tuple[dict[str, Any], ...] = ()) -> None:
        staged: list[NormalizedOperation] = []
        try:
            for index, entry in enumerate(entries):
                _validate_entry(entry, index)
                staged.append(_normalize_entry(entry, module))
        except ValueError as exc:
            modules.append(
                ModuleAvailability(
                    module=module,
                    state="unavailable",
                    reason=f"contract_violation: {exc}",
                    operation_ids=(),
                    worker_roles=(),
                )
            )
            return
        for operation in staged:
            if operation.operation_id in operations:
                owner = operations[operation.operation_id].module
                modules.append(
                    ModuleAvailability(
                        module=module,
                        state="unavailable",
                        reason=(
                            "contract_violation: duplicate operation_id "
                            f"{operation.operation_id!r} already published by "
                            f"{owner!r}"
                        ),
                        operation_ids=(),
                        worker_roles=(),
                    )
                )
                return
        for operation in staged:
            operations[operation.operation_id] = operation
        modules.append(
            ModuleAvailability(
                module=module,
                state="ready",
                reason=None,
                operation_ids=tuple(op.operation_id for op in staged),
                worker_roles=worker_roles,
            )
        )

    register(W1_MODULE, w1_descriptors())

    for source in MODULE_SOURCES:
        import_name = f"cortex_v2.{source}.operations"
        try:
            module = importer(import_name)
        except ImportError as exc:
            modules.append(
                ModuleAvailability(
                    module=source,
                    state="unavailable",
                    reason=f"{source} module is not importable: {exc}",
                    operation_ids=(),
                )
            )
            continue
        except Exception as exc:  # a broken module is never ready
            modules.append(
                ModuleAvailability(
                    module=source,
                    state="unavailable",
                    reason=f"{source} module failed to import: "
                    f"{type(exc).__name__}: {exc}",
                    operation_ids=(),
                )
            )
            continue
        entries = getattr(module, "OPERATIONS", None)
        if not isinstance(entries, (list, tuple)):
            modules.append(
                ModuleAvailability(
                    module=source,
                    state="unavailable",
                    reason=(
                        "contract_violation: operations module does not publish "
                        "an OPERATIONS list"
                    ),
                    operation_ids=(),
                )
            )
            continue
        worker_roles = _normalize_worker_roles(getattr(module, "WORKER_ROLES", ()))
        register(source, entries, worker_roles)

    return OperationRegistry(
        operations=operations,
        modules=tuple(modules),
        digest=_compute_digest(operations.values()),
    )


# Importability (module state) and mount state are different facts. A module
# can publish a valid OPERATIONS list while the composition root has not
# mounted its routes yet; discovery must not advertise such operations as
# ready. Mount truth comes ONLY from explicit marking - the API composition
# root calls ``mark_modules_mounted`` after mounting (including
# ``identity_memory`` for the W1 routes in ``create_app``) - or from the
# deployment env ``CORTEX_V2_MOUNTED_MODULES`` (comma-separated). A fresh
# CLI/MCP/worker process marks nothing and therefore reports everything as
# unmounted until told otherwise; nothing is mounted by default.
MOUNT_ENV = "CORTEX_V2_MOUNTED_MODULES"
_marked_mounted: set[str] = set()


def mark_modules_mounted(names: Iterable[str]) -> None:
    _marked_mounted.update(names)


def clear_mounted_marks() -> None:
    _marked_mounted.clear()


def mounted_modules(
    env: Mapping[str, str] | None = None,
) -> frozenset[str]:
    environment = os.environ if env is None else env
    names = set(_marked_mounted)
    raw = environment.get(MOUNT_ENV, "")
    names |= {part.strip() for part in raw.split(",") if part.strip()}
    return frozenset(names)


_REGISTRY: OperationRegistry | None = None


def get_registry() -> OperationRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return _REGISTRY


def reset_registry_cache() -> None:
    global _REGISTRY
    _REGISTRY = None
