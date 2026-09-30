"""OPERATIONS registry contract tests.

The integrator mounts ``cortex_v2.retrieval.operations.OPERATIONS`` generically;
this suite pins the documented integration contract (keys, kinds, handler
signatures, /v1 paths, pydantic request models) so a generic mount cannot
silently mis-wire an operation.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from pydantic import BaseModel, ValidationError

from test_retrieval_fakes import FakeConnection, make_context

from cortex_v2.retrieval.models import RebuildProjectionsRequest
from cortex_v2.retrieval.operations import OPERATIONS, memory_rebuild_projections

REQUIRED_IDS = {
    "memory.search",
    "memory.inspect-hit",
    "graph.explore",
    "code.callers",
    "code.impact",
    "code.blast-radius",
    "code.hotspots",
    "code.assess-change",
}

REQUIRED_KEYS = {
    "operation_id",
    "method",
    "path",
    "kind",
    "request_model",
    "handler",
    "summary",
    "usage",
}

WRITE_SIGNATURE = ["connection", "context", "idempotency_key", "payload", "path_params"]
READ_SIGNATURE = ["connection", "context", "payload", "path_params"]


def operations_by_id():
    return {operation["operation_id"]: operation for operation in OPERATIONS}


def test_every_required_operation_is_exposed():
    assert REQUIRED_IDS <= set(operations_by_id())


def test_operation_ids_are_unique_and_dotted():
    ids = [operation["operation_id"] for operation in OPERATIONS]
    assert len(ids) == len(set(ids))
    assert all("." in operation_id for operation_id in ids)


def test_entries_carry_the_full_integration_contract():
    for operation in OPERATIONS:
        missing = REQUIRED_KEYS - set(operation)
        assert not missing, f"{operation.get('operation_id')} missing {missing}"
        assert operation["method"] in {"GET", "POST"}
        assert operation["path"].startswith("/v1/")
        assert operation["kind"] in {"scoped_write", "scoped_read"}
        assert operation["summary"].strip()
        usage = operation["usage"]
        assert usage["when_to_use"].strip()
        assert usage["when_not_to_use"].strip()
        model = operation["request_model"]
        assert model is None or (
            isinstance(model, type) and issubclass(model, BaseModel)
        )


def test_read_operations_never_require_idempotency_and_writes_always_do():
    by_id = operations_by_id()
    read_ids = REQUIRED_IDS - {"code.assess-change"}
    for operation_id in read_ids:
        assert by_id[operation_id]["kind"] == "scoped_read"
    assert by_id["code.assess-change"]["kind"] == "scoped_write"


def test_handler_signatures_match_the_operation_kind():
    for operation in OPERATIONS:
        handler = operation["handler"]
        assert callable(handler)
        assert inspect.iscoroutinefunction(handler), operation["operation_id"]
        parameters = list(inspect.signature(handler).parameters)
        expected = (
            WRITE_SIGNATURE
            if operation["kind"] == "scoped_write"
            else READ_SIGNATURE
        )
        assert parameters == expected, operation["operation_id"]


def test_graph_and_index_maintenance_operations_are_writes():
    by_id = operations_by_id()
    assert by_id["graph.extract"]["kind"] == "scoped_write"
    assert by_id["graph.retract-source"]["kind"] == "scoped_write"
    assert by_id["code.publish-index"]["kind"] == "scoped_write"
    assert by_id["memory.rebuild-projections"]["kind"] == "scoped_write"
    assert by_id["code.annotate"]["kind"] == "scoped_write"


def test_graph_extract_revision_accepts_int32_max_through_queue():
    import uuid
    from contextlib import asynccontextmanager

    from cortex_v2.retrieval.operations import graph_extract

    @asynccontextmanager
    async def transaction():
        yield

    connection = FakeConnection()
    connection.transaction = transaction
    connection.add("pg_advisory_xact_lock", [])
    connection.add("FROM cortex_core.command_receipts", [])
    connection.add("SELECT count(*)", [0])
    connection.add(
        "INSERT INTO cortex_processing.jobs",
        lambda job_id, *_: [{"job_id": job_id, "status": "queued"}],
    )
    connection.add("INSERT INTO cortex_processing.budget_reservations", [])
    connection.add("INSERT INTO cortex_processing.outbox_events", [])
    connection.add("INSERT INTO cortex_core.command_receipts", [])
    request = operations_by_id()["graph.extract"]["request_model"].model_validate(
        {"content_id": str(uuid.uuid4()), "revision": 2_147_483_647}
    )

    status, receipt, replayed = asyncio.run(
        graph_extract(connection, make_context(), "max-revision", request, {})
    )

    assert (status, replayed) == (202, False)
    assert receipt["state"] == "pending_processing"
    assert receipt["revision"] == 2_147_483_647
    assert receipt["job_status"] == "queued"
    assert len(receipt["dedupe_key"]) <= 128


def test_graph_extract_revision_above_int32_returns_typed_http_422(monkeypatch):
    import uuid
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from cortex_v2.config import KAI_TEST_INSTANCE

    monkeypatch.setenv("CORTEX_V2_SANDBOX_INSTANCE", KAI_TEST_INSTANCE)
    from cortex_v2.app import create_app

    app = create_app()
    app.state.settings = SimpleNamespace(token_pepper=b"p" * 32)
    response = TestClient(app).post(
        "/v1/graphs/memory:extract",
        json={"content_id": str(uuid.uuid4()), "revision": 2_147_483_648},
        headers={"Authorization": "Bearer " + "t" * 43},
    )

    assert response.status_code == 422, response.json()
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["fields"] == [
        {"path": "revision", "type": "less_than_equal"}
    ]


def test_domain_layer_imports_no_transport_packages():
    import cortex_v2.retrieval.code_graph as code_graph
    import cortex_v2.retrieval.jobs as jobs
    import cortex_v2.retrieval.memory_graph as memory_graph
    import cortex_v2.retrieval.operations as operations
    import cortex_v2.retrieval.planner as planner
    import cortex_v2.retrieval.ports as ports

    for module in (
        code_graph,
        jobs,
        memory_graph,
        operations,
        planner,
        ports,
    ):
        source = inspect.getsource(module)
        assert "import fastapi" not in source
        assert "import mcp" not in source
        assert "from fastapi" not in source


def test_scoped_write_operations_all_carry_a_request_model():
    for operation in OPERATIONS:
        if operation["kind"] == "scoped_write":
            assert operation["request_model"] is not None, operation["operation_id"]
            assert operation["method"] == "POST", operation["operation_id"]


def test_rebuild_projections_model_is_strict_and_empty():
    assert RebuildProjectionsRequest.model_validate({}) == (
        RebuildProjectionsRequest()
    )
    with pytest.raises(ValidationError):
        RebuildProjectionsRequest.model_validate({"anything": 1})


def rebuild_connection():
    conn = FakeConnection()
    conn.add("pg_advisory_xact_lock", [])
    conn.add("FROM cortex_core.command_receipts", [])
    conn.add("rebuild_trigram_projection", [{"rebuild_trigram_projection": 3}])
    conn.add("INSERT INTO cortex_core.command_receipts", [])
    return conn


def test_rebuild_projections_handler_accepts_typed_and_absent_payload():
    for payload in (RebuildProjectionsRequest(), None):
        status, receipt, replayed = asyncio.run(
            memory_rebuild_projections(
                rebuild_connection(), make_context(), "key-1", payload, {}
            )
        )
        assert status == 200
        assert replayed is False
        assert receipt["state"] == "committed"
        assert receipt["operation"] == "memory.rebuild-projections"
        assert receipt["trigram_document_count"] == 3
