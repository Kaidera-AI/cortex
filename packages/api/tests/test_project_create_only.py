"""Create-only project registration is retry-safe and preserves operator state."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


API_MAIN = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location("cortex_api_project_create_only_test", API_MAIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(API_MAIN.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(API_MAIN.parent))
    return module


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


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


class ProjectStateConn:
    """Minimal state machine for the create-only SQL contract."""

    def __init__(self):
        self.project: dict | None = None
        self.roots: list[dict] = []
        self.agents: list[dict] = []
        self.project_inserts = 0
        self.root_inserts = 0
        self.agent_inserts = 0
        self.role_writes = 0

    def transaction(self):
        return FakeTransaction()

    async def fetchrow(self, sql, *args):
        if sql.lstrip().startswith("SELECT repo_root, metadata FROM cortex_projects"):
            return copy.deepcopy(self.project)
        if "FROM cortex_projects" in sql and "FOR UPDATE" in sql:
            assert "default_agent" in sql.split("FROM cortex_projects", 1)[0], (
                "the exact-replay query must select every field it compares"
            )
            return copy.deepcopy(self.project)
        if "SELECT cp.project_key" in sql:
            return None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        if "UNION ALL" in sql and "cp.repo_root AS root_path" in sql:
            if self.project is None:
                return []
            key = self.project["project_key"]
            rows = [
                {
                    "project_key": key,
                    "status": self.project.get("status", "active"),
                    "root_path": self.project["repo_root"],
                }
            ]
            rows.extend(
                {
                    "project_key": key,
                    "status": self.project.get("status", "active"),
                    "root_path": root["root_path"],
                }
                for root in self.roots
            )
            return rows
        if "FROM cortex_project_paths" in sql:
            return copy.deepcopy(self.roots)
        if "FROM agents" in sql:
            return copy.deepcopy(self.agents)
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            return None
        if "INSERT INTO cortex_projects" in sql:
            if self.project is not None:
                return None
            assert "graph_requires_full_rebuild" in sql
            assert "$8::jsonb, TRUE" in sql
            self.project_inserts += 1
            self.project = {
                "project_key": args[0],
                "project_id": "00000000-0000-0000-0000-000000000001",
                "graph_generation": str(uuid4()),
                "graph_requires_full_rebuild": True,
                "display_name": args[1],
                "parent_project_key": args[2],
                "repo_root": args[3],
                "repo_type": args[4],
                "status": args[5],
                "default_agent": args[6],
                "metadata": json.loads(args[7]),
            }
            return self.project["project_id"]
        if "INSERT INTO cortex_project_paths" in sql:
            if any(root["root_path"] == args[1] for root in self.roots):
                return None
            self.root_inserts += 1
            self.roots.append(
                {
                    "root_path": args[1],
                    "path_kind": args[2],
                    "metadata": json.loads(args[3]),
                }
            )
            return args[1]
        if "INSERT INTO agents" in sql:
            if any(agent["name"] == args[0] for agent in self.agents):
                return None
            self.agent_inserts += 1
            self.agents.append(
                {
                    "name": args[0],
                    "role": args[2],
                    "model": args[3],
                    "capabilities": json.loads(args[4]),
                }
            )
            return args[0]
        if "SELECT id::text FROM cortex_projects" in sql:
            return self.project["project_id"] if self.project else None
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def execute(self, sql, *args):
        if "INSERT INTO roles" in sql:
            self.role_writes += 1
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")


@pytest.fixture
def api_and_state(monkeypatch):
    api = load_api_module()
    conn = ProjectStateConn()
    api.pool_admin = FakePool(conn)
    monkeypatch.setattr(api, "require_admin_access", lambda _request: None)
    monkeypatch.setattr(api, "_invalidate_roster_policy", lambda *_args: None)
    events: list[str] = []

    async def fake_event(*_args, **_kwargs):
        events.append("project_registered")

    monkeypatch.setattr(api, "emit_team_event", fake_event)
    return api, conn, events


def project_body(api):
    return api.ProjectRegister(
        project_key="dev-os",
        display_name="Dev OS",
        repo_root="/projects/dev-os",
        roots=[{"path": "/projects/dev-os", "kind": "primary"}],
        default_agent="keith",
        agents=[
            {
                "name": "keith",
                "role": "generalist",
                "model": "reviewed-model",
                "capabilities": {"writer_scope": "work"},
            }
        ],
        metadata={"pack": "dev-os"},
    )


def create_only_request():
    return SimpleNamespace(headers={"X-Cortex-Project-Mode": "create-only"})


@pytest.mark.asyncio
async def test_lost_create_response_retry_is_exactly_read_only(api_and_state):
    api, conn, events = api_and_state
    body = project_body(api)

    # Treat the first successful response as lost. The client retries the same POST.
    first = await api.register_project(body, create_only_request())
    counters = (conn.project_inserts, conn.root_inserts, conn.agent_inserts, conn.role_writes)
    second = await api.register_project(body, create_only_request())

    assert first["registration_status"] == "created"
    assert second["registration_status"] == "unchanged"
    assert second["project_id"] == first["project_id"]
    assert conn.project["graph_requires_full_rebuild"] is True
    assert (conn.project_inserts, conn.root_inserts, conn.agent_inserts, conn.role_writes) == counters
    assert events == ["project_registered"]


@pytest.mark.asyncio
@pytest.mark.parametrize("customized_part", ["project", "root", "agent"])
async def test_concurrent_customization_conflicts_without_mutation(
    api_and_state,
    customized_part,
):
    api, conn, events = api_and_state
    body = project_body(api)
    await api.register_project(body, create_only_request())

    if customized_part == "project":
        conn.project["display_name"] = "Operator Custom Name"
        conn.project["default_agent"] = "operator-agent"
        conn.project["metadata"] = {"operator": True}
    elif customized_part == "root":
        conn.roots[0]["metadata"] = {"path": "/projects/dev-os", "kind": "primary", "operator": True}
    else:
        conn.agents[0]["capabilities"] = {"writer_scope": "read-only", "keep_visible": True}

    before = copy.deepcopy((conn.project, conn.roots, conn.agents))
    counters = (conn.project_inserts, conn.root_inserts, conn.agent_inserts, conn.role_writes)
    with pytest.raises(HTTPException) as exc:
        await api.register_project(body, create_only_request())

    assert exc.value.status_code == 409
    assert (conn.project, conn.roots, conn.agents) == before
    assert (conn.project_inserts, conn.root_inserts, conn.agent_inserts, conn.role_writes) == counters
    assert events == ["project_registered"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "value"),
    [("root_path", "/projects/drifted"), ("path_kind", "secondary")],
)
async def test_create_only_rejects_root_metadata_that_masks_column_drift(
    api_and_state, column, value
):
    api, conn, events = api_and_state
    body = project_body(api)
    await api.register_project(body, create_only_request())
    conn.roots[0][column] = value
    before = copy.deepcopy((conn.project, conn.roots, conn.agents))

    with pytest.raises(HTTPException) as exc:
        await api.register_project(body, create_only_request())
    assert exc.value.status_code == 409
    assert "metadata disagrees" in exc.value.detail
    assert (conn.project, conn.roots, conn.agents) == before
    assert events == ["project_registered"]


def test_runtime_root_projection_never_prefers_stale_project_metadata():
    api = load_api_module()
    project = {
        "project_key": "dev-os",
        "repo_root": "/projects/good",
        "metadata": {
            "roots": [{"path": "/projects/stale", "kind": "primary"}]
        },
    }
    rows = [
        {
            "root_path": "/projects/good",
            "path_kind": "primary",
            "metadata": {"path": "/projects/good", "kind": "primary"},
        }
    ]
    with pytest.raises(HTTPException) as exc:
        api.build_runtime_profile(project, rows, [])
    assert exc.value.status_code == 409
    assert "metadata roots disagree" in exc.value.detail


def test_runtime_profile_rejects_stale_persisted_beat_target():
    api = load_api_module()
    project = {
        "project_key": "dev-os",
        "repo_root": "/projects/dev-os",
        "metadata": {
            "roots": [{"path": "/projects/dev-os", "kind": "primary"}],
            "beat": {"orchestrator_agent": "ghost"},
        },
    }
    roots = [
        {
            "root_path": "/projects/dev-os",
            "path_kind": "primary",
            "metadata": {"path": "/projects/dev-os", "kind": "primary"},
        }
    ]
    agents = [
        {
            "name": "keith",
            "role": "generalist",
            "model": "reviewed-model",
            "capabilities": {"writer_scope": "work"},
        }
    ]

    with pytest.raises(HTTPException) as exc:
        api.build_runtime_profile(project, roots, agents)

    assert exc.value.status_code == 409
    assert "beat" in str(exc.value.detail).lower()
    assert "repair" in str(exc.value.detail).lower()


def test_runtime_profile_rejects_dual_persisted_beat_authorities():
    api = load_api_module()
    project = {
        "project_key": "dev-os",
        "repo_root": "/projects/dev-os",
        "metadata": {
            "roots": [{"path": "/projects/dev-os", "kind": "primary"}],
            "beat": {
                "orchestrator_agent": "keith",
                "agent": "keith",
            },
        },
    }
    roots = [
        {
            "root_path": "/projects/dev-os",
            "path_kind": "primary",
            "metadata": {"path": "/projects/dev-os", "kind": "primary"},
        }
    ]
    agents = [
        {
            "name": "keith",
            "role": "generalist",
            "model": "reviewed-model",
            "capabilities": {"writer_scope": "work"},
        }
    ]

    with pytest.raises(HTTPException) as exc:
        api.build_runtime_profile(project, roots, agents)

    assert exc.value.status_code == 409
    assert "one beat identity field" in str(exc.value.detail).lower()


def test_every_project_root_consumer_uses_one_exact_projection_helper():
    source = API_MAIN.read_text(encoding="utf-8")
    blocks = {
        "runtime": source[source.index("def build_runtime_profile(") : source.index("def parse_identity(")],
        "list": source[source.index("async def list_projects(") : source.index("async def get_project(")],
        "get": source[source.index("async def get_project(") : source.index("async def export_project(")],
        "export": source[source.index("async def export_project(") : source.index("async def import_project(")],
    }
    for name, block in blocks.items():
        assert "exact_project_roots(" in block, name


class ProjectionConsumerConn:
    def __init__(self, project, roots):
        self.project = project
        self.roots = roots

    async def fetchrow(self, sql, *args):
        if "FROM cortex_projects" in sql:
            return copy.deepcopy(self.project)
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        if "FROM cortex_projects p" in sql:
            return [copy.deepcopy(self.project)]
        if "FROM cortex_project_paths" in sql:
            return copy.deepcopy(self.roots)
        raise AssertionError(f"unexpected fetch SQL: {sql}")


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["runtime", "list", "get", "export"])
@pytest.mark.parametrize("drift", ["missing-path-row", "missing-metadata", "malformed-metadata"])
async def test_project_root_consumers_fail_closed_on_incomplete_registry(
    monkeypatch, consumer, drift
):
    api = load_api_module()
    project = {
        "project_key": "dev-os",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "display_name": "Dev OS",
        "default_agent": "keith",
        "parent_project_key": None,
        "repo_root": "/projects/dev-os",
        "repo_type": "local",
        "status": "active",
        "metadata": {"roots": [{"path": "/projects/dev-os", "kind": "primary"}]},
        "created_at": None,
        "updated_at": None,
        "agent_count": 0,
        "profile_count": 0,
    }
    roots = [
        {
            "project_key": "dev-os",
            "root_path": "/projects/dev-os",
            "path_kind": "primary",
            "metadata": {"path": "/projects/dev-os", "kind": "primary"},
        }
    ]
    if drift == "missing-path-row":
        roots = []
    elif drift == "missing-metadata":
        project["metadata"] = {}
    else:
        project["metadata"] = {"roots": ["not-an-object"]}

    conn = ProjectionConsumerConn(project, roots)
    api.pool_admin = FakePool(conn)
    monkeypatch.setattr(api, "require_admin_access", lambda _request: None)

    with pytest.raises(HTTPException) as exc:
        if consumer == "runtime":
            api.build_runtime_profile(project, roots, [])
        elif consumer == "list":
            await api.list_projects()
        elif consumer == "get":
            await api.get_project("dev-os")
        else:
            await api.export_project("dev-os", SimpleNamespace())
    assert exc.value.status_code == 409
    assert "repair" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_unknown_registration_mode_is_rejected_before_storage(api_and_state):
    api, conn, _events = api_and_state
    request = SimpleNamespace(headers={"X-Cortex-Project-Mode": "overwrite-if-convenient"})
    with pytest.raises(HTTPException) as exc:
        await api.register_project(project_body(api), request)
    assert exc.value.status_code == 400
    assert conn.project is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "roots",
    [
        [{"path": "/projects/dev-os", "kind": "secondary"}],
        [
            {"path": "/projects/dev-os", "kind": "primary"},
            {"path": "/projects/other", "kind": "primary"},
        ],
        [{"path": "/projects/other", "kind": "primary"}],
    ],
)
async def test_repo_root_requires_one_matching_primary_before_storage(api_and_state, roots):
    api, conn, _events = api_and_state
    body = api.ProjectRegister(
        project_key="dev-os",
        repo_root="/projects/dev-os",
        roots=roots,
    )
    with pytest.raises(HTTPException) as exc:
        await api.register_project(body, create_only_request())
    assert exc.value.status_code == 400
    assert conn.project is None and not conn.roots


@pytest.mark.asyncio
async def test_reserved_root_and_default_metadata_are_server_owned(api_and_state):
    api, conn, _events = api_and_state
    body = project_body(api)
    body.metadata = {
        "pack": "dev-os",
        "roots": [{"path": "/attacker", "kind": "primary"}],
        "default_agent": "attacker",
    }
    result = await api.register_project(body, create_only_request())
    assert result["roots"] == [{"path": "/projects/dev-os", "kind": "primary"}]
    assert conn.project["metadata"]["roots"] == result["roots"]
    assert conn.project["metadata"]["default_agent"] == "keith"


@pytest.mark.asyncio
async def test_project_registration_rejects_beat_target_absent_from_roster(
    api_and_state,
):
    api, conn, _events = api_and_state
    body = project_body(api)
    body.metadata = {"beat": {"orchestrator_agent": "ghost"}}

    with pytest.raises(HTTPException) as exc:
        await api.register_project(body, create_only_request())

    assert exc.value.status_code == 400
    assert "beat.orchestrator_agent" in str(exc.value.detail)
    assert conn.project is None and not conn.roots and not conn.agents


@pytest.mark.asyncio
async def test_project_registration_rejects_dual_beat_authorities(api_and_state):
    api, conn, _events = api_and_state
    body = project_body(api)
    body.metadata = {
        "beat": {
            "orchestrator_agent": "keith",
            "agent": "keith",
        }
    }

    with pytest.raises(HTTPException) as exc:
        await api.register_project(body, create_only_request())

    assert exc.value.status_code == 400
    assert "one Beat identity field" in str(exc.value.detail)
    assert conn.project is None and not conn.roots and not conn.agents


@pytest.mark.asyncio
async def test_project_registration_normalizes_legacy_beat_agent_to_canonical_field(
    api_and_state,
):
    api, conn, _events = api_and_state
    body = project_body(api)
    body.metadata = {"beat": {"agent": "keith", "cadence_minutes": 25}}

    await api.register_project(body, create_only_request())

    assert conn.project["metadata"]["beat"] == {
        "orchestrator_agent": "keith",
        "cadence_minutes": 25,
    }


@pytest.mark.asyncio
async def test_legacy_post_project_key_migration_flag_is_fail_closed(api_and_state):
    api, conn, _events = api_and_state
    body = project_body(api)
    body.metadata = {"allow_project_key_migration": True}
    with pytest.raises(HTTPException) as exc:
        await api.register_project(body, SimpleNamespace(headers={}))
    assert exc.value.status_code == 409
    assert "explicit migration workflow" in exc.value.detail
    assert conn.project is None and not conn.roots


@pytest.mark.asyncio
async def test_create_only_and_upsert_serialize_on_one_root_without_reassignment(monkeypatch):
    api = load_api_module()

    class Store:
        def __init__(self):
            self.projects = {}
            self.roots = {}
            self.locks = {}

    store = Store()

    class Transaction:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            for lock in reversed(self.conn.held):
                lock.release()
            self.conn.held.clear()
            return False

    class Conn:
        def __init__(self):
            self.held = []

        def transaction(self):
            return Transaction(self)

        async def fetchval(self, sql, *args):
            if "pg_advisory_xact_lock" in sql:
                lock = store.locks.setdefault(args[0], asyncio.Lock())
                await lock.acquire()
                self.held.append(lock)
                await asyncio.sleep(0)
                return None
            if "INSERT INTO cortex_projects" in sql:
                key = args[0]
                if key in store.projects:
                    return None
                assert "graph_requires_full_rebuild" in sql
                assert "$8::jsonb, TRUE" in sql
                project_id = f"00000000-0000-0000-0000-{len(store.projects) + 1:012d}"
                store.projects[key] = {
                    "project_id": project_id,
                    "graph_generation": str(uuid4()),
                    "graph_requires_full_rebuild": True,
                    "display_name": args[1],
                    "parent_project_key": args[2],
                    "repo_root": args[3],
                    "repo_type": args[4],
                    "status": args[5],
                    "default_agent": args[6],
                    "metadata": json.loads(args[7]),
                }
                return project_id
            if "INSERT INTO cortex_project_paths" in sql:
                key, path, kind, raw_metadata = args
                if path in store.roots:
                    return None
                store.roots[path] = {
                    "project_key": key,
                    "root_path": path,
                    "path_kind": kind,
                    "metadata": json.loads(raw_metadata),
                }
                return path
            if "SELECT id::text FROM cortex_projects" in sql:
                project = store.projects.get(args[0])
                return project["project_id"] if project else None
            raise AssertionError(sql)

        async def fetchrow(self, sql, *args):
            if "SELECT cp.project_key" in sql:
                key, paths = args
                for path in paths:
                    root = store.roots.get(path)
                    if root and root["project_key"] != key:
                        return {"project_key": root["project_key"]}
                    for other_key, project in store.projects.items():
                        if other_key != key and project["repo_root"] == path:
                            return {"project_key": other_key}
                return None
            if "FROM cortex_projects" in sql and "FOR UPDATE" in sql:
                return copy.deepcopy(store.projects.get(args[0]))
            raise AssertionError(sql)

        async def fetch(self, sql, *args):
            if "UNION ALL" in sql and "cp.repo_root AS root_path" in sql:
                rows = [
                    {
                        "project_key": key,
                        "status": project.get("status", "active"),
                        "root_path": project["repo_root"],
                    }
                    for key, project in store.projects.items()
                ]
                rows.extend(
                    {
                        "project_key": root["project_key"],
                        "status": store.projects[root["project_key"]].get("status", "active"),
                        "root_path": root["root_path"],
                    }
                    for root in store.roots.values()
                )
                return rows
            if "FROM cortex_project_paths" in sql:
                return [
                    copy.deepcopy(root)
                    for root in store.roots.values()
                    if root["project_key"] == args[0]
                ]
            if "FROM agents" in sql:
                return []
            raise AssertionError(sql)

        async def execute(self, sql, *args):
            if "INSERT INTO cortex_projects" in sql:
                key = args[0]
                assert "graph_requires_full_rebuild" in sql
                assert "$8::jsonb, TRUE" in sql
                project_id = store.projects.get(key, {}).get(
                    "project_id", f"00000000-0000-0000-0000-{len(store.projects) + 1:012d}"
                )
                store.projects[key] = {
                    "project_id": project_id,
                    "graph_generation": store.projects.get(key, {}).get(
                        "graph_generation", str(uuid4())
                    ),
                    "graph_requires_full_rebuild": store.projects.get(key, {}).get(
                        "graph_requires_full_rebuild", True
                    ),
                    "display_name": args[1],
                    "parent_project_key": args[2],
                    "repo_root": args[3],
                    "repo_type": args[4],
                    "status": args[5],
                    "default_agent": args[6],
                    "metadata": json.loads(args[7]),
                }
                return "INSERT 0 1"
            if "INSERT INTO cortex_project_paths" in sql:
                key, path, kind, raw_metadata = args
                existing = store.roots.get(path)
                if existing is None or existing["project_key"] == key:
                    store.roots[path] = {
                        "project_key": key,
                        "root_path": path,
                        "path_kind": kind,
                        "metadata": json.loads(raw_metadata),
                    }
                return "INSERT 0 1"
            if "DELETE FROM cortex_project_paths" in sql:
                key, retained = args
                for path in list(store.roots):
                    if store.roots[path]["project_key"] == key and path not in retained:
                        del store.roots[path]
                return "DELETE 1"
            raise AssertionError(sql)

    class Pool:
        def acquire(self):
            return FakeAcquire(Conn())

    api.pool_admin = Pool()
    monkeypatch.setattr(api, "require_admin_access", lambda _request: None)
    monkeypatch.setattr(api, "_invalidate_roster_policy", lambda *_args: None)

    async def no_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(api, "emit_team_event", no_event)

    def body(key):
        return api.ProjectRegister(
            project_key=key,
            display_name=key,
            repo_root="/projects/shared-root",
            roots=[{"path": "/projects/shared-root", "kind": "primary"}],
            metadata={"owner": key},
        )

    results = await asyncio.gather(
        api.register_project(body("create-project"), create_only_request()),
        api.register_project(body("upsert-project"), SimpleNamespace(headers={})),
        return_exceptions=True,
    )
    successes = [result for result in results if isinstance(result, dict)]
    conflicts = [result for result in results if isinstance(result, HTTPException)]
    assert len(successes) == 1
    assert len(conflicts) == 1 and conflicts[0].status_code == 409
    winner = successes[0]["project_key"]
    assert store.roots["/projects/shared-root"]["project_key"] == winner
    assert store.projects[winner]["repo_root"] == "/projects/shared-root"
    assert len(store.projects) == 1


class RootAuthorityStore:
    def __init__(self):
        self.projects: dict[str, dict] = {}
        self.roots: dict[str, dict] = {}
        self.locks: dict[str, asyncio.Lock] = {}


class RootAuthorityTransaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        for lock in reversed(self.conn.held):
            lock.release()
        self.conn.held.clear()
        return False


class RootAuthorityConn:
    def __init__(self, store: RootAuthorityStore):
        self.store = store
        self.held: list[asyncio.Lock] = []

    def transaction(self):
        return RootAuthorityTransaction(self)

    async def fetchval(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            lock = self.store.locks.setdefault(args[0], asyncio.Lock())
            await lock.acquire()
            self.held.append(lock)
            await asyncio.sleep(0)
            return None
        if "INSERT INTO cortex_project_paths" in sql:
            key, path = args[:2]
            existing = self.store.roots.get(path)
            if existing is not None and existing["project_key"] != key:
                return None
            self.store.roots[path] = {
                "project_key": key,
                "root_path": path,
                "path_kind": "primary" if len(args) == 3 else args[2],
                "metadata": json.loads(args[-1]),
            }
            return path
        if "SELECT id::text FROM cortex_projects" in sql:
            project = self.store.projects.get(args[0])
            return project["project_id"] if project else None
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def fetchrow(self, sql, *args):
        if "SELECT cp.project_key" in sql:
            assert "<> 'deleted'" not in sql, "deleted root owners must remain reserved"
            key, paths = args
            for other_key, project in self.store.projects.items():
                if other_key != key and project.get("repo_root") in paths:
                    return {"project_key": other_key, "status": project.get("status", "active")}
            for path in paths:
                root = self.store.roots.get(path)
                if root and root["project_key"] != key:
                    owner = self.store.projects[root["project_key"]]
                    return {"project_key": root["project_key"], "status": owner.get("status", "active")}
            return None
        if "SELECT repo_root, metadata FROM cortex_projects" in sql:
            project = self.store.projects.get(args[0])
            if not project:
                return None
            return {
                "repo_root": project["repo_root"],
                "metadata": copy.deepcopy(project["metadata"]),
            }
        if "UPDATE cortex_projects" in sql and "RETURNING repo_root, metadata" in sql:
            key, path, metadata = args
            project = self.store.projects[key]
            assert "graph_generation = CASE" in sql
            assert "graph_requires_full_rebuild = CASE" in sql
            project.setdefault("graph_generation", str(uuid4()))
            project.setdefault("graph_requires_full_rebuild", False)
            if project["repo_root"] != path:
                project["graph_generation"] = str(uuid4())
                project["graph_requires_full_rebuild"] = True
            project["repo_root"] = path
            project["metadata"] = json.loads(metadata)
            return {
                "repo_root": path,
                "metadata": copy.deepcopy(project["metadata"]),
                "graph_generation": project["graph_generation"],
                "graph_requires_full_rebuild": project[
                    "graph_requires_full_rebuild"
                ],
            }
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        if "UNION ALL" in sql and "cp.repo_root AS root_path" in sql:
            rows = [
                {
                    "project_key": key,
                    "status": project.get("status", "active"),
                    "root_path": project["repo_root"],
                }
                for key, project in self.store.projects.items()
            ]
            rows.extend(
                {
                    "project_key": root["project_key"],
                    "status": self.store.projects[root["project_key"]].get(
                        "status", "active"
                    ),
                    "root_path": root["root_path"],
                }
                for root in self.store.roots.values()
            )
            return rows
        if "FROM cortex_project_paths" in sql:
            rows = sorted(
                (
                copy.deepcopy(root)
                for root in self.store.roots.values()
                if root["project_key"] == args[0]
                ),
                key=lambda root: root["root_path"],
            )
            if "path_kind" not in sql:
                return [{"root_path": row["root_path"]} for row in rows]
            return rows
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def execute(self, sql, *args):
        if "INSERT INTO cortex_projects" in sql:
            key = args[0]
            existing = self.store.projects.get(key)
            assert "graph_generation = CASE" in sql
            assert "graph_requires_full_rebuild = CASE" in sql
            assert "$8::jsonb, TRUE" in sql
            rotate_graph = bool(
                existing
                and (
                    existing["repo_root"] != args[3]
                    or (
                        existing.get("status") == "deleted"
                        and args[5] != "deleted"
                    )
                )
            )
            self.store.projects[key] = {
                "project_id": existing["project_id"] if existing else f"id-{key}",
                "graph_generation": (
                    str(uuid4())
                    if rotate_graph or not existing
                    else existing.get("graph_generation", str(uuid4()))
                ),
                "graph_requires_full_rebuild": (
                    True
                    if rotate_graph or not existing
                    else bool(
                        existing
                        and existing.get("graph_requires_full_rebuild", False)
                    )
                ),
                "display_name": args[1],
                "parent_project_key": args[2],
                "repo_root": args[3],
                "repo_type": args[4],
                "status": args[5],
                "default_agent": args[6],
                "metadata": json.loads(args[7]),
            }
            return "INSERT 0 1"
        if "UPDATE cortex_projects SET repo_root" in sql:
            key, path, metadata = args
            self.store.projects[key]["repo_root"] = path
            self.store.projects[key]["metadata"] = json.loads(metadata)
            return "UPDATE 1"
        if "DELETE FROM cortex_project_paths" in sql:
            key = args[0]
            if "root_path <> $2" in sql:
                target = args[1]
                for path in list(self.store.roots):
                    root = self.store.roots[path]
                    if root["project_key"] == key \
                            and root["path_kind"] == "primary" and path != target:
                        del self.store.roots[path]
                return "DELETE 1"
            retained = set(args[1]) if len(args) > 1 else None
            for path in list(self.store.roots):
                if self.store.roots[path]["project_key"] == key \
                        and (
                            (retained is None and self.store.roots[path]["path_kind"] == "primary")
                            or (retained is not None and path not in retained)
                        ):
                    del self.store.roots[path]
            return "DELETE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")


class RootAuthorityPool:
    def __init__(self, store):
        self.store = store

    def acquire(self):
        return FakeAcquire(RootAuthorityConn(self.store))


def configure_root_authority_api(api, store, monkeypatch):
    api.pool_admin = RootAuthorityPool(store)
    monkeypatch.setattr(api, "require_admin_access", lambda _request: None)
    monkeypatch.setattr(api, "_invalidate_roster_policy", lambda *_args: None)

    async def registered(key):
        return store.projects.get(key)

    async def no_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(api, "require_registered_project", registered)
    monkeypatch.setattr(api, "emit_team_event", no_event)


@pytest.mark.asyncio
async def test_deleted_project_root_remains_reserved_for_explicit_migration(monkeypatch):
    api = load_api_module()
    store = RootAuthorityStore()
    store.projects["deleted-owner"] = {
        "project_id": "id-deleted",
        "repo_root": "/projects/reserved",
        "status": "deleted",
        "metadata": {},
    }
    store.roots["/projects/reserved"] = {
        "project_key": "deleted-owner",
        "root_path": "/projects/reserved",
        "path_kind": "primary",
        "metadata": {},
    }
    configure_root_authority_api(api, store, monkeypatch)
    body = api.ProjectRegister(
        project_key="new-project",
        repo_root="/projects/reserved",
        roots=[{"path": "/projects/reserved", "kind": "primary"}],
    )
    with pytest.raises(HTTPException) as exc:
        await api.register_project(body, SimpleNamespace(headers={}))
    assert exc.value.status_code == 409
    assert "new-project" not in store.projects
    assert store.roots["/projects/reserved"]["project_key"] == "deleted-owner"


@pytest.mark.asyncio
async def test_patch_and_register_serialize_without_root_reassignment(monkeypatch):
    api = load_api_module()
    store = RootAuthorityStore()
    store.projects["project-a"] = {
        "project_id": "id-a",
        "repo_root": "/projects/a",
        "status": "active",
        "metadata": {"roots": [{"path": "/projects/a", "kind": "primary"}]},
    }
    store.roots["/projects/a"] = {
        "project_key": "project-a",
        "root_path": "/projects/a",
        "path_kind": "primary",
        "metadata": {"path": "/projects/a", "kind": "primary"},
    }
    configure_root_authority_api(api, store, monkeypatch)
    target = "/projects/shared"
    patch = api.patch_project(
        "project-a",
        api.ProjectPatch(repo_root=target),
        SimpleNamespace(headers={}),
    )
    register = api.register_project(
        api.ProjectRegister(
            project_key="project-b",
            repo_root=target,
            roots=[{"path": target, "kind": "primary"}],
        ),
        SimpleNamespace(headers={}),
    )
    results = await asyncio.gather(patch, register, return_exceptions=True)
    successes = [item for item in results if isinstance(item, dict)]
    conflicts = [item for item in results if isinstance(item, HTTPException)]
    assert len(successes) == 1
    assert len(conflicts) == 1 and conflicts[0].status_code == 409
    owner = store.roots[target]["project_key"]
    assert store.projects[owner]["repo_root"] == target
    loser = "project-b" if owner == "project-a" else "project-a"
    assert store.projects.get(loser, {}).get("repo_root") != target


@pytest.mark.asyncio
async def test_lexical_and_symlink_root_aliases_have_one_canonical_owner(tmp_path, monkeypatch):
    api = load_api_module()
    store = RootAuthorityStore()
    configure_root_authority_api(api, store, monkeypatch)
    shared = tmp_path / "shared"
    shared.mkdir()
    parent = tmp_path / "parent"
    parent.mkdir()
    lexical_alias = parent / ".." / "shared"
    symlink_alias = tmp_path / "shared-link"
    symlink_alias.symlink_to(shared, target_is_directory=True)

    async def register(key: str, path: Path):
        return await api.register_project(
            api.ProjectRegister(
                project_key=key,
                repo_root=str(path),
                roots=[{"path": str(path), "kind": "primary"}],
            ),
            SimpleNamespace(headers={}),
        )

    first = await register("canonical-owner", lexical_alias)
    assert first["roots"][0]["path"] == str(shared.resolve())
    for key, alias in (("lexical-rival", shared), ("symlink-rival", symlink_alias)):
        with pytest.raises(HTTPException) as exc:
            await register(key, alias)
        assert exc.value.status_code == 409
        assert key not in store.projects
    assert store.roots[str(shared.resolve())]["project_key"] == "canonical-owner"


@pytest.mark.asyncio
async def test_legacy_raw_root_alias_is_canonical_audited_before_new_registration(
    tmp_path, monkeypatch
):
    api = load_api_module()
    store = RootAuthorityStore()
    shared = tmp_path / "shared"
    shared.mkdir()
    parent = tmp_path / "parent"
    parent.mkdir()
    legacy_spelling = str(parent / ".." / "shared")
    store.projects["legacy-owner"] = {
        "project_id": "id-legacy",
        "repo_root": legacy_spelling,
        "status": "active",
        "metadata": {"roots": [{"path": legacy_spelling, "kind": "primary"}]},
    }
    store.roots[legacy_spelling] = {
        "project_key": "legacy-owner",
        "root_path": legacy_spelling,
        "path_kind": "primary",
        "metadata": {"path": legacy_spelling, "kind": "primary"},
    }
    configure_root_authority_api(api, store, monkeypatch)

    rival = api.ProjectRegister(
        project_key="rival",
        repo_root=str(shared.resolve()),
        roots=[{"path": str(shared.resolve()), "kind": "primary"}],
    )
    with pytest.raises(HTTPException) as exc:
        await api.register_project(rival, SimpleNamespace(headers={}))
    assert exc.value.status_code == 409
    assert "rival" not in store.projects
    assert store.roots[legacy_spelling]["project_key"] == "legacy-owner"


@pytest.mark.asyncio
async def test_ambiguous_legacy_alias_registry_holds_every_root_mutation(tmp_path, monkeypatch):
    api = load_api_module()
    store = RootAuthorityStore()
    shared = tmp_path / "shared"
    shared.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(shared, target_is_directory=True)
    for key, path in (("owner-a", str(shared)), ("owner-b", str(alias))):
        store.projects[key] = {
            "project_id": f"id-{key}",
            "repo_root": path,
            "status": "active",
            "metadata": {"roots": [{"path": path, "kind": "primary"}]},
        }
        store.roots[path] = {
            "project_key": key,
            "root_path": path,
            "path_kind": "primary",
            "metadata": {"path": path, "kind": "primary"},
        }
    configure_root_authority_api(api, store, monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await api.patch_project(
            "owner-a",
            api.ProjectPatch(repo_root=str(tmp_path / "new-root")),
            SimpleNamespace(headers={}),
        )
    assert exc.value.status_code == 409
    assert "ambiguous legacy aliases" in exc.value.detail
    assert store.projects["owner-a"]["repo_root"] == str(shared)


@pytest.mark.asyncio
async def test_same_project_upsert_atomically_replaces_declared_root_set(monkeypatch):
    api = load_api_module()
    store = RootAuthorityStore()
    configure_root_authority_api(api, store, monkeypatch)

    async def upsert(primary: str, roots: list[dict]):
        return await api.register_project(
            api.ProjectRegister(
                project_key="moving-project",
                repo_root=primary,
                roots=roots,
            ),
            SimpleNamespace(headers={}),
        )

    await upsert(
        "/projects/old",
        [
            {"path": "/projects/old", "kind": "primary"},
            {"path": "/projects/old-secondary", "kind": "secondary"},
        ],
    )
    assert store.projects["moving-project"]["graph_requires_full_rebuild"] is True
    generation_before_move = store.projects["moving-project"]["graph_generation"]
    result = await upsert(
        "/projects/new",
        [{"path": "/projects/new", "kind": "primary"}],
    )
    assert result["roots"] == [{"path": "/projects/new", "kind": "primary"}]
    assert set(store.roots) == {"/projects/new"}
    assert store.projects["moving-project"]["repo_root"] == "/projects/new"
    assert store.projects["moving-project"]["metadata"]["roots"] == result["roots"]
    assert (
        store.projects["moving-project"]["graph_generation"]
        != generation_before_move
    )
    assert store.projects["moving-project"]["graph_requires_full_rebuild"] is True


@pytest.mark.asyncio
async def test_deleted_project_reactivation_rotates_graph_generation(monkeypatch):
    api = load_api_module()
    store = RootAuthorityStore()
    configure_root_authority_api(api, store, monkeypatch)
    root = "/projects/reactivated"

    async def upsert(status: str):
        return await api.register_project(
            api.ProjectRegister(
                project_key="reactivated-project",
                repo_root=root,
                status=status,
                roots=[{"path": root, "kind": "primary"}],
            ),
            SimpleNamespace(headers={}),
        )

    await upsert("deleted")
    previous_generation = store.projects["reactivated-project"]["graph_generation"]
    await upsert("active")

    project = store.projects["reactivated-project"]
    assert project["graph_generation"] != previous_generation
    assert project["graph_requires_full_rebuild"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_shape", ["target-secondary", "duplicate-primary"])
async def test_patch_rebuilds_metadata_from_one_exact_durable_primary(
    monkeypatch, legacy_shape
):
    api = load_api_module()
    store = RootAuthorityStore()
    project_key = "moving-project"
    target = "/projects/target"
    old = "/projects/old"
    store.projects[project_key] = {
        "project_id": "id-moving",
        "repo_root": target if legacy_shape == "duplicate-primary" else old,
        "status": "active",
        "metadata": {
            "roots": [
                {"path": old, "kind": "primary"},
                {
                    "path": target,
                    "kind": "primary" if legacy_shape == "duplicate-primary" else "secondary",
                },
            ]
        },
    }
    store.roots[old] = {
        "project_key": project_key,
        "root_path": old,
        "path_kind": "primary",
        "metadata": {"path": old, "kind": "primary"},
    }
    store.roots[target] = {
        "project_key": project_key,
        "root_path": target,
        "path_kind": "primary" if legacy_shape == "duplicate-primary" else "secondary",
        "metadata": {
            "path": target,
            "kind": "primary" if legacy_shape == "duplicate-primary" else "secondary",
        },
    }
    configure_root_authority_api(api, store, monkeypatch)

    await api.patch_project(
        project_key,
        api.ProjectPatch(repo_root=target),
        SimpleNamespace(headers={}),
    )
    assert set(store.roots) == {target}
    assert store.roots[target]["path_kind"] == "primary"
    assert store.projects[project_key]["repo_root"] == target
    assert store.projects[project_key]["metadata"]["roots"] == [
        {"path": target, "kind": "primary"}
    ]
