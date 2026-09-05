import asyncio
import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    module_root = str(path.parent)
    added_to_path = module_root not in sys.path
    if added_to_path:
        sys.path.insert(0, module_root)
    try:
        spec.loader.exec_module(module)
    finally:
        if added_to_path:
            sys.path.remove(module_root)
    return module


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


def admin_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/graph/prune",
            "headers": [(b"x-cortex-admin-token", b"cortex-local-admin")],
            "query_string": b"",
        }
    )


def graph_project_row(
    project: str,
    *,
    repo_root: str,
    generation_label: str = "initial",
    requires_full_rebuild: bool = False,
):
    return {
        "project_key": project,
        "project_id": str(uuid5(NAMESPACE_URL, f"cortex-project:{project}")),
        "repo_root": repo_root,
        "graph_generation": str(
            uuid5(NAMESPACE_URL, f"cortex-graph:{project}:{generation_label}")
        ),
        "graph_requires_full_rebuild": requires_full_rebuild,
    }


def expected_storage_key(
    module,
    project: str,
    *,
    repo_root: str = "/projects/kaidera-os",
    generation_label: str = "initial",
):
    return module.graph_storage_key(
        project,
        graph_project_row(
            project,
            repo_root=repo_root,
            generation_label=generation_label,
        ),
    )


def graph_build_receipt(*, full: bool, embed: bool = False):
    result = {
        "status": "ok",
        "build_type": "full" if full else "incremental",
        "summary": "graph build complete",
        "total_nodes": 10,
        "total_edges": 2,
        "errors": [],
    }
    if full:
        result["files_parsed"] = 4
    else:
        result.update(
            {
                "files_updated": 1,
                "changed_files": ["src/example.py"],
                "dependent_files": [],
            }
        )
    if embed:
        result["embeddings"] = {
            "status": "ok",
            "summary": "embeddings complete",
            "backend": "local",
            "newly_embedded": 10,
            "total_embeddings": 10,
        }
    return result


def allow_registered_project(module, monkeypatch, *, repo_root="/projects/kaidera-os"):
    async def fake_registered(project):
        return graph_project_row(project, repo_root=repo_root)

    async def fake_unique(_project, _storage_key, _project_row):
        return None

    async def fake_hold(_project, _generation):
        return None

    async def fake_mark_ready(_project, _generation):
        return None

    @asynccontextmanager
    async def fake_lease():
        yield

    monkeypatch.setattr(module, "require_registered_project", fake_registered)
    monkeypatch.setattr(module, "require_unique_graph_storage_key", fake_unique)
    monkeypatch.setattr(module, "hold_graph_generation_for_mutation", fake_hold)
    monkeypatch.setattr(module, "mark_graph_generation_ready", fake_mark_ready)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)


@pytest.mark.asyncio
async def test_ensure_graph_schema_does_not_run_ddl_for_a_migrated_database():
    module = load_module(API_MAIN_PATH, "cortex_api_graph_schema_ready_test")

    class Conn:
        async def fetchval(self, sql, *args):
            assert "to_regclass('public.cortex_entities')" in sql
            assert "to_regclass('public.cortex_relationships')" in sql
            return True

        async def execute(self, sql, *args):
            raise AssertionError("request-time graph reads must not run schema DDL")

    await module.ensure_graph_schema(Conn())


