"""Unit tests for the versioned operation registry (R23/F05).

Pure tests: a stub importer simulates peer modules that are present,
missing, or contract-violating. No database, no network.
"""

from __future__ import annotations

import importlib
import types

import pytest

from cortex_v2.interface.registry import (
    REGISTRY_VERSION,
    build_registry,
    normalize_usage,
)


def entry(op_id: str, **overrides) -> dict:
    base = {
        "operation_id": op_id,
        "method": "POST",
        "path": "/v1/" + op_id.replace(".", "-"),
        "kind": "scoped_write",
        "request_model": None,
        "handler": _noop_handler,
        "summary": f"{op_id} summary",
        "usage": {
            "purpose": "test purpose",
            "use_when": "tests exercise it",
            "avoid_when": "never in tests",
        },
    }
    base.update(overrides)
    return base


async def _noop_handler(*args, **kwargs):  # pragma: no cover - never invoked
    raise AssertionError("stub handler must not be invoked")


def module_with(operations, **attrs) -> types.ModuleType:
    module = types.ModuleType("stub_operations")
    module.OPERATIONS = operations
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


def importer_with(modules: dict[str, types.ModuleType]):
    def importer(name: str):
        if name in modules:
            return modules[name]
        if name.startswith(("cortex_v2.interface", "cortex_v2.ingest")):
            return importlib.import_module(name)
        raise ImportError(f"no module named {name!r}")

    return importer


COORD = "cortex_v2.coordination.operations"
PROC = "cortex_v2.processing.operations"


def test_available_module_is_ready_and_missing_module_is_unavailable():
    registry = build_registry(
        importer=importer_with(
            {COORD: module_with([entry("coordination.claim")])}
        )
    )
    coordination = registry.module_state("coordination")
    processing = registry.module_state("processing")
    assert coordination.state == "ready"
    assert coordination.operation_ids == ("coordination.claim",)
    assert processing.state == "unavailable"
    assert processing.reason
    assert "processing" in processing.reason
    assert "coordination.claim" in registry.operations
    assert registry.get("coordination.claim").module == "coordination"


def test_unavailable_module_operations_never_appear_ready():
    registry = build_registry(importer=importer_with({}))
    for name in ("coordination", "processing", "retrieval", "feed", "verification"):
        state = registry.module_state(name)
        assert state.state == "unavailable"
        assert state.operation_ids == ()


def test_broken_module_import_is_unavailable_not_ready():
    def importer(name: str):
        raise RuntimeError("boom")

    registry = build_registry(importer=importer)
    for module in registry.modules:
        assert module.state != "ready" or module.module in (
            "identity_memory",
            "interface",
            "ingest",
        )
    assert registry.module_state("coordination").state == "unavailable"
    assert registry.module_state("coordination").reason


def test_contract_violation_marks_module_unavailable():
    broken = entry("coordination.claim")
    del broken["kind"]
    registry = build_registry(importer=importer_with({COORD: module_with([broken])}))
    state = registry.module_state("coordination")
    assert state.state == "unavailable"
    assert "kind" in (state.reason or "")
    assert "coordination.claim" not in registry.operations


def test_duplicate_operation_id_marks_later_module_unavailable():
    registry = build_registry(
        importer=importer_with(
            {
                COORD: module_with([entry("shared.op")]),
                PROC: module_with([entry("shared.op", path="/v1/other")]),
            }
        )
    )
    assert registry.module_state("coordination").state == "ready"
    processing = registry.module_state("processing")
    assert processing.state == "unavailable"
    assert "duplicate" in (processing.reason or "")
    assert registry.get("shared.op").module == "coordination"


def test_w1_routes_are_described_and_not_mountable():
    registry = build_registry(importer=importer_with({}))
    for op_id in (
        "memory.record",
        "memory.inspect",
        "memory.search.lexical",
        "content.ingest",
        "content.search.lexical",
        "auth.enroll_principal",
        "registry.rename_scope",
        "protocol.descriptor",
    ):
        operation = registry.get(op_id)
        assert operation.module == "identity_memory"
        assert operation.handler is None
        assert operation.mounted == "app"
    assert registry.module_state("identity_memory").state == "ready"


