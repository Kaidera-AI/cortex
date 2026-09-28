"""Code-graph unit tests (R17/F09): stdlib-ast Python extraction, deterministic
symbol keys, bounded snapshot limits, generation freshness semantics and the
bounded impact traversal. Publication and traversal SQL run against route
fakes; real-DB behavior is covered by the integration suite.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from test_retrieval_fakes import PROJECT_SCOPE_ID, FakeConnection

from cortex_v2.retrieval.code_graph import (
    EXTRACTOR_PROFILE,
    MAX_FILE_BYTES,
    MAX_FILES,
    SnapshotTooLarge,
    assess_impact,
    extract_python_module,
    extract_snapshot,
    resolve_generation,
)


def run(coroutine):
    return asyncio.run(coroutine)


SAMPLE = '''\
"""Module docstring."""

import os
import cortex.v2.store as store
from cortex.v2.models import StrictInput

TOP_LEVEL = 1


class Planner:
    """A planner."""

    def plan(self, query):
        helper(query)
        self.validate(query)
        os.getcwd()
        store.authenticate(query)
        StrictInput()
        unknown_call(query)

    def validate(self, query):
        return True


def helper(query):
    return query


def entrypoint():
    planner = Planner()
    planner.plan("q")
    helper("q")
'''


def test_extract_module_symbols_have_qualified_deterministic_keys():
    extraction = extract_python_module("src/planner.py", SAMPLE)
    assert extraction.parse_error is None
    keys = {symbol.symbol_key: symbol for symbol in extraction.symbols}
    assert set(keys) == {
        "src/planner.py",
        "src/planner.py::Planner",
        "src/planner.py::Planner.plan",
        "src/planner.py::Planner.validate",
        "src/planner.py::helper",
        "src/planner.py::entrypoint",
    }
    assert keys["src/planner.py"].symbol_kind == "module"
    assert keys["src/planner.py::Planner"].symbol_kind == "class"
    assert keys["src/planner.py::Planner.plan"].symbol_kind == "method"
    assert keys["src/planner.py::helper"].symbol_kind == "function"
    plan = keys["src/planner.py::Planner.plan"]
    assert plan.span_start_line == 13
    assert plan.span_end_line >= 19


def test_extract_resolves_intra_module_calls_and_keeps_external_calls_unresolved():
    extraction = extract_python_module("src/planner.py", SAMPLE)
    calls = {
        (edge.caller_key, edge.callee_key): edge
        for edge in extraction.edges
        if edge.edge_kind == "calls"
    }
    assert ("src/planner.py::Planner.plan", "src/planner.py::helper") in calls
    assert calls[("src/planner.py::Planner.plan", "src/planner.py::helper")].resolved
    assert ("src/planner.py::Planner.plan", "src/planner.py::Planner.validate") in calls
    assert ("src/planner.py::entrypoint", "src/planner.py::Planner.plan") in calls
    # External calls keep their callee key but stay explicitly unresolved.
    external = calls[("src/planner.py::Planner.plan", "os.getcwd")]
    assert not external.resolved
    assert ("src/planner.py::Planner.plan", "cortex.v2.store.authenticate") in calls
    assert ("src/planner.py::Planner.plan", "cortex.v2.models.StrictInput") in calls
    assert ("src/planner.py::Planner.plan", "unknown_call") in calls


def test_extract_records_imports_and_contains_edges_with_source_lines():
    extraction = extract_python_module("src/planner.py", SAMPLE)
    imports = {
        (edge.caller_key, edge.callee_key)
        for edge in extraction.edges
        if edge.edge_kind == "imports"
    }
    assert ("src/planner.py", "os") in imports
    assert ("src/planner.py", "cortex.v2.store") in imports
    assert ("src/planner.py", "cortex.v2.models") in imports
    contains = {
        (edge.caller_key, edge.callee_key)
        for edge in extraction.edges
        if edge.edge_kind == "contains"
    }
    assert ("src/planner.py", "src/planner.py::Planner") in contains
    assert ("src/planner.py::Planner", "src/planner.py::Planner.plan") in contains
    assert all(edge.source_line > 0 for edge in extraction.edges)


def test_extract_is_deterministic():
    first = extract_python_module("src/planner.py", SAMPLE)
    second = extract_python_module("src/planner.py", SAMPLE)
    assert first.symbols == second.symbols
    assert first.edges == second.edges


def test_syntax_error_is_reported_not_fatal():
    extraction = extract_python_module("src/broken.py", "def f(:\n")
    assert extraction.symbols == ()
    assert extraction.edges == ()
    assert extraction.parse_error is not None


def test_snapshot_excludes_non_python_and_oversize_files_with_reasons():
    files = [
        ("src/ok.py", "def a():\n    return 1\n"),
        ("README.md", "# docs\n"),
        ("src/huge.py", "x = '" + "y" * (MAX_FILE_BYTES + 1) + "'\n"),
    ]
    snapshot = extract_snapshot(files)
    assert {item["path"] for item in snapshot.excluded} == {"README.md", "src/huge.py"}
    reasons = {item["path"]: item["reason"] for item in snapshot.excluded}
    assert reasons["README.md"] == "unsupported_language"
    assert reasons["src/huge.py"] == "file_too_large"
    assert reasons and snapshot.language_coverage == ["python"]
    assert any(symbol.symbol_key == "src/ok.py" for symbol in snapshot.symbols)


def test_snapshot_rejects_too_many_files():
    files = [(f"src/m{i}.py", "def f():\n    pass\n") for i in range(MAX_FILES + 1)]
    with pytest.raises(SnapshotTooLarge):
        extract_snapshot(files)


def test_snapshot_records_unparseable_files_in_coverage():
    snapshot = extract_snapshot([("src/broken.py", "def f(:\n")])
    assert snapshot.excluded == [{"path": "src/broken.py", "reason": "syntax_error"}]
    assert snapshot.symbols == ()


# ---------------------------------------------------------------------------
# Generation freshness (R17: stale reported, never silently served as current)
# ---------------------------------------------------------------------------

REPO_ID = uuid.UUID("eeeeeeee-0000-4000-8000-000000000001")


def generation_row(generation, commit_sha, status="published"):
    return {
        "scope_id": PROJECT_SCOPE_ID,
        "repository_id": REPO_ID,
        "generation": generation,
        "commit_sha": commit_sha,
        "extractor_profile": EXTRACTOR_PROFILE,
        "language_coverage": ["python"],
        "status": status,
        "file_count": 3,
        "excluded_files": [],
        "published_at": None,
    }


def generations_connection(rows):
    conn = FakeConnection()

    def respond(*args):
        if len(args) == 3:
            return [row for row in rows if row["commit_sha"] == args[2]]
        return [row for row in rows if row["status"] == "published"]

    conn.add("code_generations AS generation", respond)
    return conn


def test_head_matching_latest_generation_is_fresh():
    conn = generations_connection([generation_row(2, "ab12cd34")])
    resolved = run(
        resolve_generation(
            conn,
            scope_id=PROJECT_SCOPE_ID,
            repository_id=REPO_ID,
            head_commit="ab12cd34",
        )
    )
    assert resolved.graph_status == "fresh"
    assert resolved.generation["generation"] == 2


def test_head_newer_than_index_is_stale_but_still_served_explicitly():
    conn = generations_connection([generation_row(2, "ab12cd34")])
    resolved = run(
        resolve_generation(
            conn,
            scope_id=PROJECT_SCOPE_ID,
            repository_id=REPO_ID,
            head_commit="ff00ff00",
        )
    )
    assert resolved.graph_status == "stale"
    assert resolved.generation["commit_sha"] == "ab12cd34"


def test_head_matching_older_generation_selects_that_generation():
    conn = generations_connection([generation_row(1, "old0commit1")])
    resolved = run(
        resolve_generation(
            conn,
            scope_id=PROJECT_SCOPE_ID,
            repository_id=REPO_ID,
            head_commit="old0commit1",
        )
    )
    assert resolved.graph_status == "fresh"
    assert resolved.generation["generation"] == 1


def test_missing_published_generation_is_unavailable_never_empty_success():
    conn = generations_connection([])
    resolved = run(
        resolve_generation(
            conn,
            scope_id=PROJECT_SCOPE_ID,
            repository_id=REPO_ID,
            head_commit=None,
        )
    )
    assert resolved.graph_status == "unavailable"
    assert resolved.generation is None


# ---------------------------------------------------------------------------
# Bounded impact traversal
# ---------------------------------------------------------------------------


def impact_connection(hop_rows: dict[frozenset, list[dict]]):
    conn = FakeConnection()

    def respond(scope_id, repository_id, generation, frontier):
        return list(hop_rows.get(frozenset(frontier), []))

    conn.add("code_edges AS edge", respond)
    return conn


def caller_row(symbol_id, symbol_key, caller_of_line=10):
    return {
        "caller_symbol_id": symbol_id,
        "symbol_key": symbol_key,
        "symbol_kind": "function",
        "file_path": "src/callers.py",
        "edge_kind": "calls",
        "source_line": caller_of_line,
        "callee_symbol_id": None,
    }


def test_impact_traverses_reverse_edges_with_hop_counts_and_limit():
    changed = uuid.UUID("11111111-0000-4000-8000-0000000000a1")
    hop1a = uuid.UUID("11111111-0000-4000-8000-0000000000b1")
    hop1b = uuid.UUID("11111111-0000-4000-8000-0000000000b2")
    hop2 = uuid.UUID("11111111-0000-4000-8000-0000000000c1")
    hop_rows = {
        frozenset({changed}): [
            caller_row(hop1a, "src/callers.py::first"),
            caller_row(hop1b, "src/callers.py::second"),
        ],
        frozenset({hop1a, hop1b}): [caller_row(hop2, "src/callers.py::third")],
        frozenset({hop2}): [],
    }
    conn = impact_connection(hop_rows)
    result = run(
        assess_impact(
            conn,
            scope_id=PROJECT_SCOPE_ID,
            repository_id=REPO_ID,
            generation_row=generation_row(1, "ab12cd34"),
            graph_status="fresh",
            seed_symbol_ids=[changed],
            seed_symbol_keys=["src/planner.py::helper"],
            unresolved_files=[],
            unresolved_symbols=[],
            max_hops=3,
            limit=100,
            head_commit=None,
        )
    )
    direct = {node["symbol_key"]: node for node in result["direct_impacts"]}
    assert set(direct) == {"src/callers.py::first", "src/callers.py::second"}
    assert direct["src/callers.py::first"]["hop"] == 1
    transitive = {node["symbol_key"]: node for node in result["transitive_impacts"]}
    assert transitive["src/callers.py::third"]["hop"] == 2
    assert result["truncated"] is False
    assert "lower bound" in result["interpretation"]


def test_impact_truncates_at_limit_and_says_so():
    changed = uuid.UUID("11111111-0000-4000-8000-0000000000a1")
    callers = [
        caller_row(uuid.uuid4(), f"src/callers.py::c{i}") for i in range(5)
    ]
    conn = impact_connection({frozenset({changed}): callers})
    result = run(
        assess_impact(
            conn,
            scope_id=PROJECT_SCOPE_ID,
            repository_id=REPO_ID,
            generation_row=generation_row(1, "ab12cd34"),
            graph_status="fresh",
            seed_symbol_ids=[changed],
            seed_symbol_keys=[],
            unresolved_files=[],
            unresolved_symbols=[],
            max_hops=2,
            limit=3,
            head_commit=None,
        )
    )
    assert len(result["direct_impacts"]) + len(result["transitive_impacts"]) == 3
    assert result["truncated"] is True


def test_impact_unavailable_graph_returns_typed_unavailable_not_zero_hits():
    result = run(
        assess_impact(
            FakeConnection(),
            scope_id=PROJECT_SCOPE_ID,
            repository_id=REPO_ID,
            generation_row=None,
            graph_status="unavailable",
            seed_symbol_ids=[],
            seed_symbol_keys=[],
            unresolved_files=[],
            unresolved_symbols=["src/planner.py::helper"],
            max_hops=2,
            limit=10,
            head_commit=None,
        )
    )
    assert result["graph"]["status"] == "unavailable"
    assert result["direct_impacts"] == []
    assert "code_graph_not_built" in result["degraded"]
    assert result["unresolved_symbols"] == ["src/planner.py::helper"]