@pytest.mark.asyncio
async def test_cortex_graph_project_stats_is_project_scoped(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_l4_stats_test")
    calls = []

    class Conn:
        pass

    async def fake_ensure_graph_schema(conn):
        calls.append(("ensure_graph_schema", conn))

    async def fake_ensure_work_products_schema(conn):
        calls.append(("ensure_work_products_schema", conn))

    async def fake_graph_stats(conn, project):
        calls.append(("graph_stats", conn, project))
        return {
            "entity_count": 7,
            "relationship_count": 11,
            "source_counts": {"decisions": 3, "lessons": 1, "knowledge": 2, "work_products": 1},
            "backlog": {"decisions": 2, "lessons": 0, "knowledge": 0, "work_products": 0},
        }

    conn = Conn()
    monkeypatch.setattr(module, "acquire_scoped", lambda project: FakeAcquire(conn))
    monkeypatch.setattr(module, "ensure_graph_schema", fake_ensure_graph_schema)
    monkeypatch.setattr(module, "ensure_work_products_schema", fake_ensure_work_products_schema)
    monkeypatch.setattr(module, "graph_stats", fake_graph_stats)

    result = await module.cortex_graph_project_stats(x_project="marketing")

    assert result["entity_count"] == 7
    assert result["relationship_count"] == 11
    assert result["source_counts"]["work_products"] == 1
    assert calls == [
        ("ensure_graph_schema", conn),
        ("ensure_work_products_schema", conn),
        ("graph_stats", conn, "marketing"),
    ]


@pytest.mark.asyncio
async def test_cortex_memory_graph_uses_existing_relationship_schema(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_memory_graph_test")
    calls = []

    class Conn:
        async def fetchval(self, sql, *args):
            if "to_regclass('public.work_products')" in sql:
                return False
            raise AssertionError(f"Unexpected fetchval SQL: {sql}")

        async def fetch(self, sql, *args):
            calls.append((sql, args))
            if "FROM cortex_entities" in sql:
                assert "jsonb_typeof(properties->'source_refs') = 'array'" in sql
                return [
                    {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "name": "Marlow",
                        "entity_type": "agent",
                        "description": "lead agent",
                        "source_refs": [],
                        "source_count": 3,
                        "updated_at": "2026-06-27T00:00:00+00:00",
                    },
                    {
                        "id": "22222222-2222-4222-8222-222222222222",
                        "name": "Publishing cadence",
                        "entity_type": "concept",
                        "description": "scheduled work",
                        "source_refs": [],
                        "source_count": 1,
                        "updated_at": "2026-06-27T00:00:00+00:00",
                    },
                ]
            if "FROM cortex_relationships r" in sql:
                assert "r.updated_at" not in sql
                assert "ORDER BY r.created_at DESC" in sql
                return [
                    {
                        "id": "33333333-3333-4333-8333-333333333333",
                        "relationship_type": "owns",
                        "description": "",
                        "source_id": "11111111-1111-4111-8111-111111111111",
                        "source": "Marlow",
                        "source_type": "agent",
                        "target_id": "22222222-2222-4222-8222-222222222222",
                        "target": "Publishing cadence",
                        "target_type": "concept",
                    }
                ]
            raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def fake_ensure_graph_schema(_conn):
        return None

    async def fake_ensure_work_products_schema(_conn):
        return None

    conn = Conn()
    monkeypatch.setattr(module, "acquire_scoped", lambda project: FakeAcquire(conn))
    monkeypatch.setattr(module, "ensure_graph_schema", fake_ensure_graph_schema)
    monkeypatch.setattr(module, "ensure_work_products_schema", fake_ensure_work_products_schema)

    result = await module.cortex_memory_graph(x_project="marketing", limit=50)

    assert result["project"] == "marketing"
    assert [node["name"] for node in result["nodes"]] == ["Marlow", "Publishing cadence"]
    assert result["edges"][0]["relationship_type"] == "owns"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_graph_build_full_request_returns_pollable_job(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_job_test")
    allow_registered_project(module, monkeypatch)

    async def fake_create_job(project, body):
        assert project == "kaidera-os"
        assert body.repo == "/projects/kaidera-os"
        assert body.full is True
        return "44444444-4444-4444-4444-444444444444"

    scheduled = []

    def fake_schedule(project, job_id, body):
        scheduled.append((project, job_id, body))

    monkeypatch.setattr(module, "create_graph_build_job", fake_create_job)
    monkeypatch.setattr(module, "schedule_graph_build_job", fake_schedule)

    result = await module.graph_build_proxy(
        module.GraphBuildRequest(repo="kaidera-os", full=True),
        x_project="kaidera-os",
    )

    assert result.status_code == 202
    payload = json.loads(result.body)
    assert payload["job_id"] == "44444444-4444-4444-4444-444444444444"
    assert payload["status_url"] == "/graph/build/jobs/44444444-4444-4444-4444-444444444444"
    assert payload["status"] == "queued"
    assert scheduled


@pytest.mark.asyncio
async def test_graph_build_sync_request_still_proxies_immediately(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_sync_test")
    allow_registered_project(module, monkeypatch)
    storage_key = expected_storage_key(module, "kaidera-os")

    async def fake_execute(body, **scope):
        assert body.repo == "/projects/kaidera-os"
        assert body.full is False
        assert body.async_job is False
        assert scope == {
            "storage_key": storage_key,
            "registered_repo": "/projects/kaidera-os",
        }
        return graph_build_receipt(full=False, embed=True)

    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    result = await module.graph_build_proxy(
        module.GraphBuildRequest(repo="kaidera-os"),
        x_project="kaidera-os",
    )

    assert result == graph_build_receipt(full=False, embed=True)


@pytest.mark.asyncio
async def test_graph_build_rejects_any_root_other_than_registered_root(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_scope_test")
    allow_registered_project(module, monkeypatch)
    worker_calls = []

    async def fake_execute(*_args, **_kwargs):
        worker_calls.append("called")

    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    with pytest.raises(module.HTTPException) as exc_info:
        await module.graph_build_proxy(
            module.GraphBuildRequest(repo="/projects/marketing"),
            x_project="kaidera-os",
        )

    assert exc_info.value.status_code == 403
    assert worker_calls == []


@pytest.mark.asyncio
async def test_graph_build_accepts_registered_repo_root_with_different_basename(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_repo_root_test")
    repo_root = "/projects/marketing-workspace"
    allow_registered_project(module, monkeypatch, repo_root=repo_root)

    async def fake_execute(body, **_scope):
        assert body.repo == repo_root
        return graph_build_receipt(full=False, embed=True)

    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    result = await module.graph_build_proxy(
        module.GraphBuildRequest(repo=repo_root),
        x_project="marketing",
    )

    assert result == graph_build_receipt(full=False, embed=True)


@pytest.mark.asyncio
async def test_graph_repo_dot_alias_resolves_only_to_scoped_registered_root(
    monkeypatch,
):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_dot_alias_test")
    repo_root = "/projects/customer-workspace"
    allow_registered_project(module, monkeypatch, repo_root=repo_root)

    canonical_repo, storage_key, registered_repo, generation = (
        await module.require_graph_repo_scope("customer", ".")
    )

    assert canonical_repo == repo_root
    assert storage_key == expected_storage_key(
        module, "customer", repo_root=repo_root
    )
    assert registered_repo == repo_root
    assert generation == graph_project_row(
        "customer", repo_root=repo_root
    )["graph_generation"]


@pytest.mark.parametrize(
    ("route_name", "model_name", "body_kwargs"),
    [
        ("graph_blast_proxy", "GraphBlastRequest", {"files": ["api/main.py"]}),
        ("graph_callers_proxy", "GraphCallersRequest", {"target": "main"}),
        ("graph_impact_proxy", "GraphImpactRequest", {"base": "HEAD~1"}),
        ("graph_large_fn_proxy", "GraphLargeFnRequest", {"min_lines": 200}),
    ],
)
@pytest.mark.asyncio
async def test_graph_query_routes_reject_unregistered_worktree_before_worker(
    monkeypatch, route_name, model_name, body_kwargs
):
    module = load_module(API_MAIN_PATH, f"cortex_api_{route_name}_scope_test")
    allow_registered_project(module, monkeypatch)
    worker_calls = []

    async def fake_proxy(*args, **kwargs):
        worker_calls.append((args, kwargs))

    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)
    body = getattr(module, model_name)(repo="/projects/marketing", **body_kwargs)

    with pytest.raises(module.HTTPException) as exc_info:
        await getattr(module, route_name)(body, x_project="kaidera-os")

    assert exc_info.value.status_code == 403
    assert worker_calls == []


@pytest.mark.parametrize(
    ("route_name", "model_name", "body_kwargs", "worker_path", "timeout"),
    [
        (
            "graph_blast_proxy",
            "GraphBlastRequest",
            {"files": ["api/main.py"]},
            "/blast",
            130.0,
        ),
        (
            "graph_callers_proxy",
            "GraphCallersRequest",
            {"target": "main"},
            "/callers",
            70.0,
        ),
        (
            "graph_impact_proxy",
            "GraphImpactRequest",
            {"base": "HEAD~1"},
            "/impact",
            130.0,
        ),
        (
            "graph_large_fn_proxy",
            "GraphLargeFnRequest",
            {"min_lines": 200},
            "/large-fn",
            70.0,
        ),
    ],
)
@pytest.mark.asyncio
async def test_graph_query_proxy_timeout_has_worker_delivery_headroom(
    monkeypatch,
    route_name,
    model_name,
    body_kwargs,
    worker_path,
    timeout,
):
    module = load_module(API_MAIN_PATH, f"cortex_api_{route_name}_timeout_test")
    allow_registered_project(module, monkeypatch)

    async def fake_proxy(worker_url, path, **kwargs):
        assert worker_url == module.GRAPH_WORKER_URL
        assert path == worker_path
        assert kwargs["timeout"] == timeout
        return {"ok": True}

    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)
    body = getattr(module, model_name)(repo="kaidera-os", **body_kwargs)

    assert await getattr(module, route_name)(body, x_project="kaidera-os") == {
        "ok": True
    }


@pytest.mark.parametrize(
    ("route_name", "model_name", "body_kwargs"),
    [
        ("graph_build_proxy", "GraphBuildRequest", {}),
        ("graph_blast_proxy", "GraphBlastRequest", {"files": ["api/main.py"]}),
        ("graph_callers_proxy", "GraphCallersRequest", {"target": "main"}),
        ("graph_impact_proxy", "GraphImpactRequest", {"base": "HEAD~1"}),
        ("graph_large_fn_proxy", "GraphLargeFnRequest", {"min_lines": 200}),
    ],
)
@pytest.mark.asyncio
async def test_every_graph_repo_route_rejects_unknown_project_before_worker(
    monkeypatch, route_name, model_name, body_kwargs
):
    module = load_module(API_MAIN_PATH, f"cortex_api_{route_name}_unknown_test")

    async def missing_project(project):
        raise module.HTTPException(404, f"unknown project: {project}")

    async def fail_proxy(*_args, **_kwargs):
        raise AssertionError("unknown-project graph request reached the worker")

    @asynccontextmanager
    async def fake_lease():
        yield

    monkeypatch.setattr(module, "require_registered_project", missing_project)
    monkeypatch.setattr(module, "proxy_worker_json", fail_proxy)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)
    body = getattr(module, model_name)(repo="unknown", **body_kwargs)

    with pytest.raises(module.HTTPException) as exc_info:
        await getattr(module, route_name)(body, x_project="unknown")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_graph_stats_is_bound_to_project_owned_storage_key(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_stats_scope_test")
    allow_registered_project(module, monkeypatch, repo_root="/projects/customer-workspace")
    storage_key = expected_storage_key(
        module,
        "customer",
        repo_root="/projects/customer-workspace",
    )
    calls = []

    async def fake_proxy(worker_url, path, **kwargs):
        calls.append((worker_url, path, kwargs))
        return {
            "total_nodes": 0,
            "total_edges": 0,
            "repos": [
                {
                    "name": storage_key,
                    "nodes": 0,
                    "edges": 0,
                    "path": f"/graphs/{storage_key}/graph.db",
                }
            ],
        }

    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)
    result = await module.graph_stats_proxy(x_project="customer")

    assert result["total_nodes"] == 0
    assert result["repos"][0]["name"] == storage_key
    assert calls == [(
        module.GRAPH_WORKER_URL,
        "/stats",
        {"params": {"storage_key": storage_key}, "timeout": 30.0},
    )]


@pytest.mark.parametrize(
    "worker_result",
    [
        {"total_nodes": 0, "total_edges": 0, "repos": []},
        {
            "total_nodes": 0,
            "total_edges": 0,
            "repos": [{"name": "wrong-store", "nodes": 0, "edges": 0, "path": "/graphs/wrong"}],
        },
        {
            "total_nodes": 0,
            "total_edges": 0,
            "repos": [{"name": "expected", "error": "database is corrupt"}],
        },
        {"total_nodes": "0", "total_edges": 0, "repos": []},
        {
            "total_nodes": 2,
            "total_edges": 1,
            "repos": [{"name": "expected", "nodes": 1, "edges": 1, "path": "/graphs/expected/graph.db"}],
        },
    ],
)
def test_graph_stats_contract_rejects_wrong_partial_or_malformed_store(
    worker_result,
):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_stats_contract_test")

    with pytest.raises(module.HTTPException) as exc_info:
        module.require_graph_stats_success(worker_result, storage_key="expected")

    assert exc_info.value.status_code == 502


@pytest.mark.parametrize("materializer", ["full", "import"])
@pytest.mark.asyncio
async def test_fresh_project_graph_is_held_until_exact_materialization_receipt(
    monkeypatch,
    materializer,
):
    module = load_module(API_MAIN_PATH, f"cortex_api_fresh_graph_{materializer}_test")
    state = graph_project_row(
        "new-project",
        repo_root="/projects/new-project",
        requires_full_rebuild=True,
    )
    worker_calls = []

    async def fake_registered(_project):
        return dict(state)

    @asynccontextmanager
    async def fake_lease():
        yield

    async def fake_proxy(_worker_url, path, **kwargs):
        worker_calls.append((path, kwargs))
        storage_key = kwargs["params"]["storage_key"]
        return {
            "total_nodes": 0,
            "total_edges": 0,
            "repos": [
                {
                    "name": storage_key,
                    "nodes": 0,
                    "edges": 0,
                    "path": f"/graphs/{storage_key}/graph.db",
                }
            ],
        }

    async def fake_execute(body, **scope):
        worker_calls.append(("/build", scope))
        if body.import_existing:
            return {
                "status": "imported-existing-graph",
                "storage_key": scope["storage_key"],
                "repo": "/projects/new-project",
                "source": "/projects/new-project/.code-review-graph/graph.db",
                "graph_db": f"/graphs/{scope['storage_key']}/graph.db",
                "nodes": 0,
                "edges": 0,
            }
        return graph_build_receipt(full=True, embed=False)

    async def fake_mark_ready(project, generation):
        assert project == "new-project"
        assert generation == state["graph_generation"]
        state["graph_requires_full_rebuild"] = False

    async def fake_hold(project, generation):
        assert project == "new-project"
        assert generation == state["graph_generation"]
        state["graph_requires_full_rebuild"] = True

    monkeypatch.setattr(module, "require_registered_project", fake_registered)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)
    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)
    monkeypatch.setattr(module, "hold_graph_generation_for_mutation", fake_hold)
    monkeypatch.setattr(module, "mark_graph_generation_ready", fake_mark_ready)

    with pytest.raises(module.HTTPException) as stats_error:
        await module.graph_stats_proxy(x_project="new-project")
    assert stats_error.value.status_code == 409
    with pytest.raises(module.HTTPException) as query_error:
        await module.graph_callers_proxy(
            module.GraphCallersRequest(repo="new-project", target="main"),
            x_project="new-project",
        )
    assert query_error.value.status_code == 409
    with pytest.raises(module.HTTPException) as incremental_error:
        await module.graph_build_proxy(
            module.GraphBuildRequest(repo="new-project", embed=False),
            x_project="new-project",
        )
    assert incremental_error.value.status_code == 409
    assert worker_calls == []

    body = module.GraphBuildRequest(
        repo="new-project",
        full=materializer == "full",
        import_existing=materializer == "import",
        sync=True,
        embed=False,
    )
    await module.graph_build_proxy(body, x_project="new-project")

    assert state["graph_requires_full_rebuild"] is False
    assert [call[0] for call in worker_calls] == ["/build"]
    await module.graph_stats_proxy(x_project="new-project")
    assert [call[0] for call in worker_calls] == ["/build", "/stats"]


@pytest.mark.asyncio
async def test_graph_storage_key_rejects_any_non_project_authority(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_collision_test")
    row = graph_project_row("marketing-a", repo_root="/projects/marketing-a")

    with pytest.raises(module.HTTPException) as exc_info:
        await module.require_unique_graph_storage_key(
            "marketing-a", "Marketing", row
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_repo_root_generation_move_blocks_old_graph_until_full_rebuild(
    monkeypatch,
):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_generation_move_test")
    state = graph_project_row("kaidera-os", repo_root="/projects/root-a")
    worker_calls = []

    async def fake_registered(project):
        assert project == "kaidera-os"
        return dict(state)

    @asynccontextmanager
    async def fake_lease():
        yield

    async def fake_proxy(_worker_url, path, **kwargs):
        worker_calls.append((path, kwargs["payload"]))
        return {"status": "ok", "target_indexed": True, "results": []}

    async def fake_build(body, **scope):
        worker_calls.append(("/build", {"repo": body.repo, **scope}))
        return graph_build_receipt(full=body.full, embed=body.embed)

    async def fake_mark_ready(project, generation):
        assert project == "kaidera-os"
        assert generation == state["graph_generation"]
        state["graph_requires_full_rebuild"] = False

    async def fake_hold(project, generation):
        assert project == "kaidera-os"
        assert generation == state["graph_generation"]
        state["graph_requires_full_rebuild"] = True

    monkeypatch.setattr(module, "require_registered_project", fake_registered)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)
    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_build)
    monkeypatch.setattr(module, "hold_graph_generation_for_mutation", fake_hold)
    monkeypatch.setattr(module, "mark_graph_generation_ready", fake_mark_ready)

    query = module.GraphCallersRequest(repo="kaidera-os", target="main")
    await module.graph_callers_proxy(query, x_project="kaidera-os")
    generation_a_key = worker_calls[-1][1]["storage_key"]

    state.update(
        graph_project_row(
            "kaidera-os",
            repo_root="/projects/root-b",
            generation_label="root-b",
            requires_full_rebuild=True,
        )
    )
    generation_b_key = module.graph_storage_key("kaidera-os", state)
    assert generation_b_key != generation_a_key

    with pytest.raises(module.HTTPException) as query_error:
        await module.graph_callers_proxy(query, x_project="kaidera-os")
    assert query_error.value.status_code == 409
    assert len(worker_calls) == 1

    with pytest.raises(module.HTTPException) as incremental_error:
        await module.graph_build_proxy(
            module.GraphBuildRequest(repo="kaidera-os"),
            x_project="kaidera-os",
        )
    assert incremental_error.value.status_code == 409
    assert len(worker_calls) == 1

    rebuilt = await module.graph_build_proxy(
        module.GraphBuildRequest(repo="kaidera-os", full=True, sync=True),
        x_project="kaidera-os",
    )
    assert rebuilt == graph_build_receipt(full=True, embed=True)
    assert worker_calls[-1][1]["storage_key"] == generation_b_key
    assert worker_calls[-1][1]["repo"] == "/projects/root-b"

    await module.graph_callers_proxy(query, x_project="kaidera-os")
    assert worker_calls[-1][1]["storage_key"] == generation_b_key
    assert generation_a_key not in {
        call[1]["storage_key"] for call in worker_calls[1:]
    }


@pytest.mark.asyncio
async def test_graph_build_import_existing_flag_reaches_worker(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_import_test")

    async def fake_proxy(worker_url, path, *, method="GET", payload=None, timeout=120.0):
        assert worker_url == module.GRAPH_WORKER_URL
        assert path == "/build"
        assert method == "POST"
        assert timeout == 650.0
        assert payload == {
            "repo": "kaidera-os",
            "storage_key": "kaidera-os",
            "registered_repo": "/projects/kaidera-os",
            "full": False,
            "embed": False,
            "import_existing": True,
        }
        return {
            "status": "imported-existing-graph",
            "storage_key": "kaidera-os",
            "repo": "/projects/kaidera-os",
            "source": "/projects/kaidera-os/.code-review-graph/graph.db",
            "graph_db": "/graphs/kaidera-os/graph.db",
            "nodes": 10,
            "edges": 2,
        }

    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)

    result = await module.execute_graph_build_request(
        module.GraphBuildRequest(
            repo="kaidera-os",
            embed=False,
            import_existing=True,
        ),
        storage_key="kaidera-os",
        registered_repo="/projects/kaidera-os",
    )

    assert result["status"] == "imported-existing-graph"
    assert result["storage_key"] == "kaidera-os"


@pytest.mark.parametrize(
    "conflicting_mode",
    [
        {"import_existing": True},
        {"import_existing": True, "embed": False, "full": True},
    ],
)
def test_graph_build_request_rejects_ambiguous_import_modes(conflicting_mode):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_import_mode_test")

    with pytest.raises(ValueError):
        module.GraphBuildRequest(repo="kaidera-os", **conflicting_mode)


@pytest.mark.asyncio
async def test_graph_build_full_sync_override_proxies_immediately(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_full_sync_test")
    allow_registered_project(module, monkeypatch)

    async def fake_execute(body, **_scope):
        assert body.repo == "/projects/kaidera-os"
        assert body.full is True
        assert body.sync is True
        return graph_build_receipt(full=True, embed=True)

    async def fail_create_job(_project, _body):
        raise AssertionError("sync override should not create a graph build job")

    completed = []

    async def fake_mark_ready(project, generation):
        completed.append((project, generation))

    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)
    monkeypatch.setattr(module, "create_graph_build_job", fail_create_job)
    monkeypatch.setattr(module, "mark_graph_generation_ready", fake_mark_ready)

    result = await module.graph_build_proxy(
        module.GraphBuildRequest(repo="kaidera-os", full=True, sync=True),
        x_project="kaidera-os",
    )

    assert result == graph_build_receipt(full=True, embed=True)
    assert completed == [
        (
            "kaidera-os",
            graph_project_row(
                "kaidera-os", repo_root="/projects/kaidera-os"
            )["graph_generation"],
        )
    ]


@pytest.mark.parametrize(
    "receipt",
    [
        {"status": "error", "error": "parser failed"},
        {"status": "ok", "build_type": "full"},
    ],
)
@pytest.mark.asyncio
async def test_full_sync_build_does_not_clear_generation_hold_without_exact_receipt(
    monkeypatch,
    receipt,
):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_receipt_sync_test")
    state = graph_project_row(
        "kaidera-os",
        repo_root="/projects/kaidera-os",
        requires_full_rebuild=True,
    )
    marked = []

    async def fake_registered(_project):
        return dict(state)

    @asynccontextmanager
    async def fake_lease():
        yield

    async def fake_execute(_body, **_scope):
        return dict(receipt)

    async def fake_mark_ready(project, generation):
        marked.append((project, generation))
        state["graph_requires_full_rebuild"] = False

    async def fake_hold(project, generation):
        assert project == "kaidera-os"
        assert generation == state["graph_generation"]
        state["graph_requires_full_rebuild"] = True

    monkeypatch.setattr(module, "require_registered_project", fake_registered)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)
    monkeypatch.setattr(module, "hold_graph_generation_for_mutation", fake_hold)
    monkeypatch.setattr(module, "mark_graph_generation_ready", fake_mark_ready)

    with pytest.raises(module.HTTPException) as exc_info:
        await module.graph_build_proxy(
            module.GraphBuildRequest(
                repo="kaidera-os",
                full=True,
                sync=True,
                embed=False,
            ),
            x_project="kaidera-os",
        )

    assert exc_info.value.status_code == 502
    assert marked == []
    assert state["graph_requires_full_rebuild"] is True


