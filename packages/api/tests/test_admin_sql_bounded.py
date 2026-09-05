"""/admin/sql/query is memory-bounded (outage-prevention Part C, handoff 8b4b4b49).

The old handler did `conn.fetch(sql)` — the ENTIRE result set materialised in API
memory, so one large admin query could OOM the API. The bounded handler streams
via a server-side cursor and stops at a row cap, so the API never holds more than
the cap regardless of the full result size. These tests prove:

  * a result larger than the cap is truncated (rows capped, `truncated=True`) AND
    the cursor stops early — it does NOT pull the whole set (the memory proof);
  * a result under the cap returns everything with `truncated=False`;
  * a non-row-returning statement still runs and returns no rows (back-compat);
  * the admin-token authorization is unchanged.
"""

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
TEST_ADMIN_TOKEN = "unit-test-admin-token"


def _load():
    spec = importlib.util.spec_from_file_location("cortex_api_sqlbound_test", API_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.ADMIN_TOKEN = TEST_ADMIN_TOKEN
    return module


class FakePrepared:
    """Stand-in for an asyncpg PreparedStatement + its streaming cursor."""

    def __init__(self, rows, returns_rows=True):
        self._rows = rows
        self._returns = returns_rows
        self.pulled = 0          # rows the cursor actually yielded (memory proof)
        self.fetch_called = False

    def get_attributes(self):
        # Non-empty = a row-returning statement (SELECT); empty = INSERT/UPDATE/DDL.
        return (("col",),) if self._returns else ()

    async def fetch(self, *a, **k):
        self.fetch_called = True
        return list(self._rows)

    def cursor(self, *a, **k):
        prepared = self

        class _Cur:
            def __aiter__(self):
                self._it = iter(prepared._rows)
                return self

            async def __anext__(self):
                try:
                    row = next(self._it)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc
                prepared.pulled += 1
                return row

        return _Cur()


class _Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, prepared):
        self._prepared = prepared

    def transaction(self):
        return _Txn()

    async def prepare(self, sql):
        return self._prepared


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


def _admin_request():
    return Request({
        "type": "http",
        "headers": [(b"x-cortex-admin-token", TEST_ADMIN_TOKEN.encode())],
        "query_string": b"",
    })


def _inject(module, prepared):
    module.pool_admin = FakePool(FakeConn(prepared))


@pytest.mark.asyncio
async def test_large_result_is_capped_and_flagged_without_pulling_everything(monkeypatch):
    module = _load()
    prepared = FakePrepared([(i, f"v{i}") for i in range(25)])
    _inject(module, prepared)
    monkeypatch.setattr(module, "_admin_sql_max_rows", lambda: 10)

    result = await module.admin_sql_query(module.SqlRequest(sql="SELECT * FROM big"), _admin_request())

    assert len(result["rows"]) == 10
    assert result["truncated"] is True
    assert result["row_count"] == 10
    # Memory proof: the cursor stopped at the cap (+1 to detect the overflow) and
    # did NOT stream all 25 rows into API memory.
    assert prepared.pulled == 11
    assert prepared.pulled < 25


@pytest.mark.asyncio
async def test_small_result_returns_all_untruncated(monkeypatch):
    module = _load()
    prepared = FakePrepared([(1, "a"), (2, "b"), (3, "c")])
    _inject(module, prepared)
    monkeypatch.setattr(module, "_admin_sql_max_rows", lambda: 10)

    result = await module.admin_sql_query(module.SqlRequest(sql="SELECT * FROM small"), _admin_request())

    assert result["rows"] == [[1, "a"], [2, "b"], [3, "c"]]
    assert result["truncated"] is False
    assert result["row_count"] == 3
    assert prepared.pulled == 3


@pytest.mark.asyncio
async def test_non_returning_statement_runs_and_returns_no_rows():
    module = _load()
    prepared = FakePrepared([], returns_rows=False)
    _inject(module, prepared)

    result = await module.admin_sql_query(module.SqlRequest(sql="UPDATE t SET x=1"), _admin_request())

    assert result["rows"] == []
    assert result["truncated"] is False
    assert result["row_count"] == 0
    assert prepared.fetch_called is True  # executed via fetch(), not the cursor
    assert prepared.pulled == 0


@pytest.mark.asyncio
async def test_requires_admin_token():
    module = _load()
    _inject(module, FakePrepared([(1, "a")]))
    no_token = Request({"type": "http", "headers": [], "query_string": b""})
    with pytest.raises(HTTPException) as ei:
        await module.admin_sql_query(module.SqlRequest(sql="SELECT 1"), no_token)
    assert ei.value.status_code == 403


def test_max_rows_env_override(monkeypatch):
    module = _load()
    monkeypatch.setenv("CORTEX_ADMIN_SQL_MAX_ROWS", "42")
    assert module._admin_sql_max_rows() == 42
    monkeypatch.setenv("CORTEX_ADMIN_SQL_MAX_ROWS", "bad")
    assert module._admin_sql_max_rows() == module._ADMIN_SQL_MAX_ROWS_DEFAULT


def test_admin_stats_quotes_catalog_identifiers_with_the_strict_identifier_guard():
    source = API_MAIN_PATH.read_text(encoding="utf-8")
    start = source.index("async def admin_stats(request: Request):")
    end = source.index("\n\n@app.", start + 1)
    handler = source[start:end]

    assert "quote_ident(str(r['schema']))" in handler
    assert "quote_ident(str(r['rel']))" in handler
    assert 'FROM "{r["schema"]}"' not in handler
