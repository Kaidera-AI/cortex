from __future__ import annotations

from cortex_v2 import worker
from cortex_v2.processing import runtime
from cortex_v2.processing.runtime import executor_capabilities


def test_worker_entrypoint_delegates_to_processing_runtime() -> None:
    assert worker.main is runtime.main


def test_graph_role_can_claim_graph_required_jobs() -> None:
    # Retrieval enqueues graph.* jobs with required_role='graph'; the queue
    # claim filter matches required_role against the worker's executor roles.
    assert {"core", "graph"} <= set(executor_capabilities("graph"))


def test_doc_and_embed_capabilities_are_unchanged() -> None:
    assert {"core", "doc"} <= set(executor_capabilities("doc"))
    assert "core" in executor_capabilities("embed")
    assert "graph" not in executor_capabilities("embed")
    assert "doc" not in executor_capabilities("embed")


def test_unknown_role_has_no_executor_capabilities() -> None:
    assert executor_capabilities("no-such-role") == ()