@pytest.mark.asyncio
async def test_full_background_build_failure_keeps_generation_held(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_receipt_async_test")
    state = graph_project_row(
        "kaidera-os",
        repo_root="/projects/kaidera-os",
        requires_full_rebuild=True,
    )
    updates = []
    marked = []

    async def fake_registered(_project):
        return dict(state)

    @asynccontextmanager
    async def fake_lease():
        yield

    async def fake_execute(_body, **_scope):
        return {"status": "error", "error": "full build failed"}

    async def fake_persist(project, job_id, **kwargs):
        updates.append((project, job_id, kwargs))

    async def fake_mark_ready(project, generation):
        marked.append((project, generation))
        state["graph_requires_full_rebuild"] = False

    async def fake_hold(project, generation):
        assert project == "kaidera-os"
        assert generation == state["graph_generation"]
        state["graph_requires_full_rebuild"] = True

    monkeypatch.setattr(module, "require_registered_project", fake_registered)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)
    monkeypatch.setattr(module, "persist_graph_build_job", fake_persist)
    monkeypatch.setattr(module, "hold_graph_generation_for_mutation", fake_hold)
    monkeypatch.setattr(module, "mark_graph_generation_ready", fake_mark_ready)

    await module.run_graph_build_job(
        "kaidera-os",
        "99999999-9999-4999-8999-999999999999",
        module.GraphBuildRequest(repo="kaidera-os", full=True, embed=False),
    )

    assert updates[0][2]["status"] == "running"
    assert updates[-1][2]["status"] == "failed"
    assert "did not complete the requested full build" in updates[-1][2]["error"]
    assert marked == []
    assert state["graph_requires_full_rebuild"] is True


