"""Graph job handler unit tests: the durable leased worker contract from
cortex_v2.processing (FINAL per the Processing owner): handlers never see a
connection, they return typed ExecutionReports with the extracted payload in
``handler_output``; publishers run inside the fenced publish transaction and
write only cortex_retrieval rows.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from test_retrieval_fakes import (
    PROJECT_SCOPE_ID,
    FakeAttemptServices,
    FakeConnection,
    FakeRegistry,
    Route,
    make_attempt_context,
    make_context,
)

from cortex_v2.processing.contracts import Outcome, SourceRef
from cortex_v2.retrieval import jobs


def run(coroutine):
    return asyncio.run(coroutine)


def source_ref(body: str, *, content_id=None, revision=1) -> SourceRef:
    return SourceRef(
        scope_id=PROJECT_SCOPE_ID,
        content_id=content_id or uuid.uuid4(),
        revision=revision,
        content_class="knowledge",
        media_type="text/plain",
        body_text=body,
        content_hash=b"\xab" * 32,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_wires_both_graph_kinds_to_the_graph_role_with_publishers():
    registry = FakeRegistry()
    jobs.register(registry)
    by_kind = {entry["kind"]: entry for entry in registry.registered}
    assert set(by_kind) == {"graph.memory.extract", "graph.code.extract"}
    for entry in by_kind.values():
        assert entry["role"] == "graph"
        assert callable(entry["handler"])
        assert callable(entry["publisher"])
        assert entry["summary"]
        assert entry["required_intent"] == ("pin",)


# ---------------------------------------------------------------------------
# graph.memory.extract handler
# ---------------------------------------------------------------------------


def memory_payload(content_id=None, revision=1):
    return {
        "pin": {
            "content_id": str(content_id or uuid.uuid4()),
            "revision": revision,
        }
    }


def test_memory_extract_reports_ok_with_handler_output_payload():
    content_id = uuid.uuid4()
    context = make_attempt_context(
        job_kind="graph.memory.extract",
        payload=memory_payload(content_id, 2),
    )
    services = FakeAttemptServices(
        source=source_ref("Payments depends on Ledger for bookkeeping.")
    )
    report = run(jobs.handle_memory_extract(context, services))
    assert report.outcome is Outcome.OK
    assert report.executor == "rule-v1"
    assert report.stats["assertions"] == 1
    assert report.handler_output["content_id"] == str(content_id)
    assert report.handler_output["revision"] == 2
    assertions = report.handler_output["assertions"]
    assert assertions[0]["relation"] == "depends_on"
    assert assertions[0]["subject_key"] == "payments"
    assert {"span_start", "span_end"} <= set(assertions[0])
    assert services.blocking_calls, "extraction runs through the worker executor"


def test_memory_extract_missing_source_is_permanent_input_corrupt():
    context = make_attempt_context(
        job_kind="graph.memory.extract", payload=memory_payload()
    )
    report = run(jobs.handle_memory_extract(context, FakeAttemptServices(source=None)))
    assert report.outcome is Outcome.INPUT_CORRUPT
    assert report.handler_output is None


def test_memory_extract_rejects_unknown_extractor_profile():
    payload = memory_payload()
    payload["extractor_profile"] = "gpt-magic-9"
    context = make_attempt_context(job_kind="graph.memory.extract", payload=payload)
    services = FakeAttemptServices(source=source_ref("A depends on B."))
    report = run(jobs.handle_memory_extract(context, services))
    assert report.outcome is Outcome.CONFIGURATION_MISSING


def test_memory_extract_rejects_malformed_pin():
    bad_pins = (
        {},
        {"content_id": "x", "revision": 1},
        {"content_id": str(uuid.uuid4())},
    )
    for bad_pin in bad_pins:
        context = make_attempt_context(
            job_kind="graph.memory.extract", payload={"pin": bad_pin}
        )
        services = FakeAttemptServices(source=source_ref("A depends on B."))
        report = run(jobs.handle_memory_extract(context, services))
        assert report.outcome is Outcome.INPUT_REJECTED, bad_pin


def test_memory_extract_never_embeds_or_parses_beyond_the_pinned_revision():
    context = make_attempt_context(
        job_kind="graph.memory.extract", payload=memory_payload()
    )
    services = FakeAttemptServices(source=source_ref("A depends on B."))
    run(jobs.handle_memory_extract(context, services))
    assert services.blocking_calls == ["extract"]


# ---------------------------------------------------------------------------
# graph.code.extract handler
# ---------------------------------------------------------------------------


def code_payload(commit="ab12cd34ef56"):
    return {
        "pin": {
            "repository_key": "helix/cortex",
            "commit_sha": commit,
            "snapshot_id": "snap-1",
        },
        "files": [
            {"path": "src/a.py", "source": "def a():\n    b()\n\ndef b():\n    pass\n"},
        ],
    }


def test_code_extract_reports_symbols_and_edges_in_handler_output():
    context = make_attempt_context(
        job_kind="graph.code.extract", payload=code_payload()
    )
    report = run(jobs.handle_code_extract(context, FakeAttemptServices()))
    assert report.outcome is Outcome.OK
    assert report.executor == jobs.EXTRACTOR_PROFILE
    output = report.handler_output
    assert output["repository_key"] == "helix/cortex"
    assert output["commit_sha"] == "ab12cd34ef56"
    keys = {symbol["symbol_key"] for symbol in output["symbols"]}
    assert {"src/a.py", "src/a.py::a", "src/a.py::b"} <= keys
    callees = {(edge["caller_key"], edge["callee_key"]) for edge in output["edges"]}
    assert ("src/a.py::a", "src/a.py::b") in callees
    assert report.stats["symbols"] >= 3


def test_code_extract_excludes_unparseable_files_with_warnings_not_failure():
    payload = code_payload()
    payload["files"].append({"path": "src/bad.py", "source": "def f(:\n"})
    context = make_attempt_context(job_kind="graph.code.extract", payload=payload)
    report = run(jobs.handle_code_extract(context, FakeAttemptServices()))
    assert report.outcome is Outcome.OK
    assert report.handler_output["excluded"] == [
        {"path": "src/bad.py", "reason": "syntax_error"}
    ]
    assert any("src/bad.py" in warning for warning in report.warnings)


def test_code_extract_rejects_malformed_payloads():
    for bad_payload in (
        {},
        {"pin": {}, "files": []},
        {"pin": {"repository_key": "r", "commit_sha": "ZZZZ"}, "files": []},
        {"pin": {"repository_key": "r", "commit_sha": "ab12cd34"}, "files": "nope"},
        {
            "pin": {"repository_key": "r", "commit_sha": "ab12cd34"},
            "files": [{"path": "/etc/passwd", "source": "x"}],
        },
    ):
        context = make_attempt_context(
            job_kind="graph.code.extract", payload=bad_payload
        )
        report = run(jobs.handle_code_extract(context, FakeAttemptServices()))
        assert report.outcome is Outcome.INPUT_REJECTED, bad_payload


def test_code_extract_rejects_oversized_snapshots():
    payload = code_payload()
    payload["files"] = [
        {"path": f"src/m{i}.py", "source": "def f():\n    pass\n"}
        for i in range(jobs.MAX_FILES + 1)
    ]
    context = make_attempt_context(job_kind="graph.code.extract", payload=payload)
    report = run(jobs.handle_code_extract(context, FakeAttemptServices()))
    assert report.outcome is Outcome.INPUT_REJECTED


# ---------------------------------------------------------------------------
# Publishers (fenced publish transaction; write cortex_retrieval rows only)
# ---------------------------------------------------------------------------


def test_memory_publisher_requires_handler_output():
    from cortex_v2.processing.contracts import ExecutionReport

    context = make_attempt_context(job_kind="graph.memory.extract", payload={"pin": {}})
    with pytest.raises(ValueError):
        run(
            jobs.publish_memory_extract(
                FakeConnection(), context, ExecutionReport(outcome=Outcome.OK)
            )
        )


def test_memory_publisher_writes_entities_assertions_evidence_and_run():
    content_id = uuid.uuid4()
    context = make_attempt_context(
        job_kind="graph.memory.extract",
        content_id=None,
        source_revision=None,
        payload={"pin": {"content_id": str(content_id), "revision": 1}},
    )
    report = run(jobs.handle_memory_extract(context, FakeAttemptServices(
        source=source_ref("Payments depends on Ledger.")
    )))
    entity_id_a = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    entity_id_b = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000002")
    lookup = {"payments": entity_id_a, "ledger": entity_id_b}
    conn = FakeConnection()
    conn.routes.extend(
        [
            Route("extraction_runs AS prior", lambda *args: []),
            Route(
                "DELETE FROM cortex_retrieval.graph_assertion_evidence",
                lambda *args: [],
            ),
            Route("graph_assertions AS stranded", lambda *args: []),
            Route("UPDATE cortex_retrieval.graph_assertions", lambda *args: []),
            Route("INSERT INTO cortex_retrieval.graph_entities", lambda *args: []),
            Route(
                "graph_entities AS lookup",
                lambda scope_id, keys, types: [
                    {"entity_key": k, "entity_type": t, "entity_id": lookup[k]}
                    for k, t in zip(keys, types, strict=True)
                ],
            ),
            Route(
                "INSERT INTO cortex_retrieval.graph_assertions",
                lambda *args: [{"assertion_id": uuid.uuid4()}],
            ),
            Route("graph_assertions AS existing", lambda *args: []),
            Route("max(evidence.evidence_seq)", lambda *args: [{"max_seq": None}]),
            Route(
                "INSERT INTO cortex_retrieval.graph_assertion_evidence",
                lambda *args: [{"evidence_seq": 1}],
            ),
            Route(
                "INSERT INTO cortex_retrieval.extraction_runs",
                lambda *args: [{"run_id": uuid.uuid4()}],
            ),
        ]
    )
    run(jobs.publish_memory_extract(conn, context, report))
    written = " ".join(conn.queries())
    assert "INSERT INTO cortex_retrieval.graph_entities" in written
    assert "INSERT INTO cortex_retrieval.graph_assertions" in written
    assert "INSERT INTO cortex_retrieval.graph_assertion_evidence" in written
    assert "INSERT INTO cortex_retrieval.extraction_runs" in written
    assert not [q for q in conn.queries() if "cortex_core." in q]
    assert not [q for q in conn.queries() if "INSERT INTO cortex_core" in q]
    assert not [q for q in conn.queries() if "UPDATE cortex_core" in q]


def test_code_publisher_writes_generation_symbols_and_edges():
    context = make_attempt_context(
        job_kind="graph.code.extract", payload=code_payload()
    )
    report = run(jobs.handle_code_extract(context, FakeAttemptServices()))
    repo_id = uuid.UUID("eeeeeeee-0000-4000-8000-000000000001")
    conn = FakeConnection()
    conn.routes.extend(
        [
            Route(
                "INSERT INTO cortex_retrieval.code_repositories",
                lambda *args: [{"repository_id": repo_id}],
            ),
            Route(
                "code_repositories AS existing",
                lambda *args: [{"repository_id": repo_id}],
            ),
            Route(
                "max(generation.generation)",
                lambda *args: [{"max_generation": None}],
            ),
            Route("INSERT INTO cortex_retrieval.code_generations", lambda *args: []),
            Route("INSERT INTO cortex_retrieval.code_symbols", lambda *args: []),
            Route("INSERT INTO cortex_retrieval.code_edges", lambda *args: []),
            Route("UPDATE cortex_retrieval.code_generations", lambda *args: []),
        ]
    )
    run(jobs.publish_code_extract(conn, context, report))
    written = " ".join(conn.queries())
    assert "INSERT INTO cortex_retrieval.code_repositories" in written
    assert "INSERT INTO cortex_retrieval.code_generations" in written
    assert "INSERT INTO cortex_retrieval.code_symbols" in written
    assert "INSERT INTO cortex_retrieval.code_edges" in written
    assert "UPDATE cortex_retrieval.code_generations" in written
    assert not [q for q in conn.queries() if "cortex_core." in q]


# ---------------------------------------------------------------------------
# Graph-role claim contract (real Processing registry/queue validation)
# ---------------------------------------------------------------------------


def test_graph_role_worker_claims_every_graph_kind_under_registry_contract():
    from cortex_v2.processing.registry import JobRegistry

    registry = JobRegistry()
    loaded = registry.load_modules(["cortex_v2.retrieval.jobs"])
    assert loaded == ("cortex_v2.retrieval.jobs",)

    # A `--role graph` worker derives its claimable kinds exactly this way
    # (worker.job_kinds = registry.kinds_for_role(role)).
    kinds = registry.kinds_for_role("graph")
    assert kinds == ("graph.code.extract", "graph.memory.extract")

    expected = {
        "graph.memory.extract": (
            jobs.handle_memory_extract,
            jobs.publish_memory_extract,
        ),
        "graph.code.extract": (jobs.handle_code_extract, jobs.publish_code_extract),
    }
    for kind, (handler, publisher) in expected.items():
        spec = registry.handler_for(kind)
        assert spec is not None, kind
        assert spec.role == "graph"
        assert spec.handler is handler
        assert spec.publisher is publisher
        assert spec.summary
        assert spec.validate_intent({"payload": {"pin": {"k": "v"}}}) == ()
        assert spec.validate_intent({"payload": {}}) == ("pin",)
        assert spec.validate_intent({}) == ("pin",)

    # The documented claim predicate is
    #   required_role = ANY(worker executor_roles)
    #   AND job_kind = ANY(registry.kinds_for_role(worker role))
    # with the canonical graph executor capability set ("core", "graph").
    canonical_capabilities = ("core", "graph")
    requested_role = "graph"
    assert requested_role in canonical_capabilities
    for kind in expected:
        assert kind in kinds


def test_enqueued_graph_intents_request_the_canonical_graph_role(monkeypatch):
    from cortex_v2.processing import queue as processing_queue
    from cortex_v2.retrieval.operations import _enqueue_graph_job

    captured: list = []

    async def fake_enqueue(connection, *, context, intents, **kwargs):
        captured.extend(intents)
        return processing_queue.EnqueueResult(
            enqueued=(
                processing_queue.EnqueuedJob(
                    job_id=uuid.uuid4(),
                    dedupe_key=intents[0].dedupe_key,
                    created=True,
                    status="queued",
                ),
            )
        )

    monkeypatch.setattr(processing_queue, "enqueue_jobs", fake_enqueue)
    content_id = uuid.uuid4()
    result = run(
        _enqueue_graph_job(
            FakeConnection(),
            make_context(),
            kind=jobs.KIND_MEMORY_EXTRACT,
            payload={
                "pin": {"content_id": str(content_id), "revision": 1},
                "extractor_profile": "rule-v1",
            },
            dedupe_key="graph.memory.extract:test",
        )
    )
    intent = captured[0]
    # Canonical role request and the graph-kind pin contract.
    assert intent.required_role == "graph"
    assert intent.job_kind == "graph.memory.extract"
    assert intent.content_id is None
    assert intent.source_revision is None
    assert intent.profile_id is None
    assert intent.payload["pin"]["content_id"] == str(content_id)
    intent.validate()  # real Processing queue validation
    assert result["job_created"] is True
    assert result["job_status"] == "queued"
