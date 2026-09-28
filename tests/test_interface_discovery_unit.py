"""Unit tests for permission-filtered discovery, usage cards and worker-role
availability (R27, W5, F13, plus the doc/embed/graph role reporting)."""

from __future__ import annotations

import importlib
import types

from cortex_v2.interface.discovery import (
    operation_summary,
    render_usage_card,
    visible_operations,
)
from cortex_v2.interface.registry import build_registry
from cortex_v2.interface.roles import (
    activated_media_roles,
    resolve_worker_roles,
)

COORD = "cortex_v2.coordination.operations"
PROC = "cortex_v2.processing.operations"


def entry(op_id: str, **overrides) -> dict:
    base = {
        "operation_id": op_id,
        "method": "POST",
        "path": "/v1/" + op_id.replace(".", "-"),
        "kind": "scoped_write",
        "request_model": None,
        "handler": _noop,
        "summary": f"{op_id} summary",
        "usage": {"purpose": "p", "use_when": "u", "avoid_when": "a"},
    }
    base.update(overrides)
    return base


async def _noop(*args, **kwargs):  # pragma: no cover
    raise AssertionError("stub handler must not be invoked")


async def _formats_handler(connection, context, payload, path_params):
    return {
        "formats": [
            {"name": "pdf", "tested": True, "max_bytes": 52_428_800},
            {"name": "docx", "tested": True, "max_bytes": 26_214_400},
        ],
        "limits": {"max_pages": 2000},
    }


def module_with(operations, **attrs) -> types.ModuleType:
    module = types.ModuleType("stub_operations")
    module.OPERATIONS = operations
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


def importer_with(modules):
    def importer(name):
        if name in modules:
            return modules[name]
        if name.startswith(("cortex_v2.interface", "cortex_v2.ingest")):
            return importlib.import_module(name)
        raise ImportError(name)

    return importer


def test_write_operations_hidden_without_write_grant():
    registry = build_registry(importer=importer_with({}))
    visible = visible_operations(registry, can_write=False, is_owner=False)
    ids = {item["operation_id"] for item in visible}
    assert "context.prepare" not in ids
    assert "memory.record" not in ids
    assert "capability.discover" in ids
    assert "content.inspect" in ids


def test_owner_operations_only_visible_to_owner():
    registry = build_registry(importer=importer_with({}))
    non_owner = {item["operation_id"] for item in
                 visible_operations(registry, can_write=True, is_owner=False)}
    owner = {item["operation_id"] for item in
             visible_operations(registry, can_write=True, is_owner=True)}
    assert "auth.enroll_principal" not in non_owner
    assert "auth.enroll_principal" in owner
    assert "registry.rename_scope" in owner


def test_unavailable_module_operations_visible_with_reason_never_ready():
    registry = build_registry(importer=importer_with({}))
    summaries = {
        item["operation_id"]: item
        for item in visible_operations(registry, can_write=True, is_owner=True)
    }
    for module in ("coordination", "processing", "retrieval", "feed", "verification"):
        state = registry.module_state(module)
        assert state.state == "unavailable"
    # Modules that never loaded have no operations to list; the module report
    # itself must expose the reason.
    report = {m.module: m for m in registry.modules}
    assert report["coordination"].reason
    for item in summaries.values():
        assert item["state"] in ("ready", "unavailable")
        if item["state"] == "unavailable":
            assert item["reason"]


def test_operation_summary_shape():
    registry = build_registry(importer=importer_with({}))
    summary = operation_summary(registry.get("capability.discover"), "ready", None)
    assert summary["operation_id"] == "capability.discover"
    assert summary["method"] == "GET"
    assert summary["path"] == "/v1/capabilities"
    assert summary["kind"] == "scoped_read"
    assert summary["cost_class"]
    assert summary["card"] == "/v1/capabilities/capability.discover"


def test_usage_card_reports_guidance_and_evidence_contract():
    registry = build_registry(importer=importer_with({}))
    card = render_usage_card(
        registry.get("context.prepare"),
        "ready",
        None,
        worker_roles={"doc": {"state": "unavailable"}},
    )
    assert card["operation_id"] == "context.prepare"
    assert card["use_when"]
    assert card["avoid_when"]
    assert card["cost_class"]
    assert card["freshness"]
    assert card["evidence_contract"]
    assert card["failure_codes"]
    assert card["recovery_actions"]
    assert "examples" in card
    assert "counterexamples" in card
    assert card["worker_roles"]["doc"]["state"] == "unavailable"


def test_worker_roles_core_present_media_only_when_activated():
    registry = build_registry(importer=importer_with({}))
    roles = resolve_worker_roles(registry, activated=(), formats_result=None)
    assert set(roles) == {"doc", "embed", "graph"}
    for role in roles.values():
        assert role["state"] == "unavailable"
        assert role["reason"]
    with_media = resolve_worker_roles(
        registry, activated=("audio", "vision"), formats_result=None
    )
    assert set(with_media) == {"doc", "embed", "graph", "audio", "vision"}
    assert "video" not in with_media


def test_worker_roles_use_processing_metadata_when_ready():
    registry = build_registry(
        importer=importer_with(
            {
                PROC: module_with(
                    [entry("processing.doc.formats", kind="scoped_read",
                           handler=_formats_handler)],
                    WORKER_ROLES=[
                        {"role": "doc", "state": "ready",
                         "limits": {"max_pages": 2000}},
                        {"role": "embed", "state": "blocked",
                         "reason": "no embedding space configured"},
                    ],
                )
            }
        )
    )
    formats = {"formats": [{"name": "pdf", "tested": True}], "limits": {}}
    roles = resolve_worker_roles(registry, activated=(), formats_result=formats)
    assert roles["doc"]["state"] == "ready"
    assert roles["doc"]["formats"] == formats["formats"]
    assert roles["embed"]["state"] == "blocked"
    assert roles["embed"]["reason"] == "no embedding space configured"
    assert roles["graph"]["state"] == "unavailable"


def test_activated_media_roles_env_parsing():
    assert activated_media_roles({}) == ()
    assert activated_media_roles({"CORTEX_V2_ACTIVATED_MEDIA_ROLES": "audio, video"}) \
        == ("audio", "video")
    try:
        activated_media_roles({"CORTEX_V2_ACTIVATED_MEDIA_ROLES": "doc"})
    except ValueError as exc:
        assert "doc" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("core role must not be activatable as media role")