@pytest.mark.parametrize(
    ("build_mode", "outcome"),
    [
        ("full", "partial"),
        ("full", "exception"),
        ("incremental", "partial"),
    ],
)
@pytest.mark.asyncio
async def test_failed_live_build_is_held_before_mutation_and_blocks_all_reuse(
    monkeypatch,
    build_mode,
    outcome,
):
    module = load_module(
        API_MAIN_PATH,
        f"cortex_api_live_build_hold_{build_mode}_{outcome}_test",
    )
    state = graph_project_row("kaidera-os", repo_root="/projects/kaidera-os")
    events = []
    worker_calls = []
    marked = []

    async def fake_registered(_project):
        return dict(state)

    @asynccontextmanager
    async def fake_lease():
        yield

    async def fake_hold(project, generation):
        assert project == "kaidera-os"
        assert generation == state["graph_generation"]
        events.append("held")
        state["graph_requires_full_rebuild"] = True

    async def fake_execute(body, **_scope):
        assert state["graph_requires_full_rebuild"] is True
        events.append("worker")
        worker_calls.append(body)
        if outcome == "exception":
            raise RuntimeError("worker terminated during mutation")
        receipt = graph_build_receipt(full=body.full, embed=False)
        receipt["errors"] = ["src/broken.py: parse failed"]
        return receipt

    async def fake_mark_ready(project, generation):
        marked.append((project, generation))
        state["graph_requires_full_rebuild"] = False

    async def fail_proxy(*_args, **_kwargs):
        raise AssertionError("held generation reached a graph query worker")

    monkeypatch.setattr(module, "require_registered_project", fake_registered)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)
    monkeypatch.setattr(module, "hold_graph_generation_for_mutation", fake_hold)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)
    monkeypatch.setattr(module, "mark_graph_generation_ready", fake_mark_ready)
    monkeypatch.setattr(module, "proxy_worker_json", fail_proxy)

    body = module.GraphBuildRequest(
        repo="kaidera-os",
        full=build_mode == "full",
        sync=True,
        embed=False,
    )
    expected_error = RuntimeError if outcome == "exception" else module.HTTPException
    with pytest.raises(expected_error):
        await module.graph_build_proxy(body, x_project="kaidera-os")

    assert events == ["held", "worker"]
    assert len(worker_calls) == 1
    assert marked == []
    assert state["graph_requires_full_rebuild"] is True

    with pytest.raises(module.HTTPException) as stats_error:
        await module.graph_stats_proxy(x_project="kaidera-os")
    assert stats_error.value.status_code == 409
    with pytest.raises(module.HTTPException) as retry_error:
        await module.graph_build_proxy(
            module.GraphBuildRequest(repo="kaidera-os", embed=False),
            x_project="kaidera-os",
        )
    assert retry_error.value.status_code == 409
    assert len(worker_calls) == 1


