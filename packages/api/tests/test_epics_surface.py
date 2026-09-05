"""E006 epic-surface endpoint tests (GET /epics, GET /epics/{id}, POST /epics).

These prove, without a live DB (same FakeConn/FakePool style as test_contracts):
  - GET /epics binds the X-Project scope into `WHERE project = $1` and shapes the
    increments JSONB — i.e. it is project-scoped exactly like /roster, so a caller
    can never read another project's epics (no cross-project leak).
  - GET /epics/{id} 404s when the (project, epic_id) pair is absent.
  - POST /epics is admin-gated (require_admin_access → 403 without the token) and,
    with the token, upserts within the caller's project scope only.
  - acquire_scoped() sets/clears the cortex.project GUC around the query (the RLS
    enforcement contract).
"""

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
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


class EpicConn:
    """Fake connection that records the project bound into each epics query.

    Stores rows keyed by (project, epic_id) so the WHERE project=$1 filter is
    actually exercised — a query for another project returns nothing.
    """

    def __init__(self, rows):
        # rows: list of dicts with project/epic_id/title/status/overall_pct/increments
        self.rows = rows
        self.scope_calls = []  # every set_config('cortex.project', X) value
        self.upserts = []

    async def execute(self, sql, *args):
        # acquire_scoped sets the GUC with a bound arg, then clears it with a
        # SQL literal '' (no bound arg). Record the set-value; record None on clear.
        if "set_config('cortex.project', $1" in sql:
            self.scope_calls.append(args[0] if args else None)
            return "SELECT 1"
        if "set_config('cortex.project', ''" in sql:
            self.scope_calls.append("")  # cleared
            return "SELECT 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetch(self, sql, *args):
        if "FROM epics" in sql and "WHERE project = $1" in sql:
            project = args[0]
            return [
                {
                    "project": r["project"],
                    "epic_id": r["epic_id"],
                    "title": r["title"],
                    "status": r["status"],
                    "overall_pct": r["overall_pct"],
                    "increments": r["increments"],
                    "updated_at": "2026-06-01T00:00:00+00:00",
                }
                for r in self.rows
                if r["project"] == project
            ]
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def fetchrow(self, sql, *args):
        if "FROM epics" in sql and "WHERE project = $1 AND epic_id = $2" in sql:
            project, epic_id = args
            for r in self.rows:
                if r["project"] == project and r["epic_id"] == epic_id:
                    return {
                        "project": r["project"],
                        "epic_id": r["epic_id"],
                        "title": r["title"],
                        "status": r["status"],
                        "overall_pct": r["overall_pct"],
                        "increments": r["increments"],
                        "updated_at": "2026-06-01T00:00:00+00:00",
                    }
            return None
        if sql.strip().startswith("INSERT INTO epics"):
            project, epic_id, title, status, overall_pct, increments_json = args
            self.upserts.append(
                {
                    "project": project,
                    "epic_id": epic_id,
                    "overall_pct": overall_pct,
                    "increments_json": increments_json,
                }
            )
            return {
                "project": project,
                "epic_id": epic_id,
                "title": title,
                "status": status,
                "overall_pct": overall_pct,
                "increments": increments_json,  # JSONB returned as text → handler parses
                "updated_at": "2026-06-01T00:00:00+00:00",
            }
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")


SEED_ROWS = [
    {
        "project": "kaidera-os",
        "epic_id": "E006",
        "title": "Cortex Surface Canonicalization + Redis Retirement",
        "status": "active",
        "overall_pct": 40,
        "increments": [
            {"num": 0, "title": "drift map", "status": "done", "pct": 100},
            {"num": 4, "title": "roster-as-data", "status": "in_progress", "pct": 90},
        ],
    },
    {
        "project": "kaidera-os",
        "epic_id": "E007",
        "title": "Kaidera OS Harness Platform",
        "status": "build",
        "overall_pct": 18,
        "increments": [{"num": 0, "title": "dashboard", "status": "in_progress", "pct": 100}],
    },
    # A different project's epic — must never surface for X-Project: kaidera-os.
    {
        "project": "kaidera",
        "epic_id": "E900",
        "title": "Some other project epic",
        "status": "active",
        "overall_pct": 55,
        "increments": [],
    },
]


@pytest.fixture
def api():
    return load_module(API_MAIN_PATH, "cortex_api_main_epics_test")


@pytest.fixture
def conn():
    return EpicConn([dict(r) for r in SEED_ROWS])


@pytest.mark.asyncio
async def test_list_epics_is_project_scoped(api, conn, monkeypatch):
    monkeypatch.setattr(api, "pool_app", FakePool(conn))

    result = await api.list_epics(x_project="kaidera-os")

    assert result["project"] == "kaidera-os"
    ids = {e["epic_id"] for e in result["epics"]}
    # Only kaidera-os epics; kaidera's E900 must NOT leak.
    assert ids == {"E006", "E007"}
    assert "E900" not in ids
    # acquire_scoped set the GUC to kaidera-os then cleared it.
    assert conn.scope_calls[0] == "kaidera-os"
    assert conn.scope_calls[-1] == ""


