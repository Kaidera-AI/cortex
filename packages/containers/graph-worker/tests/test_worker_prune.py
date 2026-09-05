import asyncio
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


WORKER_PATH = Path(__file__).resolve().parents[1] / "worker.py"


def load_worker(name: str):
    spec = importlib.util.spec_from_file_location(name, WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def host_path(module, repo: Path) -> str:
    relative = repo.relative_to(module.PROJECTS_DIR)
    return str(Path(module.HOST_PROJECTS_ROOT) / relative)


def build_body(module, repo: Path, *, registered_repo: Path | None = None, **kwargs):
    return module.BuildBody(
        repo=host_path(module, repo),
        registered_repo=host_path(module, registered_repo or repo),
        storage_key=kwargs.pop("storage_key", "fixture-project"),
        **kwargs,
    )


def test_resolve_repo_translates_only_the_exact_registered_host_path(tmp_path):
    module = load_worker("graph_worker_resolve_host_path_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "Library" / "CloudStorage" / "Drive" / "Marketing"
    repo.mkdir(parents=True)
    requested = "/Users/alice/Library/CloudStorage/Drive/Marketing"

    path = module._resolve_repo(requested, requested)

    assert path == repo


def test_resolve_repo_maps_only_exact_appliance_registry_descendants(tmp_path):
    module = load_worker("graph_worker_appliance_registry_prefix_test")
    module.PROJECTS_DIR = tmp_path / "state" / "projects"
    module.PROJECTS_DIR.mkdir(parents=True)
    module.HOST_PROJECTS_ROOT = ""
    module.REGISTERED_PROJECTS_ROOT = "/projects"
    repo = module.PROJECTS_DIR / "fixture"
    repo.mkdir()

    assert module._resolve_repo("/projects/fixture", "/projects/fixture") == repo

    for rejected in (
        "/outside/fixture",
        "/projects/../outside/fixture",
        "/projects/fixture/..",
    ):
        with pytest.raises(module.HTTPException) as exc_info:
            module._resolve_repo(rejected, "/projects/fixture")
        assert exc_info.value.status_code == 403

    target = module.PROJECTS_DIR / "real"
    target.mkdir()
    alias = module.PROJECTS_DIR / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(module.HTTPException) as exc_info:
        module._resolve_repo("/projects/alias", "/projects/alias")
    assert exc_info.value.status_code == 400


def test_resolve_repo_rejects_unmapped_host_path_without_basename_fallback(tmp_path):
    module = load_worker("graph_worker_no_basename_fallback_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "Drive" / "Marketing"
    repo.mkdir(parents=True)

    with pytest.raises(module.HTTPException) as exc_info:
        module._resolve_repo(
            "/different/root/Marketing",
            "/Users/alice/Drive/Marketing",
        )

    assert exc_info.value.status_code == 403
    assert repo.is_dir()


def test_resolve_repo_preserves_only_the_outer_projects_mount_alias(tmp_path):
    module = load_worker("graph_worker_outer_mount_alias_test")
    real_root = tmp_path / "real-projects"
    repo = real_root / "Drive" / "Marketing"
    repo.mkdir(parents=True)
    mount_alias = tmp_path / "projects"
    mount_alias.symlink_to(real_root, target_is_directory=True)
    module.PROJECTS_DIR = mount_alias
    module.HOST_PROJECTS_ROOT = "/Users/alice"

    path = module._resolve_repo(
        "/Users/alice/Drive/Marketing",
        "/Users/alice/Drive/Marketing",
    )

    assert path == repo.resolve(strict=True)


def test_resolve_repo_rejects_a_sibling_worktree_of_the_registered_root(tmp_path):
    module = load_worker("graph_worker_sibling_worktree_rejected_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    registered = module.PROJECTS_DIR / "registered"
    registered_git = registered / ".git"
    worktree = module.PROJECTS_DIR / "worktrees" / "review"
    worktree_git = registered_git / "worktrees" / "review"
    worktree_git.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(
        f"gitdir: {worktree_git}\n", encoding="utf-8"
    )

    with pytest.raises(module.HTTPException) as exc_info:
        module._resolve_repo(
            "/Users/alice/worktrees/review",
            "/Users/alice/registered",
        )

    assert exc_info.value.status_code == 403
    assert "separate project" in exc_info.value.detail


def test_resolve_repo_rejects_unrelated_same_basename_as_registered_root(tmp_path):
    module = load_worker("graph_worker_unrelated_same_basename_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    registered = module.PROJECTS_DIR / "customer-a" / "Marketing"
    requested = module.PROJECTS_DIR / "customer-b" / "Marketing"
    (registered / ".git").mkdir(parents=True)
    (requested / ".git").mkdir(parents=True)

    with pytest.raises(module.HTTPException) as exc_info:
        module._resolve_repo(
            "/Users/alice/customer-b/Marketing",
            "/Users/alice/customer-a/Marketing",
        )

    assert exc_info.value.status_code == 403
    assert "worktree" in exc_info.value.detail


@pytest.mark.parametrize("target_location", ["sibling", "outside"])
def test_resolve_repo_rejects_inner_directory_symlink_aliases(
    tmp_path, target_location
):
    module = load_worker(f"graph_worker_symlink_escape_{target_location}_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.PROJECTS_DIR.mkdir()
    module.HOST_PROJECTS_ROOT = "/Users/alice"

    if target_location == "sibling":
        target = module.PROJECTS_DIR / "real" / "Marketing"
    else:
        target = tmp_path / "outside" / "Marketing"
    target.mkdir(parents=True)
    alias = module.PROJECTS_DIR / "Drive" / "Marketing"
    alias.parent.mkdir()
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(module.HTTPException) as exc_info:
        module._resolve_repo(
            "/Users/alice/Drive/Marketing",
            "/Users/alice/Drive/Marketing",
        )

    assert exc_info.value.status_code == 400


def test_resolve_repo_rejects_the_projects_mount_root(tmp_path):
    module = load_worker("graph_worker_mount_root_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.PROJECTS_DIR.mkdir()
    module.HOST_PROJECTS_ROOT = "/Users/alice"

    with pytest.raises(module.HTTPException) as exc_info:
        module._resolve_repo("/Users/alice", "/Users/alice")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_non_git_graph_requires_explicit_import_before_atomic_import(tmp_path):
    module = load_worker("graph_worker_import_existing_graph_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "Drive" / "Marketing"
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir(parents=True)
    source_db = graph_dir / "graph.db"
    con = sqlite3.connect(source_db)
    con.execute("CREATE TABLE nodes (id TEXT)")
    con.execute("CREATE TABLE edges (id TEXT)")
    con.executemany("INSERT INTO nodes (id) VALUES (?)", [("a",), ("b",)])
    con.execute("INSERT INTO edges (id) VALUES ('e1')")
    con.commit()
    con.close()
    stale_dir = module.GRAPHS_DIR / "Marketing"
    stale_dir.mkdir(parents=True)
    (stale_dir / "graph.db").write_text("stale", encoding="utf-8")

    with pytest.raises(module.HTTPException) as exc_info:
        await module.build(
            build_body(
                module,
                repo,
                storage_key="marketing-project",
                full=True,
                embed=False,
            )
        )

    assert exc_info.value.status_code == 400
    assert "import_existing=true" in exc_info.value.detail
    assert (stale_dir / "graph.db").read_text(encoding="utf-8") == "stale"

    result = await module.build(
        build_body(
            module,
            repo,
            storage_key="marketing-project",
            embed=False,
            import_existing=True,
        )
    )

    assert result["status"] == "imported-existing-graph"
    assert result["nodes"] == 2
    assert result["edges"] == 1
    assert (
        module.GRAPHS_DIR / "marketing-project" / "graph.db"
    ).read_bytes() == source_db.read_bytes()
    assert (module.GRAPHS_DIR / "Marketing" / "graph.db").read_text(
        encoding="utf-8"
    ) == "stale"


@pytest.mark.asyncio
async def test_build_explicitly_imports_existing_graph_for_git_repo(
    tmp_path, monkeypatch
):
    module = load_worker("graph_worker_explicit_import_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "kaidera-os"
    (repo / ".git").mkdir(parents=True)
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir()
    source_db = graph_dir / "graph.db"
    con = sqlite3.connect(source_db)
    con.execute("CREATE TABLE nodes (id TEXT)")
    con.execute("CREATE TABLE edges (id TEXT)")
    con.execute("INSERT INTO nodes (id) VALUES ('a')")
    con.execute("INSERT INTO edges (id) VALUES ('e1')")
    con.commit()
    con.close()

    monkeypatch.setattr(
        module,
        "_run_bcrg",
        lambda *_args, **_kwargs: pytest.fail("explicit import must not rebuild"),
    )

    result = await module.build(
        build_body(module, repo, import_existing=True, embed=False)
    )

    assert result["status"] == "imported-existing-graph"
    assert result["nodes"] == 1
    assert result["edges"] == 1


@pytest.mark.parametrize(
    "conflicting_mode",
    [
        {"import_existing": True},
        {"import_existing": True, "embed": False, "full": True},
    ],
)
def test_worker_build_body_rejects_ambiguous_import_modes(
    tmp_path,
    conflicting_mode,
):
    module = load_worker("graph_worker_import_mode_contract_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "fixture"
    repo.mkdir(parents=True)

    with pytest.raises(ValueError):
        build_body(module, repo, **conflicting_mode)


def test_run_bcrg_reports_timeout_then_same_worker_path_recovers(monkeypatch):
    module = load_worker("graph_worker_timeout_test")
    calls = []

    def timeout_then_success(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=command, timeout=7)
        return 0, b'{"ok": true}\n', b""

    monkeypatch.setattr(module, "_run_process_bounded", timeout_then_success)

    with pytest.raises(module.HTTPException) as exc_info:
        module._run_bcrg("print('{}')", timeout=7)

    assert exc_info.value.status_code == 504
    assert exc_info.value.detail == (
        "code graph operation exceeded 7s; "
        "the bounded process was terminated; retry the request"
    )
    assert len(calls) == 1

    assert module._run_bcrg("print('{}')", timeout=7) == {"ok": True}
    assert len(calls) == 2
    assert calls[1][0] == calls[0][0]


def test_run_bcrg_uses_the_installed_venv_without_uv_or_runtime_resolution(monkeypatch):
    module = load_worker("graph_worker_exact_bcrg_python_test")
    module.BCRG_PYTHON = "/opt/bcrg/bin/python"
    seen = {}

    def completed(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return 0, b'{"ok": true}\n', b""

    monkeypatch.setattr(module, "_run_process_bounded", completed)

    assert module._run_bcrg("print('{}')") == {"ok": True}
    assert seen["command"] == ["/opt/bcrg/bin/python", "-I", "-c", "print('{}')"]
    assert "uv" not in seen["command"]
    assert seen["kwargs"]["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert seen["kwargs"]["env"]["GIT_PROTOCOL_FROM_USER"] == "0"
    assert int(seen["kwargs"]["env"]["GIT_CONFIG_COUNT"]) >= 15


def test_bcrg_preamble_forces_the_receipt_pinned_qwen_cache_offline(
    tmp_path, monkeypatch
):
    module = load_worker("graph_worker_offline_qwen_preamble_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    repo = tmp_path / "repo"
    repo.mkdir()

    package = types.ModuleType("better_code_review_graph")
    package.__path__ = []
    tools = types.ModuleType("better_code_review_graph.tools")
    embeddings = types.ModuleType("better_code_review_graph.embeddings")

    class Backend:
        def __init__(self):
            self._model = None
            self._model_name = "n24q02m/Qwen3-Embedding-0.6B-ONNX"

    embeddings.Qwen3EmbedBackend = Backend
    package.tools = tools
    package.embeddings = embeddings
    monkeypatch.setitem(sys.modules, "better_code_review_graph", package)
    monkeypatch.setitem(sys.modules, "better_code_review_graph.tools", tools)
    monkeypatch.setitem(sys.modules, "better_code_review_graph.embeddings", embeddings)

    calls = []
    qwen = types.ModuleType("qwen3_embed")

    class TextEmbedding:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    qwen.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "qwen3_embed", qwen)

    preamble = module._tool_preamble("fixture", repo, materialize=True)
    exec(preamble, {})

    monkeypatch.delenv("QWEN3_EMBED_CACHE_PATH", raising=False)
    with pytest.raises(RuntimeError, match="receipt-pinned immutable Qwen cache"):
        Backend()._get_model()
    assert calls == []

    monkeypatch.setenv("QWEN3_EMBED_CACHE_PATH", module.QWEN_CACHE_DIR)
    backend = Backend()
    first = backend._get_model()
    assert backend._get_model() is first
    assert calls == [
        {
            "model_name": "n24q02m/Qwen3-Embedding-0.6B-ONNX",
            "cache_dir": module.QWEN_CACHE_DIR,
            "local_files_only": True,
        }
    ]
    assert tools.get_db_path(repo) == module.GRAPHS_DIR / "fixture" / "graph.db"


@pytest.mark.asyncio
async def test_bcrg_single_flight_sheds_capacity_off_the_event_loop(monkeypatch):
    module = load_worker("graph_worker_single_flight_test")
    module.SINGLE_FLIGHT_ADMISSION_SECONDS = 0.01
    started = threading.Event()
    release = threading.Event()
    thread_ids = []

    def run(_code, *, timeout):
        assert 0 < timeout <= 7
        thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=1)
        return {"ok": True}

    monkeypatch.setattr(module, "_run_bcrg", run)
    event_loop_thread = threading.get_ident()
    first = asyncio.create_task(
        module._run_bcrg_single_flight("one", timeout=7)
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()

    with pytest.raises(module.HTTPException) as exc_info:
        await module._run_bcrg_single_flight("two", timeout=7)

    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "1"}
    release.set()
    assert await first == {"ok": True}
    assert thread_ids and all(
        thread_id != event_loop_thread for thread_id in thread_ids
    )


@pytest.mark.asyncio
async def test_cancelled_request_holds_single_flight_until_thread_exits(monkeypatch):
    module = load_worker("graph_worker_cancelled_single_flight_test")
    module.SINGLE_FLIGHT_ADMISSION_SECONDS = 0.01
    started = threading.Event()
    release = threading.Event()

    def run(_code, *, timeout):
        assert timeout > 0
        started.set()
        assert release.wait(timeout=1)
        return {"ok": True}

    monkeypatch.setattr(module, "_run_bcrg", run)
    first = asyncio.create_task(
        module._run_bcrg_single_flight("one", timeout=7)
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(module.HTTPException) as exc_info:
        await module._run_bcrg_single_flight("overlap", timeout=7)
    assert exc_info.value.status_code == 503

    release.set()
    for _ in range(100):
        if not module._bcrg_slots.locked():
            break
        await asyncio.sleep(0.001)
    assert not module._bcrg_slots.locked()
    assert await module._run_bcrg_single_flight("recovered", timeout=7) == {
        "ok": True
    }


@pytest.mark.asyncio
async def test_health_reports_missing_runtime_dependencies(tmp_path):
    module = load_worker("graph_worker_dependency_health_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    module.GRAPHS_DIR.mkdir()
    module.PROJECTS_DIR.mkdir()
    module.BCRG_PYTHON = str(tmp_path / "missing-bcrg-python")

    result = await module.health()

    assert result["ok"] is False
    assert result["graphs_dir_available"] is True
    assert result["projects_dir_available"] is True
    assert result["bcrg_available"] is False


def test_stats_returns_only_the_requested_project_graph(tmp_path):
    module = load_worker("graph_worker_project_stats_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    for name, node_count in (("customer-a", 2), ("customer-b", 5)):
        graph_dir = module.GRAPHS_DIR / name
        graph_dir.mkdir(parents=True)
        con = sqlite3.connect(graph_dir / "graph.db")
        con.execute("CREATE TABLE nodes (id TEXT)")
        con.execute("CREATE TABLE edges (id TEXT)")
        con.executemany(
            "INSERT INTO nodes (id) VALUES (?)",
            [(f"node-{index}",) for index in range(node_count)],
        )
        con.execute("INSERT INTO edges (id) VALUES ('edge-1')")
        con.commit()
        con.close()

    result = module._collect_stats("customer-a")

    assert result["total_nodes"] == 2
    assert result["total_edges"] == 1
    assert [repo["name"] for repo in result["repos"]] == ["customer-a"]


@pytest.mark.parametrize("db_state", ["missing", "empty"])
def test_stats_rejects_an_unmaterialized_authorized_graph(tmp_path, db_state):
    module = load_worker(f"graph_worker_missing_stats_{db_state}_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    graph_dir = module.GRAPHS_DIR / "authorized"
    graph_dir.mkdir(parents=True)
    if db_state == "empty":
        (graph_dir / "graph.db").touch()

    with pytest.raises(module.HTTPException) as exc_info:
        module._collect_stats("authorized")

    assert exc_info.value.status_code == 409


def test_empty_but_materialized_sqlite_graph_has_an_exact_stats_receipt(tmp_path):
    module = load_worker("graph_worker_empty_materialized_stats_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    graph_dir = module.GRAPHS_DIR / "authorized"
    graph_dir.mkdir(parents=True)
    con = sqlite3.connect(graph_dir / "graph.db")
    con.execute("CREATE TABLE nodes (id TEXT)")
    con.execute("CREATE TABLE edges (id TEXT)")
    con.commit()
    con.close()

    result = module._collect_stats("authorized")

    assert result["total_nodes"] == 0
    assert result["total_edges"] == 0
    assert result["repos"][0]["name"] == "authorized"


def test_stats_endpoint_requires_a_repo_query_parameter():
    module = load_worker("graph_worker_stats_required_scope_test")
    client = TestClient(module.app)

    response = client.get("/stats")

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_prune_dry_run_reports_stale_graph_dirs_without_deleting(tmp_path):
    module = load_worker("graph_worker_prune_dry_run_test")
    module.GRAPHS_DIR = tmp_path
    (tmp_path / "active").mkdir()
    (tmp_path / "active" / "graph.db").write_text("active", encoding="utf-8")
    (tmp_path / "stale").mkdir()
    (tmp_path / "stale" / "graph.db").write_text("stale", encoding="utf-8")

    result = await module.prune(
        module.PruneBody(active_projects=["active"], dry_run=True)
    )

    assert result["dry_run"] is True
    assert [item["name"] for item in result["candidates"]] == ["stale"]
    assert result["pruned"] == []
    assert (tmp_path / "stale" / "graph.db").exists()


@pytest.mark.asyncio
async def test_prune_apply_deletes_only_stale_graph_dirs(tmp_path):
    module = load_worker("graph_worker_prune_apply_test")
    module.GRAPHS_DIR = tmp_path
    (tmp_path / "active").mkdir()
    (tmp_path / "active" / "graph.db").write_text("active", encoding="utf-8")
    (tmp_path / "stale").mkdir()
    (tmp_path / "stale" / "graph.db").write_text("stale", encoding="utf-8")

    result = await module.prune(
        module.PruneBody(active_projects=["active"], dry_run=False)
    )

    assert [item["name"] for item in result["pruned"]] == ["stale"]
    assert (tmp_path / "active" / "graph.db").exists()
    assert not (tmp_path / "stale").exists()


@pytest.mark.asyncio
async def test_prune_recovers_marker_only_failed_generation_directory(tmp_path):
    module = load_worker("graph_worker_prune_failed_generation_test")
    module.GRAPHS_DIR = tmp_path
    active = tmp_path / "active"
    active.mkdir()
    (active / "graph.db").write_text("active", encoding="utf-8")
    failed = tmp_path / "failed"
    failed.mkdir()
    (failed / ".git").write_text("gitdir: /projects/repo/.git\n", encoding="utf-8")

    preview = await module.prune(
        module.PruneBody(active_projects=["active"], dry_run=True)
    )
    assert [item["name"] for item in preview["candidates"]] == ["failed"]
    assert preview["candidates"][0]["graph_db_state"] == "missing"
    assert failed.is_dir()

    applied = await module.prune(
        module.PruneBody(active_projects=["active"], dry_run=False)
    )
    assert applied["pruned_count"] == 1
    assert [item["name"] for item in applied["pruned"]] == ["failed"]
    assert not failed.exists()
    assert (active / "graph.db").exists()


@pytest.mark.asyncio
async def test_prune_streams_over_response_cap_and_removes_every_orphan(tmp_path):
    module = load_worker("graph_worker_prune_over_cap_recovery_test")
    module.GRAPHS_DIR = tmp_path
    module.MAX_GRAPH_RESPONSE_ITEMS = 2
    for index in range(5):
        graph_dir = tmp_path / f"orphan-{index}"
        graph_dir.mkdir()
        (graph_dir / ".git").write_text("failed build", encoding="utf-8")

    preview = await module.prune(module.PruneBody(active_projects=[], dry_run=True))
    assert preview["candidate_count"] == 5
    assert len(preview["candidates"]) == 2
    assert preview["truncated"] is True
    assert len(list(tmp_path.glob("orphan-*"))) == 5

    applied = await module.prune(module.PruneBody(active_projects=[], dry_run=False))
    assert applied["candidate_count"] == 5
    assert applied["pruned_count"] == 5
    assert len(applied["pruned"]) == 2
    assert applied["truncated"] is True
    assert list(tmp_path.glob("orphan-*")) == []


@pytest.mark.asyncio
async def test_prune_rejects_sibling_symlink_without_deleting_target(tmp_path):
    module = load_worker("graph_worker_prune_symlink_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    active = module.GRAPHS_DIR / "active"
    active.mkdir(parents=True)
    (active / "graph.db").write_text("active", encoding="utf-8")
    (module.GRAPHS_DIR / "stale").symlink_to(active, target_is_directory=True)

    with pytest.raises(module.HTTPException) as exc_info:
        await module.prune(module.PruneBody(active_projects=[], dry_run=False))

    assert exc_info.value.status_code == 409
    assert (active / "graph.db").read_text(encoding="utf-8") == "active"
    assert (module.GRAPHS_DIR / "stale").is_symlink()


@pytest.mark.asyncio
async def test_import_rejects_graph_db_symlink_and_preserves_managed_db(tmp_path):
    module = load_worker("graph_worker_import_symlink_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "fixture"
    donor_dir = repo / ".code-review-graph"
    donor_dir.mkdir(parents=True)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"not a donor")
    (donor_dir / "graph.db").symlink_to(outside)
    managed = module.GRAPHS_DIR / "fixture" / "graph.db"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"managed-good")

    with pytest.raises(module.HTTPException) as exc_info:
        await module.build(
            build_body(
                module,
                repo,
                storage_key="fixture",
                import_existing=True,
                embed=False,
            )
        )

    assert exc_info.value.status_code == 400
    assert managed.read_bytes() == b"managed-good"


@pytest.mark.parametrize("unsafe_target", ["symlink", "hardlink"])
def test_full_build_rejects_aliased_managed_db_before_bcrg(
    tmp_path,
    monkeypatch,
    unsafe_target,
):
    module = load_worker(f"graph_worker_full_{unsafe_target}_target_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "fixture"
    (repo / ".git").mkdir(parents=True)
    graph_dir = module.GRAPHS_DIR / "authorized"
    graph_dir.mkdir(parents=True)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"must-not-change")
    managed = graph_dir / "graph.db"
    if unsafe_target == "symlink":
        managed.symlink_to(outside)
    else:
        os.link(outside, managed)
    bcrg_calls = []

    def fake_bcrg(*args, **kwargs):
        bcrg_calls.append((args, kwargs))

    monkeypatch.setattr(module, "_run_bcrg", fake_bcrg)

    with pytest.raises(module.HTTPException) as exc_info:
        module._build_graph(
            build_body(
                module,
                repo,
                storage_key="authorized",
                full=True,
                embed=False,
            ),
            timeout=1,
        )

    assert exc_info.value.status_code == 409
    assert bcrg_calls == []
    assert outside.read_bytes() == b"must-not-change"
    assert managed.read_bytes() == b"must-not-change"


def test_full_build_prepares_one_exact_owner_controlled_db_inode(tmp_path):
    module = load_worker("graph_worker_full_exact_target_test")
    module.GRAPHS_DIR = tmp_path / "graphs"

    graph_dir, identity = module._prepare_materialization_graph_db("authorized")

    managed = graph_dir / "graph.db"
    managed_stat = managed.lstat()
    assert (managed_stat.st_dev, managed_stat.st_ino) == identity
    assert managed_stat.st_uid == os.geteuid()
    assert managed_stat.st_nlink == 1
    assert managed_stat.st_size == 0
    assert oct(managed_stat.st_mode & 0o777) == "0o600"
    assert sorted(path.name for path in graph_dir.iterdir()) == ["graph.db"]


@pytest.mark.asyncio
async def test_import_validates_sqlite_before_atomic_replace(tmp_path):
    module = load_worker("graph_worker_import_invalid_sqlite_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    donor = module.PROJECTS_DIR / "fixture" / ".code-review-graph" / "graph.db"
    donor.parent.mkdir(parents=True)
    donor.write_bytes(b"not sqlite")
    managed = module.GRAPHS_DIR / "fixture" / "graph.db"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"managed-good")

    with pytest.raises(module.HTTPException) as exc_info:
        await module.build(
            build_body(
                module,
                donor.parents[1],
                storage_key="fixture",
                import_existing=True,
                embed=False,
            )
        )

    assert exc_info.value.status_code == 400
    assert "failed validation" in exc_info.value.detail
    assert managed.read_bytes() == b"managed-good"
    assert list(managed.parent.glob(".graph.db.import-*")) == []


@pytest.mark.asyncio
async def test_import_rejects_uncheckpointed_sqlite_sidecar(tmp_path):
    module = load_worker("graph_worker_import_wal_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    donor = module.PROJECTS_DIR / "fixture" / ".code-review-graph" / "graph.db"
    donor.parent.mkdir(parents=True)
    donor.write_bytes(b"sqlite bytes")
    donor.with_name("graph.db-wal").write_bytes(b"pending")

    with pytest.raises(module.HTTPException) as exc_info:
        await module.build(
            build_body(
                module,
                donor.parents[1],
                storage_key="fixture",
                import_existing=True,
                embed=False,
            )
        )

    assert exc_info.value.status_code == 409
    assert "not checkpointed" in exc_info.value.detail


@pytest.mark.asyncio
async def test_import_preserves_managed_db_and_sidecar_when_target_is_dirty(tmp_path):
    module = load_worker("graph_worker_import_managed_wal_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    donor = module.PROJECTS_DIR / "fixture" / ".code-review-graph" / "graph.db"
    donor.parent.mkdir(parents=True)
    con = sqlite3.connect(donor)
    con.execute("CREATE TABLE nodes (id TEXT)")
    con.execute("CREATE TABLE edges (id TEXT)")
    con.execute("INSERT INTO nodes VALUES ('new')")
    con.commit()
    con.close()
    managed = module.GRAPHS_DIR / "fixture" / "graph.db"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"managed-old")
    managed_wal = managed.with_name("graph.db-wal")
    managed_wal.write_bytes(b"pending-managed-state")

    with pytest.raises(module.HTTPException) as exc_info:
        await module.build(
            build_body(
                module,
                donor.parents[1],
                storage_key="fixture",
                import_existing=True,
                embed=False,
            )
        )

    assert exc_info.value.status_code == 409
    assert "active or uncheckpointed" in exc_info.value.detail
    assert managed.read_bytes() == b"managed-old"
    assert managed_wal.read_bytes() == b"pending-managed-state"
    assert list(managed.parent.glob(".graph.db.import-*")) == []


def test_sqlite_readonly_uri_quotes_query_and_fragment_characters(tmp_path):
    module = load_worker("graph_worker_sqlite_uri_quoting_test")
    db = tmp_path / "graph?#%.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE nodes (id TEXT)")
    con.executemany("INSERT INTO nodes VALUES (?)", [("a",), ("b",)])
    con.commit()
    con.close()

    assert module._sqlite_count(db, "nodes") == 2


def test_request_models_reject_extra_fields_and_oversized_lists():
    module = load_worker("graph_worker_request_model_bounds_test")
    scope = {
        "repo": "/host/fixture",
        "registered_repo": "/host/fixture",
        "storage_key": "fixture",
    }

    with pytest.raises(Exception):
        module.BuildBody(**scope, unexpected=True)
    with pytest.raises(Exception):
        module.BlastBody(**scope, files=["x"] * 513)
    with pytest.raises(Exception):
        module.CallersBody(**scope, target="x", max_results=10_001)
    with pytest.raises(Exception):
        module.BuildBody(repo="/host/fixture")
    with pytest.raises(Exception):
        module.BuildBody(**{**scope, "storage_key": "Fixture"})


def test_blast_normalizes_and_bounds_the_pinned_321_contract():
    module = load_worker("graph_worker_blast_contract_test")
    raw = {
        "status": "ok",
        "summary": "Blast radius fixture",
        "changed_files": [f"src/file-{index}.py" for index in range(12)],
        "changed_nodes": [{"name": f"changed-{index}"} for index in range(15)],
        "impacted_nodes": [{"name": f"impacted-{index}"} for index in range(30)],
        "impacted_files": [f"src/impact-{index}.py" for index in range(20)],
        "edges": [{"source": index, "target": index + 1} for index in range(40)],
        "total_impacted": 73,
        "truncated": False,
        "_cortex_totals": {
            "changed_files": 12,
            "changed_nodes": 15,
            "impacted_nodes": 30,
            "impacted_files": 20,
            "edges": 40,
        },
    }

    result = module._normalize_blast_response(raw, max_results=10)

    assert result["status"] == "ok"
    assert len(result["changed_files"]) == 10
    assert len(result["changed_nodes"]) == 10
    assert len(result["impacted_nodes"]) == 10
    assert len(result["impacted_files"]) == 10
    assert len(result["edges"]) == 10
    assert result["totals"] == {
        "changed_files": 12,
        "changed_nodes": 15,
        "impacted_nodes": 30,
        "impacted_files": 20,
        "edges": 40,
    }
    assert result["total_impacted"] == 73
    assert result["truncated"] is True
    assert len(json.dumps(result).encode("utf-8")) <= module.MAX_GRAPH_RESPONSE_BYTES


def test_normalized_graph_response_enforces_the_byte_ceiling():
    module = load_worker("graph_worker_normalized_byte_ceiling_test")
    raw = {
        "status": "ok",
        "summary": "large fixture",
        "changed_files": [],
        "changed_nodes": [],
        "impacted_nodes": [
            {"name": f"node-{index}", "payload": "x" * 4096}
            for index in range(200)
        ],
        "impacted_files": [],
        "edges": [],
        "total_impacted": 200,
    }

    result = module._normalize_blast_response(raw, max_results=256)

    assert len(result["impacted_nodes"]) < 200
    assert result["totals"]["impacted_nodes"] == 200
    assert result["truncated"] is True
    assert len(json.dumps(result).encode("utf-8")) <= module.MAX_GRAPH_RESPONSE_BYTES


def test_callers_preserves_typed_query_status_and_index_proof():
    module = load_worker("graph_worker_callers_status_contract_test")
    resolved = module._normalize_callers_response(
        {
            "status": "ok",
            "pattern": "callers_of",
            "target": "dispatch",
            "description": "Find callers",
            "summary": "Found 2 results",
            "header": {"embeddings_count": 0, "keyword_only": True},
            "results": [{"name": "one"}, {"name": "two"}],
            "edges": [{"source": "one", "target": "dispatch"}],
        },
        max_results=10,
    )
    builtin_skip = module._normalize_callers_response(
        {
            "status": "ok",
            "pattern": "callers_of",
            "target": "map",
            "summary": "common builtin skipped",
            "results": [],
            "edges": [],
        },
        max_results=10,
    )

    assert resolved["target_indexed"] is True
    assert resolved["totals"] == {
        "results": 2,
        "edges": 1,
        "candidates": 0,
        "indexed_kinds": 0,
        "indexed_under": 0,
    }
    assert resolved["truncated"] is False
    assert builtin_skip["status"] == "ok"
    assert builtin_skip["target_indexed"] is False


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (
            {
                "status": "ambiguous",
                "reason": "ambiguous_unqualified",
                "summary": "Multiple matches",
                "candidates": [{"name": f"candidate-{index}"} for index in range(12)],
                "indexed_kinds": ["File", "Function"],
                "indexed_under": ["a.py::target", "b.py::target"],
                "hint": "Pass a qualified symbol",
            },
            "ambiguous",
        ),
        (
            {
                "status": "not_found",
                "reason": "no_such_symbol",
                "summary": "No node found",
                "indexed_kinds": ["Class", "Function"],
                "hint": "Verify the spelling",
            },
            "not_found",
        ),
        (
            {
                "status": "error",
                "error": "Unknown pattern",
            },
            "error",
        ),
    ],
)
def test_callers_normalizes_non_success_query_statuses(raw, status):
    module = load_worker(f"graph_worker_callers_{status}_contract_test")

    result = module._normalize_callers_response(raw, max_results=5)

    assert result["status"] == status
    assert result["target_indexed"] is False
    assert len(result["candidates"]) <= 5
    assert result["truncated"] is (status == "ambiguous")


def test_impact_normalizes_the_pinned_321_context_shape_and_caps_every_collection():
    module = load_worker("graph_worker_impact_contract_test")
    raw = {
        "status": "ok",
        "summary": "Review context fixture",
        "context": {
            "changed_files": [f"src/change-{index}.py" for index in range(8)],
            "impacted_files": [f"src/impact-{index}.py" for index in range(9)],
            "graph": {
                "changed_nodes": [{"name": f"changed-{index}"} for index in range(10)],
                "impacted_nodes": [{"name": f"impact-{index}"} for index in range(11)],
                "edges": [{"source": index, "target": index + 1} for index in range(12)],
            },
            "review_guidance": "Review authentication boundaries first.",
            "untested_functions": [
                {"name": f"untested-{index}"} for index in range(7)
            ],
        },
    }

    result = module._normalize_impact_response(raw, max_results=5)

    assert result["status"] == "ok"
    assert result["context"]["review_guidance"] == (
        "Review authentication boundaries first."
    )
    assert len(result["context"]["changed_files"]) == 5
    assert len(result["context"]["impacted_files"]) == 5
    assert len(result["context"]["graph"]["changed_nodes"]) == 5
    assert len(result["context"]["graph"]["impacted_nodes"]) == 5
    assert len(result["context"]["graph"]["edges"]) == 5
    assert len(result["context"]["untested_functions"]) == 5
    assert result["totals"] == {
        "changed_files": 8,
        "impacted_files": 9,
        "untested_functions": 7,
        "changed_nodes": 10,
        "impacted_nodes": 11,
        "edges": 12,
    }
    assert result["truncated"] is True


def test_impact_accepts_the_pinned_no_changes_context():
    module = load_worker("graph_worker_impact_no_changes_contract_test")

    result = module._normalize_impact_response(
        {
            "status": "ok",
            "summary": "No changes detected. Nothing to review.",
            "context": {},
        },
        max_results=10,
    )

    assert result["context"] == {
        "changed_files": [],
        "impacted_files": [],
        "untested_functions": [],
        "graph": {"changed_nodes": [], "impacted_nodes": [], "edges": []},
        "review_guidance": "",
    }
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_request_body_middleware_rejects_declared_oversize_before_app():
    module = load_worker("graph_worker_declared_body_limit_test")
    called = False
    sent = []

    async def inner(_scope, _receive, _send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = module.RequestBodyLimitMiddleware(inner, max_body_bytes=4)
    await middleware(
        {"type": "http", "headers": [(b"content-length", b"5")]},
        receive,
        send,
    )

    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_request_body_middleware_counts_streamed_chunks():
    module = load_worker("graph_worker_streamed_body_limit_test")
    chunks = iter(
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"de", "more_body": False},
        ]
    )
    sent = []

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    async def consume(_scope, limited_receive, _send):
        while True:
            message = await limited_receive()
            if not message.get("more_body"):
                return

    middleware = module.RequestBodyLimitMiddleware(consume, max_body_bytes=4)
    await middleware({"type": "http", "headers": []}, receive, send)

    assert sent[0]["status"] == 413
    body_message = next(
        message for message in sent if message["type"] == "http.response.body"
    )
    assert json.loads(body_message["body"])["detail"] == "request body is too large"


def test_fastapi_chunked_json_overlimit_emits_one_413_response():
    module = load_worker("graph_worker_fastapi_streamed_body_limit_test")
    test_app = FastAPI()

    @test_app.post("/parse")
    async def parse(request: Request):
        return await request.json()

    test_app.add_middleware(module.RequestBodyLimitMiddleware, max_body_bytes=32)

    def chunks():
        yield b'{"repo":"'
        yield b"x" * 64
        yield b'"}'

    with TestClient(test_app) as client:
        response = client.post(
            "/parse",
            content=chunks(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body is too large"}


def test_bcrg_output_is_stream_capped(tmp_path):
    module = load_worker("graph_worker_output_cap_test")
    module.BCRG_PYTHON = sys.executable
    module.MAX_SUBPROCESS_OUTPUT_BYTES = 64

    with pytest.raises(module.HTTPException) as exc_info:
        module._run_bcrg("print('x' * 1000)", timeout=2)

    assert exc_info.value.status_code == 502
    assert "output limit" in exc_info.value.detail


def test_bounded_runner_kills_descendant_process_group_on_timeout(tmp_path):
    module = load_worker("graph_worker_process_group_timeout_test")
    marker = tmp_path / "orphan-marker"
    child_code = (
        "import pathlib,time; time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('orphan', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(10)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        module._run_process_bounded(
            [sys.executable, "-c", parent_code],
            timeout=0.1,
            env=module._bcrg_environment(),
        )
    time.sleep(0.7)

    assert not marker.exists()


def test_bounded_runner_reaps_descendant_process_group_after_success(tmp_path):
    module = load_worker("graph_worker_process_group_success_test")
    marker = tmp_path / "successful-parent-orphan-marker"
    child_code = (
        "import pathlib,time; time.sleep(0.6); "
        f"pathlib.Path({str(marker)!r}).write_text('orphan', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL)"
    )

    returncode, stdout, stderr = module._run_process_bounded(
        [sys.executable, "-c", parent_code],
        timeout=2,
        env=module._bcrg_environment(),
    )
    time.sleep(0.8)

    assert returncode == 0
    assert stdout == b""
    assert stderr == b""
    assert not marker.exists()


def test_bounded_runner_kills_sigterm_resistant_descendant_after_leader_exits(
    tmp_path,
):
    module = load_worker("graph_worker_sigterm_resistant_descendant_test")
    marker = tmp_path / "sigterm-resistant-orphan-marker"
    child_code = (
        "import pathlib,time; time.sleep(1.2); "
        f"pathlib.Path({str(marker)!r}).write_text('orphan', encoding='utf-8')"
    )
    parent_code = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "signal.signal(signal.SIGTERM, signal.SIG_DFL); time.sleep(10)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        module._run_process_bounded(
            [sys.executable, "-c", parent_code],
            timeout=0.2,
            env=module._bcrg_environment(),
        )
    time.sleep(1.4)

    assert not marker.exists()


def test_tool_preamble_rejects_repo_git_config_external_commands(tmp_path):
    module = load_worker("graph_worker_dangerous_git_config_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.GRAPHS_DIR = tmp_path / "graphs"
    repo = module.PROJECTS_DIR / "fixture"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n"
        '[diff "evil"]\n\tcommand = /tmp/attacker-command\n',
        encoding="utf-8",
    )

    with pytest.raises(module.HTTPException) as exc_info:
        module._tool_preamble("fixture", repo, materialize=True)

    assert exc_info.value.status_code == 400
    assert "executable directives" in exc_info.value.detail


@pytest.mark.parametrize("graph_state", ["never-created", "deleted"])
@pytest.mark.parametrize(
    "operation",
    ["stats", "incremental", "blast", "callers", "impact", "large-fn"],
)
def test_non_materializing_operations_never_create_a_missing_authorized_db(
    tmp_path,
    monkeypatch,
    graph_state,
    operation,
):
    module = load_worker(f"graph_worker_missing_db_{graph_state}_{operation}_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.GRAPHS_DIR.mkdir()
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "fixture"
    (repo / ".git").mkdir(parents=True)
    graph_dir = module.GRAPHS_DIR / "authorized"
    if graph_state == "deleted":
        graph_dir.mkdir()
        db = graph_dir / "graph.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE nodes (id TEXT)")
        con.execute("CREATE TABLE edges (id TEXT)")
        con.commit()
        con.close()
        db.unlink()

    def fail_bcrg(*_args, **_kwargs):
        raise AssertionError("missing authorized DB reached BCRG")

    monkeypatch.setattr(module, "_run_bcrg", fail_bcrg)
    scope = {
        "repo": host_path(module, repo),
        "registered_repo": host_path(module, repo),
        "storage_key": "authorized",
    }
    body = {
        "incremental": module.BuildBody(**scope, full=False, embed=False),
        "blast": module.BlastBody(**scope, files=["src/a.py"]),
        "callers": module.CallersBody(**scope, target="dispatch"),
        "impact": module.ImpactBody(**scope),
        "large-fn": module.LargeFnBody(**scope),
    }.get(operation)

    with pytest.raises(module.HTTPException) as exc_info:
        if operation == "stats":
            module._collect_stats("authorized")
        elif operation == "incremental":
            module._build_graph(body, timeout=1)
        else:
            getattr(module, f"_{operation.replace('-', '_')}")(body, timeout=1)

    assert exc_info.value.status_code == 409
    assert not (graph_dir / "graph.db").exists()
    if graph_state == "never-created":
        assert not graph_dir.exists()