@pytest.mark.asyncio
async def test_exact_incremental_receipt_reopens_same_held_generation(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_incremental_hold_success_test")
    state = graph_project_row("kaidera-os", repo_root="/projects/kaidera-os")
    original_generation = state["graph_generation"]
    storage_key = module.graph_storage_key("kaidera-os", state)
    events = []

    async def fake_registered(_project):
        return dict(state)

    @asynccontextmanager
    async def fake_lease():
        yield

    async def fake_hold(_project, generation):
        assert generation == original_generation
        events.append("held")
        state["graph_requires_full_rebuild"] = True

    async def fake_execute(body, **scope):
        assert body.full is False
        assert scope["storage_key"] == storage_key
        assert state["graph_requires_full_rebuild"] is True
        events.append("worker")
        return graph_build_receipt(full=False, embed=False)

    async def fake_mark_ready(_project, generation):
        assert generation == original_generation
        assert state["graph_requires_full_rebuild"] is True
        events.append("ready")
        state["graph_requires_full_rebuild"] = False

    async def fake_proxy(_worker_url, path, **kwargs):
        assert path == "/stats"
        assert kwargs["params"]["storage_key"] == storage_key
        events.append("stats")
        return {
            "total_nodes": 10,
            "total_edges": 2,
            "repos": [
                {
                    "name": storage_key,
                    "nodes": 10,
                    "edges": 2,
                    "path": f"/graphs/{storage_key}/graph.db",
                }
            ],
        }

    monkeypatch.setattr(module, "require_registered_project", fake_registered)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)
    monkeypatch.setattr(module, "hold_graph_generation_for_mutation", fake_hold)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)
    monkeypatch.setattr(module, "mark_graph_generation_ready", fake_mark_ready)
    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)

    result = await module.graph_build_proxy(
        module.GraphBuildRequest(
            repo="kaidera-os",
            sync=True,
            embed=False,
        ),
        x_project="kaidera-os",
    )
    assert result == graph_build_receipt(full=False, embed=False)
    assert state["graph_generation"] == original_generation
    assert state["graph_requires_full_rebuild"] is False

    await module.graph_stats_proxy(x_project="kaidera-os")
    assert events == ["held", "worker", "ready", "stats"]