def test_w1_descriptor_transport_metadata():
    registry = build_registry(importer=importer_with({}))
    record = registry.get("memory.record")
    assert record.method == "POST"
    assert record.path == "/v1/memory/records"
    assert record.kind == "scoped_write"
    assert record.requires_idempotency_key is True
    assert record.requires_scope is True
    ingest = registry.get("content.ingest")
    assert ingest.requires_idempotency_key is False
    enroll = registry.get("auth.enroll_principal")
    assert enroll.requires_scope is False
    assert enroll.authority == "installation_owner"
    principal = registry.get("auth.principal")
    assert principal.requires_scope is False
    assert principal.kind == "scoped_read"


def test_own_module_operations_are_registered():
    registry = build_registry(importer=importer_with({}))
    for op_id in (
        "capability.discover",
        "capability.card",
        "context.prepare",
        "persona.enact",
        "rule.enact",
        "skill.enact",
        "skill.bind",
        "skill.bindings",
        "skill.get",
        "harness.preview",
        "harness.apply",
        "harness.drift",
        "harness.rollback",
        "compatibility.profiles",
        "ingest.session",
        "ingest.message",
        "ingest.local_state",
        "ingest.diary",
        "ingest.save_chat",
        "ingest.run",
    ):
        operation = registry.get(op_id)
        assert operation.handler is not None
        assert operation.mounted == "registry"
    assert registry.get("context.prepare").kind == "scoped_write"
    assert registry.get("context.prepare").requires_idempotency_key is True
    assert registry.get("harness.preview").kind == "scoped_read"
    bindings = registry.get("skill.bindings")
    assert bindings.kind == "scoped_read"
    assert bindings.method == "GET"
    assert bindings.path == "/v1/context/skill-bindings"


def test_path_params_extracted_from_templates():
    registry = build_registry(importer=importer_with({}))
    assert registry.get("content.inspect").path_params == ("content_id",)
    assert registry.get("registry.rename_scope").path_params == ("alias",)
    assert registry.get("capability.card").path_params == ("operation_id",)
    assert registry.get("memory.record").path_params == ()


def test_digest_is_stable_and_sensitive():
    left = build_registry(importer=importer_with({COORD: module_with([entry("x.op")])}))
    right = build_registry(
        importer=importer_with({COORD: module_with([entry("x.op")])})
    )
    assert left.digest == right.digest
    changed = build_registry(
        importer=importer_with(
            {COORD: module_with([entry("x.op", path="/v1/changed")])}
        )
    )
    assert changed.digest != left.digest
    assert len(left.digest) == 64


def test_registry_version_is_versioned_namespace():
    registry = build_registry(importer=importer_with({}))
    assert registry.version == REGISTRY_VERSION
    assert registry.version.startswith("cortex.operations.")


def test_module_worker_role_metadata_is_captured():
    registry = build_registry(
        importer=importer_with(
            {
                PROC: module_with(
                    [entry("processing.doc.formats", kind="scoped_read")],
                    WORKER_ROLES=[{"role": "doc", "state": "ready"}],
                )
            }
        )
    )
    processing = registry.module_state("processing")
    assert processing.state == "ready"
    assert {"role": "doc", "state": "ready"} in processing.worker_roles


def test_normalize_usage_accepts_string_and_dict():
    from_string = normalize_usage("short summary")
    assert from_string["summary"] == "short summary"
    assert from_string["use_when"]
    assert from_string["avoid_when"]
    from_dict = normalize_usage({"purpose": "p", "use_when": "u", "avoid_when": "a"})
    assert from_dict["purpose"] == "p"
    assert "cost_class" in from_dict
    assert "evidence_contract" in from_dict


def test_invalid_kind_and_method_rejected():
    registry = build_registry(
        importer=importer_with(
            {COORD: module_with([entry("coordination.bad", kind="global_write")])}
        )
    )
    assert registry.module_state("coordination").state == "unavailable"


def test_registry_is_iterable_in_stable_order():
    registry = build_registry(importer=importer_with({}))
    ids = [operation.operation_id for operation in registry.operations_sorted()]
    assert ids == sorted(ids)
    with pytest.raises(KeyError):
        registry.get("does.not.exist")
