"""Pure unit tests for the feed and verification OPERATIONS contracts.

Same integration contract as coordination: the integrator mounts these
lists generically; domain code never imports a transport framework.
"""

from __future__ import annotations

import ast
import inspect
import string
from pathlib import Path

import pytest
from pydantic import BaseModel

from cortex_v2.feed import operations as feed_operations_module
from cortex_v2.feed.operations import OPERATIONS as FEED_OPERATIONS
from cortex_v2.verification import operations as verification_operations_module
from cortex_v2.verification.operations import OPERATIONS as VERIFICATION_OPERATIONS

WRITE_SIGNATURE = ["connection", "context", "idempotency_key", "payload", "path_params"]
READ_SIGNATURE = ["connection", "context", "payload", "path_params"]

FEED_IDS = {
    "feed.poll",
    "feed.dashboard",
    "feed.dispatch",
    "feed.prune",
    "feed.generation.advance",
}
VERIFICATION_IDS = {
    "verification.record",
    "verification.record.get",
    "verification.subject.list",
}


def check_contract(
    operations: list[dict], required_ids: set[str], prefix: str
) -> None:
    ids = {entry["operation_id"] for entry in operations}
    assert required_ids <= ids
    assert len(ids) == len(operations)
    allowed_path_chars = set(string.ascii_letters + string.digits + "/{}:.-_")
    for entry in operations:
        assert set(entry) == {
            "operation_id",
            "method",
            "path",
            "kind",
            "request_model",
            "handler",
            "summary",
            "usage",
        }
        operation_id = entry["operation_id"]
        assert operation_id.startswith(prefix), operation_id
        if entry["kind"] == "scoped_write":
            assert entry["method"] == "POST"
            assert isinstance(entry["request_model"], type)
            assert issubclass(entry["request_model"], BaseModel)
            assert entry["request_model"].model_config.get("extra") == "forbid"
            expected = WRITE_SIGNATURE
        elif entry["kind"] == "scoped_read":
            assert entry["method"] == "GET"
            assert entry["request_model"] is None
            expected = READ_SIGNATURE
        else:
            pytest.fail(f"unknown kind {entry['kind']!r}")
        assert entry["path"].startswith("/v1/")
        assert set(entry["path"]) <= allowed_path_chars
        handler = entry["handler"]
        assert inspect.iscoroutinefunction(handler), operation_id
        assert list(inspect.signature(handler).parameters) == expected
        assert 10 <= len(entry["summary"]) <= 160
        assert entry["usage"].startswith("Use when ")
        assert "do not" in entry["usage"].lower()


def test_feed_operations_contract() -> None:
    check_contract(FEED_OPERATIONS, FEED_IDS, "feed.")


def test_verification_operations_contract() -> None:
    check_contract(VERIFICATION_OPERATIONS, VERIFICATION_IDS, "verification.")


def test_feed_operations_paths() -> None:
    paths = {
        entry["operation_id"]: entry["path"] for entry in FEED_OPERATIONS
    }
    assert paths["feed.poll"] == "/v1/feed/entries"
    assert paths["feed.dashboard"] == "/v1/feed/dashboard"
    assert paths["feed.dispatch"] == "/v1/feed:dispatch"
    assert paths["feed.prune"] == "/v1/feed:prune"
    assert paths["feed.generation.advance"] == "/v1/feed:generation-advance"


def test_verification_operations_paths() -> None:
    paths = {
        entry["operation_id"]: entry["path"]
        for entry in VERIFICATION_OPERATIONS
    }
    assert paths["verification.record"] == "/v1/verification/records"
    assert (
        paths["verification.record.get"]
        == "/v1/verification/records/{verification_id}"
    )
    assert paths["verification.subject.list"] == "/v1/verification/subjects"


@pytest.mark.parametrize(
    "module_root",
    [
        Path(feed_operations_module.__file__).resolve().parent,
        Path(verification_operations_module.__file__).resolve().parent,
    ],
)
def test_domain_modules_import_no_transport_frameworks(
    module_root: Path,
) -> None:
    banned = {"fastapi", "mcp", "click", "typer", "uvicorn", "starlette"}
    for source_file in sorted(module_root.glob("*.py")):
        tree = ast.parse(source_file.read_text(), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, source_file
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                assert node.module.split(".")[0] not in banned, source_file