@pytest.mark.asyncio
async def test_queued_build_for_old_root_cannot_unlock_rotated_generation(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_queued_root_rotation_test")
    state = graph_project_row(
        "kaidera-os",
        repo_root="/projects/root-b",
        generation_label="root-b",
        requires_full_rebuild=True,
    )
    updates = []
    worker_calls = []
    marked = []

    async def fake_registered(project):
        assert project == "kaidera-os"
        return dict(state)

    @asynccontextmanager
    async def fake_lease():
        yield

    async def fake_execute(*args, **kwargs):
        worker_calls.append((args, kwargs))

    async def fake_persist(project, job_id, **kwargs):
        updates.append((project, job_id, kwargs))

    async def fake_mark_ready(project, generation):
        marked.append((project, generation))

    monkeypatch.setattr(module, "require_registered_project", fake_registered)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)
    monkeypatch.setattr(module, "persist_graph_build_job", fake_persist)
    monkeypatch.setattr(module, "mark_graph_generation_ready", fake_mark_ready)

    # This body was scoped and queued while root-a was registered.  The worker
    # starts only after the project moves to root-b and rotates its generation.
    await module.run_graph_build_job(
        "kaidera-os",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        module.GraphBuildRequest(repo="/projects/root-a", full=True, embed=False),
    )

    assert updates[0][2]["status"] == "running"
    assert updates[-1][2]["status"] == "failed"
    assert "must exactly match" in updates[-1][2]["error"]
    assert worker_calls == []
    assert marked == []
    assert state["graph_requires_full_rebuild"] is True


