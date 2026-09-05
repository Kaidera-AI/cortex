"""Job-ledger DDL must run on the admin pool, never on a scoped app connection.

main.py:304 states the rule: "Runtime project handlers use pool_app/cortex_app and must
not run DDL." Both job-ledger helpers broke it by running CREATE TABLE on whatever
connection the caller held — always the scoped app connection.

cortex_app holds USAGE but not CREATE on schema public, so on marlow every async
backfill died with `InsufficientPrivilegeError: permission denied for schema public`.
It stayed invisible because the sync path (limit <= CORTEX_EMBED_SYNC_LIMIT, 100) never
touches the ledger, and the existing async test stubs create_embedding_backfill_job out
entirely — so nothing ever executed the DDL.
"""

import importlib.util
from pathlib import Path

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RecordingConn:
    def __init__(self, label):
        self.label = label
        self.sql = []

    async def execute(self, sql, *args):
        self.sql.append(sql)
        return "OK"

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        return None

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return []

    @property
    def ddl(self):
        return [s for s in self.sql if "CREATE TABLE" in s or "CREATE INDEX" in s]


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


def wire(module):
    admin = RecordingConn("admin")
    app = RecordingConn("app")
    module.pool_admin = FakePool(admin)
    module.pool_app = FakePool(app)
    module.pool = FakePool(app)
    return admin, app


@pytest.mark.asyncio
async def test_embedding_backfill_ledger_ddl_runs_on_admin_pool():
    module = load_module(API_MAIN_PATH, "cortex_api_admin_ddl_embedding_test")
    admin, app = wire(module)

    await module.ensure_embedding_backfill_jobs_schema()

    assert any("embedding_backfill_jobs" in s for s in admin.ddl), (
        "CREATE TABLE embedding_backfill_jobs did not run on the admin pool"
    )
    assert app.ddl == [], f"DDL leaked onto the scoped app pool: {app.ddl}"


@pytest.mark.asyncio
async def test_graph_build_ledger_ddl_runs_on_admin_pool():
    module = load_module(API_MAIN_PATH, "cortex_api_admin_ddl_graph_test")
    admin, app = wire(module)

    await module.ensure_graph_build_jobs_schema()

    assert any("graph_build_jobs" in s for s in admin.ddl), (
        "CREATE TABLE graph_build_jobs did not run on the admin pool"
    )
    assert app.ddl == [], f"DDL leaked onto the scoped app pool: {app.ddl}"


@pytest.mark.asyncio
async def test_ledger_helpers_take_no_connection_argument():
    """A conn parameter is what let callers hand these the wrong pool.

    Keeping the signature argument-less is the property that prevents the defect
    from returning; a caller cannot pass the app connection if there is nowhere
    to put it.
    """
    import inspect

    module = load_module(API_MAIN_PATH, "cortex_api_admin_ddl_signature_test")
    for fn in (
        module.ensure_embedding_backfill_jobs_schema,
        module.ensure_graph_build_jobs_schema,
    ):
        assert not inspect.signature(fn).parameters, (
            f"{fn.__name__} accepts a connection again; it must acquire pool_admin itself"
        )
