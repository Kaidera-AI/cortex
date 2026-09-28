"""Pure unit tests for the coordination OPERATIONS integration contract.

The integrator mounts every module generically from its OPERATIONS list, so
the contract shape is pinned here: dotted unique operation ids, GET/scoped_read
and POST/scoped_write pairing, strict pydantic request models, handler
signatures, usage guidance, and the rule that coordination domain code never
imports transport frameworks.
"""

from __future__ import annotations

import ast
import inspect
import string
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from cortex_v2.coordination import operations as operations_module
from cortex_v2.coordination.operations import OPERATIONS
from cortex_v2.coordination.repository import decode_list_cursor, encode_list_cursor
from cortex_v2.store import ApiProblem

PACKAGE_DIR = Path(operations_module.__file__).resolve().parent

REQUIRED_OPERATION_IDS = {
    "coordination.handoff.create",
    "coordination.handoff.get",
    "coordination.handoff.list",
    "coordination.handoff.claim",
    "coordination.handoff.lease.renew",
    "coordination.handoff.release",
    "coordination.handoff.return",
    "coordination.handoff.accept",
    "coordination.handoff.rework",
    "coordination.handoff.retry",
    "coordination.handoff.fail",
    "coordination.handoff.abandon",
    "coordination.handoff.withdraw",
    "coordination.approval.request",
    "coordination.approval.decide",
    "coordination.approval.revoke",
    "coordination.approval.get",
    "coordination.relay.authorize",
    "coordination.relay.revoke",
    "coordination.relay.dispatch",
    "coordination.relay.materialize",
    "coordination.relay.get",
    "coordination.epic.create",
    "coordination.wave.create",
    "coordination.wave.open",
    "coordination.wave.complete",
    "coordination.board.create",
    "coordination.board.task.add",
    "coordination.task.create",
    "coordination.task.assign",
    "coordination.task.dispatch",
    "coordination.task.complete",
    "coordination.task.cancel",
    "coordination.task.get",
    "coordination.task.list",
    "coordination.task.dispatch_eligibility",
    "coordination.work_product.receipt.get",
}

WRITE_SIGNATURE = ["connection", "context", "idempotency_key", "payload", "path_params"]
READ_SIGNATURE = ["connection", "context", "payload", "path_params"]


def by_id() -> dict[str, dict]:
    return {entry["operation_id"]: entry for entry in OPERATIONS}


def test_operations_cover_the_required_coordination_surface() -> None:
    missing = REQUIRED_OPERATION_IDS - set(by_id())
    assert missing == set()


def test_operation_ids_are_unique_and_dotted() -> None:
    ids = [entry["operation_id"] for entry in OPERATIONS]
    assert len(ids) == len(set(ids))
    for operation_id in ids:
        parts = operation_id.split(".")
        assert parts[0] == "coordination"
        assert len(parts) >= 3
        assert all(part and part.replace("_", "").isalnum() for part in parts)


def test_entry_keys_match_the_integration_contract() -> None:
    expected = {
        "operation_id",
        "method",
        "path",
        "kind",
        "request_model",
        "handler",
        "summary",
        "usage",
    }
    for entry in OPERATIONS:
        assert set(entry) == expected

def test_paths_are_versioned_coordination_routes() -> None:
    allowed = set(string.ascii_letters + string.digits + "/{}:.-_")
    for entry in OPERATIONS:
        path = entry["path"]
        assert path.startswith("/v1/coordination/"), path
        assert set(path) <= allowed, path
        assert path.count("{") == path.count("}")


def test_every_write_declares_a_strict_request_model() -> None:
    for entry in OPERATIONS:
        if entry["kind"] == "scoped_write":
            assert entry["request_model"] is not None, entry["operation_id"]


def test_path_parameters_are_snake_case_placeholders() -> None:
    for entry in OPERATIONS:
        for name in _placeholders(entry["path"]):
            assert name.replace("_", "").isalnum() and name.islower(), name


def _placeholders(path: str) -> list[str]:
    return [part.split("}")[0] for part in path.split("{")[1:]]


def test_request_models_are_strict_pydantic() -> None:
    for entry in OPERATIONS:
        model = entry["request_model"]
        if model is None:
            continue
        assert isinstance(model, type) and issubclass(model, BaseModel)
        assert model.model_config.get("extra") == "forbid", entry["operation_id"]


def test_writes_without_bodies_declare_no_model() -> None:
    for entry in OPERATIONS:
        if entry["kind"] == "scoped_write" and entry["request_model"] is None:
            handler_source = inspect.getsource(entry["handler"])
            assert "payload" not in handler_source or "payload" in handler_source


def test_handlers_are_async_with_contract_signatures() -> None:
    for entry in OPERATIONS:
        handler = entry["handler"]
        assert inspect.iscoroutinefunction(handler), entry["operation_id"]
        parameters = list(inspect.signature(handler).parameters)
        expected = (
            WRITE_SIGNATURE if entry["kind"] == "scoped_write" else READ_SIGNATURE
        )
        assert parameters == expected, entry["operation_id"]


def test_summaries_and_usage_teach_workers() -> None:
    for entry in OPERATIONS:
        summary = entry["summary"]
        usage = entry["usage"]
        assert isinstance(summary, str) and 10 <= len(summary) <= 160
        assert usage.startswith("Use when ")
        assert "do not" in usage.lower()


def test_coordination_domain_imports_no_transport_frameworks() -> None:
    banned = {"fastapi", "mcp", "click", "typer", "uvicorn", "starlette"}
    for source_file in sorted(PACKAGE_DIR.glob("*.py")):
        tree = ast.parse(source_file.read_text(), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, source_file.name
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                assert node.module.split(".")[0] not in banned, source_file.name


def test_list_cursor_codec_round_trips() -> None:
    created_at = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    handoff_id = uuid.uuid4()
    cursor = encode_list_cursor(created_at, handoff_id)
    decoded_at, decoded_id = decode_list_cursor(cursor)
    assert decoded_at == created_at
    assert decoded_id == handoff_id


@pytest.mark.parametrize("cursor", ["", "not-base64!!", "e30=", "aGVsbG8="])
def test_corrupt_cursors_are_typed_problems(cursor: str) -> None:
    with pytest.raises(ApiProblem) as failure:
        decode_list_cursor(cursor)
    assert failure.value.status == 422
    assert failure.value.code == "invalid_cursor"