@pytest.mark.asyncio
async def test_run_graph_build_job_records_completed_result(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_runner_test")
    allow_registered_project(module, monkeypatch)
    updates = []

    async def fake_update(project, job_id, **kwargs):
        updates.append((project, job_id, kwargs))

    async def fake_execute(body, **_scope):
        assert body.repo == "/projects/kaidera-os"
        return graph_build_receipt(full=False, embed=True)

    monkeypatch.setattr(module, "update_graph_build_job", fake_update)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    await module.run_graph_build_job(
        "kaidera-os",
        "55555555-5555-5555-5555-555555555555",
        module.GraphBuildRequest(repo="kaidera-os"),
    )

    assert updates[0][2] == {"status": "running", "result": None, "error": None}
    assert updates[1][2] == {
        "status": "completed",
        "result": graph_build_receipt(full=False, embed=True),
        "error": None,
    }


@pytest.mark.asyncio
async def test_run_graph_build_job_records_failure(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_failure_test")
    allow_registered_project(module, monkeypatch)
    updates = []

    async def fake_update(project, job_id, **kwargs):
        updates.append((project, job_id, kwargs))

    async def fake_execute(_body, **_scope):
        raise RuntimeError("worker timed out")

    monkeypatch.setattr(module, "update_graph_build_job", fake_update)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    await module.run_graph_build_job(
        "kaidera-os",
        "66666666-6666-6666-6666-666666666666",
        module.GraphBuildRequest(repo="kaidera-os"),
    )

    assert updates[0][2] == {"status": "running", "result": None, "error": None}
    assert updates[1][2]["status"] == "failed"
    assert updates[1][2]["result"] is None
    assert "RuntimeError: worker timed out" in updates[1][2]["error"]


@pytest.mark.asyncio
async def test_graph_registry_lease_holds_exact_project_root_lock_until_exit(
    monkeypatch,
):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_registry_lease_test")
    events = []

    class Conn:
        async def fetchval(self, sql, key):
            assert key == "cortex:project-root-registry"
            if "pg_advisory_unlock" in sql:
                events.append("unlocked")
                return True
            assert "pg_advisory_lock" in sql
            events.append("locked")
            return None

        def terminate(self):
            events.append("terminated")

    conn = Conn()
    monkeypatch.setattr(module, "pool_admin", FakePool(conn))

    async with module.graph_registry_lease() as leased_conn:
        assert leased_conn is conn
        events.append("worker-request")

    assert events == ["locked", "worker-request", "unlocked"]


@pytest.mark.asyncio
async def test_four_graph_requests_cannot_exhaust_pool_behind_registry_lease(
    monkeypatch,
):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_registry_pool_deadlock_test")
    state = graph_project_row("kaidera-os", repo_root="/projects/kaidera-os")
    registry_lock = asyncio.Lock()
    all_lock_attempts = asyncio.Event()
    lock_attempts = 0
    worker_calls = []

    class Conn:
        def __init__(self, name):
            self.name = name
            self.owns_registry_lock = False

        async def fetchrow(self, sql, project):
            assert "FROM cortex_projects" in sql
            assert project == "kaidera-os"
            assert self.owns_registry_lock is True
            return dict(state)

        async def fetchval(self, sql, *args):
            nonlocal lock_attempts
            if "pg_advisory_unlock" in sql:
                assert self.owns_registry_lock is True
                self.owns_registry_lock = False
                registry_lock.release()
                return True
            if "pg_advisory_lock" in sql:
                lock_attempts += 1
                if lock_attempts == 4:
                    all_lock_attempts.set()
                await registry_lock.acquire()
                self.owns_registry_lock = True
                return None
            assert "UPDATE cortex_projects" in sql
            assert self.owns_registry_lock is True
            project, generation, held = args
            assert project == "kaidera-os"
            assert generation == state["graph_generation"]
            state["graph_requires_full_rebuild"] = held
            return generation

        def terminate(self):
            if self.owns_registry_lock:
                self.owns_registry_lock = False
                registry_lock.release()

    class Acquire:
        def __init__(self, fake_pool):
            self.fake_pool = fake_pool
            self.conn = None

        async def __aenter__(self):
            self.conn = await self.fake_pool.available.get()
            self.fake_pool.in_use += 1
            self.fake_pool.max_in_use = max(
                self.fake_pool.max_in_use,
                self.fake_pool.in_use,
            )
            self.fake_pool.acquire_count += 1
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            self.fake_pool.in_use -= 1
            self.fake_pool.available.put_nowait(self.conn)
            return False

    class BoundedPool:
        def __init__(self):
            self.available = asyncio.Queue(maxsize=4)
            for index in range(4):
                self.available.put_nowait(Conn(f"conn-{index}"))
            self.in_use = 0
            self.max_in_use = 0
            self.acquire_count = 0

        def acquire(self):
            return Acquire(self)

    bounded_pool = BoundedPool()

    async def fake_execute(body, **_scope):
        assert state["graph_requires_full_rebuild"] is True
        worker_calls.append(body.repo)
        if len(worker_calls) == 1:
            await asyncio.wait_for(all_lock_attempts.wait(), timeout=1)
        return graph_build_receipt(full=False, embed=False)

    monkeypatch.setattr(module, "pool_admin", bounded_pool)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                module.graph_build_proxy(
                    module.GraphBuildRequest(
                        repo="kaidera-os",
                        sync=True,
                        embed=False,
                    ),
                    x_project="kaidera-os",
                )
                for _index in range(4)
            )
        ),
        timeout=2,
    )

    assert results == [graph_build_receipt(full=False, embed=False)] * 4
    assert worker_calls == ["/projects/kaidera-os"] * 4
    assert lock_attempts == 4
    assert bounded_pool.max_in_use == 4
    assert bounded_pool.acquire_count == 4
    assert bounded_pool.in_use == 0
    assert state["graph_requires_full_rebuild"] is False