@pytest.mark.asyncio
async def test_list_epics_other_project_cannot_see_kaidera_os(api, conn, monkeypatch):
    monkeypatch.setattr(api, "pool_app", FakePool(conn))

    result = await api.list_epics(x_project="kaidera")

    ids = {e["epic_id"] for e in result["epics"]}
    assert ids == {"E900"}
    assert "E006" not in ids and "E007" not in ids


@pytest.mark.asyncio
async def test_list_epics_shapes_increments(api, conn, monkeypatch):
    monkeypatch.setattr(api, "pool_app", FakePool(conn))

    result = await api.list_epics(x_project="kaidera-os")
    e006 = next(e for e in result["epics"] if e["epic_id"] == "E006")

    assert e006["overall_pct"] == 40
    assert isinstance(e006["increments"], list)
    assert e006["increments"][0]["num"] == 0
    assert e006["increments"][0]["status"] == "done"


@pytest.mark.asyncio
async def test_list_epics_requires_project_header(api):
    with pytest.raises(HTTPException) as exc:
        await api.list_epics(x_project="")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_get_epic_returns_single(api, conn, monkeypatch):
    monkeypatch.setattr(api, "pool_app", FakePool(conn))

    result = await api.get_epic("E007", x_project="kaidera-os")

    assert result["epic_id"] == "E007"
    assert result["status"] == "build"
    assert result["overall_pct"] == 18


@pytest.mark.asyncio
async def test_get_epic_404_for_unknown(api, conn, monkeypatch):
    monkeypatch.setattr(api, "pool_app", FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await api.get_epic("E999", x_project="kaidera-os")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_epic_404_when_other_projects_epic(api, conn, monkeypatch):
    # kaidera-os asking for kaidera's E900 must 404 (scoped), never return it.
    monkeypatch.setattr(api, "pool_app", FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await api.get_epic("E900", x_project="kaidera-os")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_post_epic_requires_admin_token(api, conn, monkeypatch):
    monkeypatch.setattr(api, "pool_app", FakePool(conn))
    # Force a non-empty configured token so the gate is active, and send none.
    monkeypatch.setattr(api, "ADMIN_TOKEN", "secret-token")
    request = Request({"type": "http", "headers": []})

    body = api.EpicUpsert(epic_id="E010", title="New", status="active", overall_pct=10)
    with pytest.raises(HTTPException) as exc:
        await api.upsert_epic(body, request, x_project="kaidera-os")
    assert exc.value.status_code == 403
    # No write happened.
    assert conn.upserts == []


@pytest.mark.asyncio
async def test_post_epic_upserts_within_scope_with_token(api, conn, monkeypatch):
    monkeypatch.setattr(api, "pool_app", FakePool(conn))
    monkeypatch.setattr(api, "ADMIN_TOKEN", "secret-token")

    async def fake_require_registered_project(project):
        return {"project_key": project, "project_id": "33333333-3333-4333-8333-333333333333"}

    monkeypatch.setattr(api, "require_registered_project", fake_require_registered_project)

    request = Request(
        {
            "type": "http",
            "headers": [(b"x-cortex-admin-token", b"secret-token")],
        }
    )
    body = api.EpicUpsert(
        epic_id="E010",
        title="New Epic",
        status="active",
        overall_pct=25,
        increments=[api.EpicIncrement(num=0, title="kickoff", status="in_progress", pct=25)],
    )

    result = await api.upsert_epic(body, request, x_project="kaidera-os")

    assert result["upserted"] is True
    assert result["epic"]["epic_id"] == "E010"
    assert result["epic"]["overall_pct"] == 25
    assert isinstance(result["epic"]["increments"], list)
    assert result["epic"]["increments"][0]["title"] == "kickoff"
    # The write was bound to the kaidera-os scope.
    assert conn.upserts[0]["project"] == "kaidera-os"
    assert conn.scope_calls[0] == "kaidera-os"


@pytest.mark.asyncio
async def test_post_epic_rejects_out_of_range_pct(api, conn, monkeypatch):
    monkeypatch.setattr(api, "pool_app", FakePool(conn))
    monkeypatch.setattr(api, "ADMIN_TOKEN", "secret-token")

    async def fake_require_registered_project(project):
        return {"project_key": project, "project_id": "33333333-3333-4333-8333-333333333333"}

    monkeypatch.setattr(api, "require_registered_project", fake_require_registered_project)

    request = Request(
        {"type": "http", "headers": [(b"x-cortex-admin-token", b"secret-token")]}
    )
    body = api.EpicUpsert(epic_id="E011", overall_pct=150)
    with pytest.raises(HTTPException) as exc:
        await api.upsert_epic(body, request, x_project="kaidera-os")
    assert exc.value.status_code == 400