@pytest.mark.asyncio
async def test_recover_interrupted_graph_jobs_marks_only_inflight_failed(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_job_recovery_test")
    calls = []

    class Conn:
        async def execute(self, sql, *args):
            calls.append((sql, args))

    conn = Conn()

    async def fake_schema(actual):
        assert actual is conn
        calls.append(("schema", ()))

    monkeypatch.setattr(module, "pool_admin", FakePool(conn))
    monkeypatch.setattr(module, "_create_graph_build_jobs_schema", fake_schema)

    await module.recover_interrupted_graph_build_jobs()

    assert calls[0] == ("schema", ())
    recovery_sql = calls[1][0]
    assert "status IN ('queued', 'running')" in recovery_sql
    assert "status = 'failed'" in recovery_sql
    assert "retry the build" in recovery_sql
    assert "completed_at = NOW()" in recovery_sql


@pytest.mark.asyncio
async def test_schedule_graph_build_job_retains_task_until_terminal(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_job_retention_test")
    release = asyncio.Event()

    async def fake_runner(_project, _job_id, _body):
        await release.wait()

    monkeypatch.setattr(module, "run_graph_build_job", fake_runner)
    task = module.schedule_graph_build_job(
        "kaidera-os",
        "77777777-7777-4777-8777-777777777777",
        module.GraphBuildRequest(repo="kaidera-os"),
    )
    await asyncio.sleep(0)
    assert task in module._GRAPH_BUILD_TASKS

    release.set()
    await task
    await asyncio.sleep(0)
    assert task not in module._GRAPH_BUILD_TASKS


@pytest.mark.asyncio
async def test_persist_graph_build_job_retries_transient_ledger_errors(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_job_retry_test")
    attempts = []
    delays = []

    async def flaky_update(project, job_id, **kwargs):
        attempts.append((project, job_id, kwargs))
        if len(attempts) < 3:
            raise ConnectionError("transient ledger outage")

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module, "update_graph_build_job", flaky_update)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    await module.persist_graph_build_job(
        "kaidera-os",
        "88888888-8888-4888-8888-888888888888",
        status="completed",
        result={"nodes": 4},
    )

    assert len(attempts) == 3
    assert delays == [0.1, 0.5]
    assert attempts[-1][2] == {
        "status": "completed",
        "result": {"nodes": 4},
        "error": None,
    }


@pytest.mark.asyncio
async def test_graph_prune_preserves_every_non_deleted_project_and_keep_override(
    monkeypatch,
):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_prune_test")
    module.ADMIN_TOKEN = "cortex-local-admin"
    rows = [
        graph_project_row("kaidera-os", repo_root="kaidera-os"),
        graph_project_row("dxb", repo_root="dxb"),
        graph_project_row("paused", repo_root="paused"),
        graph_project_row("archived", repo_root="archived"),
        graph_project_row("manual-keep", repo_root="deleted/manual-keep"),
    ]
    expected_storage_keys = sorted(
        module.graph_storage_key(row["project_key"], row) for row in rows
    )

    class Conn:
        async def fetch(self, sql, *args):
            assert "FROM cortex_projects" in sql
            assert "COALESCE(status, 'active') <> 'deleted'" in sql
            assert args == (["manual-keep"],)
            return rows

    async def fake_proxy(worker_url, path, *, method="GET", payload=None, timeout=120.0):
        assert worker_url == module.GRAPH_WORKER_URL
        assert path == "/prune"
        assert method == "POST"
        assert timeout == 30.0
        assert payload == {
            "active_projects": expected_storage_keys,
            "dry_run": True,
        }
        return {"dry_run": True, "candidates": []}

    @asynccontextmanager
    async def fake_lease():
        yield

    monkeypatch.setattr(module, "pool_admin", FakePool(Conn()))
    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)

    result = await module.graph_prune_proxy(
        module.GraphPruneRequest(dry_run=True, keep_projects=["manual-keep"]),
        admin_request(),
    )

    assert result == {"dry_run": True, "candidates": []}


@pytest.mark.asyncio
async def test_graph_prune_uses_project_key_not_reclaimable_repo_basename(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_prune_legacy_key_test")
    module.ADMIN_TOKEN = "cortex-local-admin"
    row = graph_project_row(
        "marketing", repo_root="/projects/Marketing Workspace"
    )
    storage_key = module.graph_storage_key("marketing", row)

    class Conn:
        async def fetch(self, _sql, *_args):
            return [row]

    async def fake_proxy(_worker_url, _path, **kwargs):
        assert kwargs["payload"]["active_projects"] == [storage_key]
        return {"dry_run": False, "pruned": []}

    @asynccontextmanager
    async def fake_lease():
        yield

    monkeypatch.setattr(module, "pool_admin", FakePool(Conn()))
    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)
    result = await module.graph_prune_proxy(
        module.GraphPruneRequest(dry_run=False), admin_request()
    )

    assert result == {"dry_run": False, "pruned": []}


@pytest.mark.asyncio
async def test_graph_prune_keeps_distinct_projects_with_same_repo_basename(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_prune_collision_test")
    module.ADMIN_TOKEN = "cortex-local-admin"
    rows = [
        graph_project_row("alpha", repo_root="/projects/Shared"),
        graph_project_row("bravo", repo_root="/archive/shared"),
    ]
    expected_storage_keys = sorted(
        module.graph_storage_key(row["project_key"], row) for row in rows
    )

    class Conn:
        async def fetch(self, _sql, *_args):
            return rows

    async def fake_proxy(*_args, **kwargs):
        assert kwargs["payload"]["active_projects"] == expected_storage_keys
        return {"dry_run": False, "pruned": []}

    @asynccontextmanager
    async def fake_lease():
        yield

    monkeypatch.setattr(module, "pool_admin", FakePool(Conn()))
    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)
    monkeypatch.setattr(module, "graph_registry_lease", fake_lease)

    assert await module.graph_prune_proxy(
        module.GraphPruneRequest(dry_run=False), admin_request()
    ) == {"dry_run": False, "pruned": []}
