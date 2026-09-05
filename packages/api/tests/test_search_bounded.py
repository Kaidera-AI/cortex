"""Regression coverage for incident 912073e2 — unbounded /search.

On marlow-aws the trigram stage ran an unindexable predicate over full body text,
averaging 36.7s per execution while clients timed out at 20s. Postgres kept
serving results nobody would read until it took SIGPIPE writing to a departed
client and crash-recovered the whole cluster — 362 times in 14 hours.

These tests pin the three properties that prevent a repeat:
  1. the trigram match predicate stays index-servable,
  2. a query too short to use a trigram index skips the stage entirely,
  3. a query killed by the database deadline degrades instead of 500-ing.
"""

from __future__ import annotations

import asyncio
import time
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest


@pytest.fixture
def api_module():
    src = Path(__file__).resolve().parent.parent / "main.py"
    spec = importlib.util.spec_from_file_location("cortex_api_bounded_under_test", src)
    assert spec and spec.loader, f"could not load spec for {src}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingConn:
    """Captures every SQL statement /search issues.

    `rows_for` / `on_execute` may raise to simulate a stage the database killed
    at the statement deadline — the failure mode the search budget introduced.
    """

    def __init__(self, rows_for=None, on_execute=None):
        self.statements: list[str] = []
        self._rows_for = rows_for or (lambda sql: [])
        self._on_execute = on_execute

    async def fetch(self, sql, *_args):
        self.statements.append(sql)
        return self._rows_for(sql)

    async def fetchrow(self, *_args, **_kwargs):
        return None

    async def execute(self, sql, *_args, **_kwargs):
        self.statements.append(sql)
        if self._on_execute is not None:
            self._on_execute(sql)
        return "UPDATE 0"


def _run_search(api_module, conn, query, **kwargs):
    return asyncio.run(
        api_module.execute_search(
            conn, "kaidera", query, rerank=False, graph=False, limit=5, **kwargs
        )
    )


@pytest.fixture
def offline_search(api_module, monkeypatch):
    """Neutralise the embedding + graph stages so tests isolate the lexical path."""

    async def no_embedding(_query, _project=""):
        return None

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_query_cached", no_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)
    return api_module


def _trigram_statements(conn):
    """Just the Stage 1 trigram queries.

    Stage 1 is the only one that aliases with lowercase `as source` over the
    knowledge/decisions/lessons tables; BM25 uses `AS source`, and the
    work_products and artifact stages have their own shapes. Matching on
    "similarity(" alone would sweep those in and make these assertions lie.
    """
    return [
        s
        for s in conn.statements
        if "' as source" in s
        and any(f"FROM {t}" in s for t in ("knowledge", "decisions", "lessons"))
    ]


def test_trigram_predicate_stays_index_servable(offline_search):
    """The `similarity(col,$1) > 0.1 OR ...` disjunction must not come back.

    A GIN trgm index can serve a disjunction only if EVERY branch is indexable.
    similarity() as a filter never is, so that one branch forced a sequential
    scan over full body text and left the existing index unused.
    """
    conn = RecordingConn()
    _run_search(offline_search, conn, "cannot connect now error")

    trigram_sql = _trigram_statements(conn)
    assert trigram_sql, "expected the trigram stage to run for a long-enough query"

    for sql in trigram_sql:
        assert "similarity(content, $1) > 0.1" not in sql
        assert "similarity(summary, $1) > 0.1" not in sql
        # similarity() may only ever appear in ORDER BY, ranking rows the index
        # already returned — never in WHERE, where it would defeat the index.
        where_clause = sql.split("ORDER BY")[0]
        assert "similarity(" not in where_clause, (
            f"similarity() reappeared in a WHERE clause, which forces a seq scan:\n{sql}"
        )


def test_trigram_matches_on_bare_column_so_the_existing_index_applies(offline_search):
    """Match on the bare column; the deployed trgm indexes are on bare columns.

    idx_knowledge_content_trgm / idx_decisions_summary_trgm /
    idx_lessons_summary_trgm are all `USING gin (<col> gin_trgm_ops)`. Postgres
    only uses an expression index on an exact expression match, so wrapping the
    match column in LEFT()/LOWER() would silently restore the sequential scan.
    """
    conn = RecordingConn()
    _run_search(offline_search, conn, "broken pipe signal 13")

    statements = _trigram_statements(conn)
    assert statements, "expected the trigram stage to run"

    for sql in statements:
        # Only the match predicate matters — the one directly after WHERE. LEFT()
        # is fine and intentional elsewhere (display columns, room filter, ORDER
        # BY), because those run on rows the index already narrowed.
        match_predicate = sql.split("WHERE", 1)[1].split("AND", 1)[0].strip()
        assert match_predicate in (
            "content ILIKE '%' || $1 || '%'",
            "summary ILIKE '%' || $1 || '%'",
        ), f"match predicate is not a bare indexed column: {match_predicate!r}"


def test_trigram_similarity_ranks_a_bounded_candidate_set(offline_search):
    """A common term must not similarity-sort every index match.

    `handoff` matched 47k decisions on production: the GIN scan took 13ms but
    heap-reading and ranking the full set took 2.3s. The materialized candidate
    cap keeps fuzzy ranking bounded while BM25/vector stages preserve recall.
    """
    conn = RecordingConn()
    _run_search(offline_search, conn, "handoff recovery signal")

    statements = _trigram_statements(conn)
    assert statements
    assert offline_search.TRGM_CANDIDATE_LIMIT == 100
    for sql in statements:
        assert "WITH trigram_candidates AS MATERIALIZED" in sql
        candidate_sql = sql.split(
            ")\n                 SELECT", 1
        )[0]
        assert f"LIMIT {offline_search.TRGM_CANDIDATE_LIMIT}" in candidate_sql
        assert "SELECT *" not in candidate_sql
        assert "AS rank_text" in candidate_sql
        assert "similarity(rank_text, $1)" in sql


def test_decisions_trigram_uses_the_scoped_index_bridge(offline_search):
    """RLS must stay enabled without forcing decisions back to a seq scan."""
    conn = RecordingConn()
    _run_search(offline_search, conn, "release gate")

    bridge_sql = [
        sql for sql in conn.statements if "cortex_search_decisions_trigram" in sql
    ]
    assert len(bridge_sql) == 1
    assert "FROM decisions" not in bridge_sql[0]
    assert str(offline_search.TRGM_CANDIDATE_LIMIT) in bridge_sql[0]


def test_messages_bm25_uses_the_scoped_index_bridge(offline_search):
    """RLS must stay enabled without forcing messages back to a seq scan."""
    conn = RecordingConn()
    _run_search(offline_search, conn, "release gate")

    bridge_sql = [
        sql for sql in conn.statements if "cortex_search_messages_bm25" in sql
    ]
    assert len(bridge_sql) == 1
    assert "FROM messages" not in bridge_sql[0]
    assert "($2, $1, $3)" in bridge_sql[0]
    assert not any(
        "ts_rank_cd" in sql and "FROM messages" in sql for sql in conn.statements
    )


def test_missing_messages_bm25_bridge_fails_loud(offline_search):
    """A missed required migration must not silently erase message recall."""

    def rows_for(sql):
        if "cortex_search_messages_bm25" in sql:
            raise RuntimeError("required messages BM25 bridge is missing")
        return []

    with pytest.raises(RuntimeError, match="required messages BM25 bridge is missing"):
        _run_search(offline_search, RecordingConn(rows_for=rows_for), "release gate")


def test_short_query_skips_the_trigram_stage(offline_search, api_module):
    """Below 3 characters ILIKE '%x%' has no trigram to look up.

    pg_trgm cannot index-serve such a pattern, so running the stage anyway would
    mean a guaranteed full scan — precisely what the incident was.
    """
    assert api_module.TRGM_MIN_QUERY_LEN == 3

    conn = RecordingConn()
    result = _run_search(offline_search, conn, "ab")

    assert _trigram_statements(conn) == []
    assert result["degraded"] == []


def test_cancelled_query_degrades_instead_of_failing_the_request(offline_search):
    """A statement-deadline kill is reported, not swallowed and not fatal.

    During the incident this raised out of the handler as a 500. It must instead
    drop that one table and name it in `degraded`, so callers can tell a partial
    answer from a complete one.
    """

    def rows_for(sql):
        if "FROM knowledge" in sql and "similarity(" in sql:
            raise asyncpg.exceptions.QueryCanceledError("statement timeout")
        return []

    conn = RecordingConn(rows_for=rows_for)
    result = _run_search(offline_search, conn, "marlow search timeout")

    assert result["degraded"] == ["trigram:knowledge"], (
        "a deadline-killed stage must be reported as degraded"
    )
    assert "results" in result


def test_healthy_search_reports_no_degradation(offline_search):
    """The truthful-health contract runs both ways: don't cry degraded when fine."""
    conn = RecordingConn()
    result = _run_search(offline_search, conn, "healthy query text")
    assert result["degraded"] == []


def test_app_pool_carries_a_deadline_shorter_than_client_timeouts(api_module):
    """The containment backstop: no app query may outlive its reader.

    The incident's client gave up at 20.047s; the server must give up first.
    """
    assert 0 < api_module.APP_STATEMENT_TIMEOUT_MS < 20_000


def test_search_budget_is_tighter_than_the_app_wide_backstop(api_module):
    """Search must fail fast; batch paths keep the larger ceiling.

    Both bounds matter: search under the incident's 5s hard maximum, and strictly
    below the app-wide backstop so raising one never silently raises the other.
    """
    assert 0 < api_module.SEARCH_STATEMENT_TIMEOUT_MS <= 5_000
    assert api_module.SEARCH_STATEMENT_TIMEOUT_MS < api_module.APP_STATEMENT_TIMEOUT_MS


def test_search_applies_its_budget_and_restores_the_pool_default(api_module):
    """The tighter budget is session-level, so it must be reset on release.

    A connection returned to the pool still carrying search's 3s budget would
    impose it on whatever handler acquired that connection next.
    """
    executed: list[str] = []

    class PoolConn:
        async def execute(self, sql, *_args):
            executed.append(sql)
            return "SET"

    class FakePool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_inner):
                    return PoolConn()

                async def __aexit__(self_inner, *_exc):
                    return False

            return Ctx()

    async def scenario():
        async with api_module.acquire_scoped("kaidera", statement_timeout_ms=3000):
            pass

    original = api_module.pool_app
    api_module.pool_app = FakePool()
    try:
        asyncio.run(scenario())
    finally:
        api_module.pool_app = original

    assert "SET statement_timeout = 3000" in executed
    assert "RESET statement_timeout" in executed
    assert executed.index("SET statement_timeout = 3000") < executed.index(
        "RESET statement_timeout"
    )


def test_release_survives_a_client_side_dead_connection(api_module):
    """A crash-recovered cluster raises InterfaceError, not PostgresError.

    asyncpg's two exception roots are disjoint: PostgresError means the server
    sent an error, InterfaceError means the socket is already gone
    (`_check_open` -> InterfaceError('connection is closed')). Post-crash-recovery
    — the incident's own scenario — produces the latter. If the release guard only
    catches PostgresError, the reset failure propagates and REPLACES the real error
    being unwound, which is the "connection has been released back to the pool"
    misdirection that hid the root cause for 14 hours.
    """
    terminated: list[bool] = []

    class DeadConn:
        async def execute(self, sql, *_args):
            if "set_config('cortex.project', $1" in sql or sql.startswith("SET "):
                return "SET"
            # Release-time statements hit a socket that is already gone.
            raise asyncpg.InterfaceError("connection is closed")

        def terminate(self):
            terminated.append(True)

    class FakePool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_inner):
                    return DeadConn()

                async def __aexit__(self_inner, *_exc):
                    return False

            return Ctx()

    async def scenario():
        async with api_module.acquire_scoped("kaidera", statement_timeout_ms=3000):
            raise RuntimeError("the real error")

    original = api_module.pool_app
    api_module.pool_app = FakePool()
    try:
        with pytest.raises(RuntimeError, match="the real error"):
            asyncio.run(scenario())
    finally:
        api_module.pool_app = original

    # Scope is unknown, so the connection must not go back into the pool.
    assert terminated == [True]


def test_search_bulkhead_sheds_load_with_a_typed_503(api_module, monkeypatch):
    """Excess search load is refused, not queued onto a saturated pool."""
    monkeypatch.setattr(api_module, "SEARCH_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(api_module, "_SEARCH_SEMAPHORE", None)

    async def scenario():
        held = api_module.search_bulkhead()
        await held.__aenter__()
        try:
            with pytest.raises(api_module.HTTPException) as excinfo:
                async with api_module.search_bulkhead():
                    pass
            return excinfo.value
        finally:
            await held.__aexit__(None, None, None)

    exc = asyncio.run(scenario())
    assert exc.status_code == 503
    # Typed, so a client can distinguish shed load from a genuine Cortex outage.
    assert exc.detail["error"] == "search_overloaded"


def test_bulkhead_admits_again_after_the_slot_is_released(api_module, monkeypatch):
    """Shedding must be transient — a freed slot accepts the next request."""
    monkeypatch.setattr(api_module, "SEARCH_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(api_module, "_SEARCH_SEMAPHORE", None)

    async def scenario():
        async with api_module.search_bulkhead():
            pass
        async with api_module.search_bulkhead():
            return True

    assert asyncio.run(scenario()) is True


def test_default_bulkhead_matches_a_two_vcpu_self_contained_host(api_module):
    """The shipped default must contain aggregate pressure, not just each query."""
    assert api_module.SEARCH_MAX_CONCURRENCY == 2


def _bounded_request(api_module):
    return api_module._execute_bounded_search(
        project="kaidera",
        query="recovery probe",
        search_type="all",
        rerank=False,
        room=None,
        hall="project",
        graph=False,
        limit=5,
    )


def test_shed_search_never_touches_the_database(api_module, monkeypatch):
    """Admission must happen before even the lightweight project lookup."""
    monkeypatch.setattr(api_module, "SEARCH_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(api_module, "_SEARCH_SEMAPHORE", None)
    registration_calls: list[str] = []

    async def registered(project):
        registration_calls.append(project)

    monkeypatch.setattr(api_module, "require_registered_project", registered)

    async def scenario():
        held = api_module.search_bulkhead()
        await held.__aenter__()
        try:
            with pytest.raises(api_module.HTTPException) as excinfo:
                await _bounded_request(api_module)
            return excinfo.value
        finally:
            await held.__aexit__(None, None, None)

    exc = asyncio.run(scenario())
    assert exc.detail["error"] == "search_overloaded"
    assert registration_calls == []


def test_search_acquire_during_recovery_returns_typed_503(api_module, monkeypatch):
    """PostgreSQL rejects new sessions during recovery; that must not leak as 500."""

    async def registered(_project):
        return True

    @asynccontextmanager
    async def recovering_pool(*_args, **_kwargs):
        raise asyncpg.exceptions.CannotConnectNowError("database is starting up")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(api_module, "require_registered_project", registered)
    monkeypatch.setattr(api_module, "acquire_scoped", recovering_pool)

    with pytest.raises(api_module.HTTPException) as excinfo:
        asyncio.run(_bounded_request(api_module))

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"] == "search_unavailable"
    assert "database is starting up" not in str(excinfo.value.detail)


def test_search_connection_loss_returns_typed_503(api_module, monkeypatch):
    """A backend restart can sever an acquired socket while search is executing."""

    async def registered(_project):
        return True

    @asynccontextmanager
    async def connected_pool(*_args, **_kwargs):
        yield object()

    async def disconnected(*_args, **_kwargs):
        raise asyncpg.ConnectionDoesNotExistError("connection was closed")

    monkeypatch.setattr(api_module, "require_registered_project", registered)
    monkeypatch.setattr(api_module, "acquire_scoped", connected_pool)
    monkeypatch.setattr(api_module, "execute_search", disconnected)

    with pytest.raises(api_module.HTTPException) as excinfo:
        asyncio.run(_bounded_request(api_module))

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"] == "search_unavailable"


def test_search_outer_deadline_returns_typed_503(api_module, monkeypatch):
    """A timeout outside an optional stage still degrades instead of becoming 500."""

    async def registered(_project):
        return True

    @asynccontextmanager
    async def connected_pool(*_args, **_kwargs):
        yield object()

    async def timed_out(*_args, **_kwargs):
        raise asyncpg.exceptions.QueryCanceledError("statement timeout")

    monkeypatch.setattr(api_module, "require_registered_project", registered)
    monkeypatch.setattr(api_module, "acquire_scoped", connected_pool)
    monkeypatch.setattr(api_module, "execute_search", timed_out)

    with pytest.raises(api_module.HTTPException) as excinfo:
        asyncio.run(_bounded_request(api_module))

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"] == "search_timed_out"


# ---------------------------------------------------------------------------
# Defects introduced BY the containment itself (review of 5747828c).
#
# Adding a 3s database-side budget made QueryCanceledError reachable on every
# statement in execute_search for the first time — pre-fix the app pool had no
# statement_timeout, so it could not fire at all. Only the trigram stage was
# taught to handle it, which left two defects against the incident's own
# acceptance criteria: stages that 500 (violating "zero Cortex 5xx") and stages
# that drop a table while still reporting degraded=[] (violating "truthful").
# ---------------------------------------------------------------------------


@pytest.fixture
def vector_search(api_module, monkeypatch):
    """Enable the pgvector stage — offline_search disables it by returning no embedding."""

    async def fake_embedding(_query, _project=""):
        return [0.0] * 768

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_query_cached", fake_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)
    return api_module


def _vector_statements(conn):
    """Just the Stage 2 pgvector queries over knowledge/decisions/lessons."""
    return [s for s in conn.statements if "(semantic)' as source" in s]


def test_cancelled_vector_stage_degrades_instead_of_500(vector_search):
    """The blocker: a deadline-killed vector stage must not become an HTTP 500.

    A filtered HNSW scan under a selective project/room/invalidation post-filter
    is exactly the shape that blows a 3s budget, so this is reachable rather than
    theoretical. Pre-fix this fetch had no handler at all and the raw
    QueryCanceledError escaped execute_search — reproducing the incident's 500
    via the very mechanism meant to contain it.
    """

    def rows_for(sql):
        if "(semantic)' as source" in sql and "FROM knowledge" in sql:
            raise asyncpg.exceptions.QueryCanceledError("statement timeout")
        return []

    conn = RecordingConn(rows_for=rows_for)
    result = _run_search(vector_search, conn, "marlow search timeout")

    assert "vector:knowledge" in result["degraded"], (
        "a deadline-killed vector stage must degrade, not fail the request"
    )


def test_cancelled_bm25_stage_is_reported_not_silently_dropped(offline_search):
    """The should-fix: `except Exception: pass` made degraded lie.

    BM25 swallowed everything, so a deadline-killed stage dropped that table and
    still returned degraded=[] — a partial answer presented as a complete one.
    """

    def rows_for(sql):
        if "ts_rank_cd" in sql and "FROM knowledge" in sql:
            raise asyncpg.exceptions.QueryCanceledError("statement timeout")
        return []

    conn = RecordingConn(rows_for=rows_for)
    result = _run_search(offline_search, conn, "marlow search timeout")

    assert "bm25:knowledge" in result["degraded"], (
        "a deadline-killed BM25 stage must be named, not swallowed"
    )


def test_cancelled_artifact_stage_is_reported_not_silently_dropped(offline_search):
    """Same swallow, same fix — the L5 artifact stage."""

    def rows_for(sql):
        if "artifact_candidates" in sql:
            raise asyncpg.exceptions.QueryCanceledError("statement timeout")
        return []

    conn = RecordingConn(rows_for=rows_for)
    result = _run_search(offline_search, conn, "marlow search timeout")

    assert "artifacts" in result["degraded"]


def test_cancelled_selection_tracking_never_discards_a_finished_answer(offline_search):
    """Telemetry runs after the answer exists; it must not be able to destroy it.

    times_selected is a statistics counter written once results are assembled.
    Under the new budget an unguarded UPDATE there would 500 a request whose
    results were already complete and correct.
    """

    def rows_for(sql):
        if "' as source" in sql and "FROM knowledge" in sql:
            return [("row-id", "matched text", "src.md", "cat", "knowledge")]
        return []

    def on_execute(sql):
        if "times_selected" in sql:
            raise asyncpg.exceptions.QueryCanceledError("statement timeout")

    conn = RecordingConn(rows_for=rows_for, on_execute=on_execute)
    result = _run_search(offline_search, conn, "marlow search timeout")

    assert result["results"], "the assembled answer must survive a telemetry failure"
    assert "selection_tracking" in result["degraded"]


def test_quality_and_selection_telemetry_keep_uuid_indexes_reachable(offline_search):
    """Post-search telemetry must not scan a large table once per result."""

    def rows_for(sql):
        if "cortex_search_decisions_trigram" in sql:
            return [
                ("11111111-1111-4111-8111-111111111111", "one", "cat", "agent", "decisions"),
                ("22222222-2222-4222-8222-222222222222", "two", "cat", "agent", "decisions"),
            ]
        return []

    conn = RecordingConn(rows_for=rows_for)
    _run_search(offline_search, conn, "handoff recovery signal")

    quality_sql = [sql for sql in conn.statements if "quality_score" in sql]
    assert quality_sql
    assert all("WHERE id = ANY($1::uuid[])" in sql for sql in quality_sql)
    selection_sql = [sql for sql in conn.statements if "UPDATE decisions" in sql]
    assert len(selection_sql) == 1
    assert "WHERE id = ANY($1::uuid[])" in selection_sql[0]


def test_missing_relation_still_skips_the_stage_quietly(offline_search):
    """Narrowing those catches must not break deployments predating a schema.

    An older install without search_vector should still get a usable search, and
    a stage that was never there is not a degraded stage — it is simply absent.
    """

    def rows_for(sql):
        if "ts_rank_cd" in sql:
            raise asyncpg.exceptions.UndefinedTableError("no search_vector here")
        return []

    conn = RecordingConn(rows_for=rows_for)
    result = _run_search(offline_search, conn, "older deployment query")

    assert result["degraded"] == []


def test_a_real_bug_in_a_required_stage_still_surfaces(vector_search):
    """The fix must not become a universal swallow.

    Degrading on a deadline is deliberate; hiding a genuine schema or query bug
    behind the same handler would trade a loud 500 for silent wrong answers.
    """

    def rows_for(sql):
        if "(semantic)' as source" in sql:
            raise asyncpg.exceptions.UndefinedColumnError("embedding")
        return []

    conn = RecordingConn(rows_for=rows_for)
    with pytest.raises(asyncpg.exceptions.UndefinedColumnError):
        _run_search(vector_search, conn, "a real bug must not be hidden")


def test_vector_room_filter_is_bounded_like_the_lexical_stages(vector_search):
    """The room filter's unbounded body ILIKE survived in the vector stage.

    Stage 0 and Stage 1 bound it to the indexed prefix; leaving Stage 2 unbounded
    kept a full-length scan over body text on every room-filtered search.
    """
    conn = RecordingConn()
    _run_search(vector_search, conn, "bounded room filter", room="marlow")

    vector_sql = _vector_statements(conn)
    assert vector_sql, "expected the pgvector stage to run"
    for sql in vector_sql:
        assert "OR content ILIKE" not in sql
        assert "OR summary ILIKE" not in sql
        assert f"LEFT(content, {vector_search.TRGM_PREFIX_CHARS})" in sql or (
            f"LEFT(summary, {vector_search.TRGM_PREFIX_CHARS})" in sql
        )


# ---------------------------------------------------------------------------
# Whole-request budget. A per-stage limit does not bound the sum of the stages:
# seven stages each obediently under 3s still served 24s on marlow-aws.
# ---------------------------------------------------------------------------


class SlowConn(RecordingConn):
    """Every fetch burns wall-clock, so the request budget actually binds."""

    def __init__(self, per_fetch_s: float, **kwargs):
        super().__init__(**kwargs)
        self._per_fetch_s = per_fetch_s
        self.executes: list[str] = []

    async def fetch(self, sql, *args):
        await asyncio.sleep(self._per_fetch_s)
        return await super().fetch(sql, *args)

    async def execute(self, sql, *_args, **_kwargs):
        self.executes.append(sql)
        return "SET"


def test_total_budget_stops_starting_stages_once_it_is_spent(api_module, offline_search, monkeypatch):
    """The defect: 7 stages x per-stage limit, with nothing bounding the total."""
    monkeypatch.setattr(api_module, "SEARCH_TOTAL_BUDGET_MS", 120)
    conn = SlowConn(per_fetch_s=0.05)

    start = time.monotonic()
    out = _run_search(api_module, conn, "budget probe")
    elapsed = time.monotonic() - start

    # Bounded by the request budget, NOT by stages x per-stage timeout.
    assert elapsed < 1.0, f"request ran {elapsed:.2f}s despite a 120ms budget"
    # Stages that never got to run must be reported as partial, not passed off
    # as a complete answer — the same contract as a database-killed stage.
    assert out["degraded"], "stages were skipped but the answer claimed to be complete"


def test_last_stage_clamps_its_timeout_to_the_remaining_budget(api_module, offline_search, monkeypatch):
    """Without the clamp the final stage can overrun by a whole per-stage timeout."""
    monkeypatch.setattr(api_module, "SEARCH_TOTAL_BUDGET_MS", 120)
    monkeypatch.setattr(api_module, "SEARCH_STATEMENT_TIMEOUT_MS", 3000)
    conn = SlowConn(per_fetch_s=0.05)

    _run_search(api_module, conn, "clamp probe")

    clamps = [s for s in conn.executes if "statement_timeout" in s]
    assert clamps, "no statement_timeout clamp issued as the budget ran down"
    # Every clamp must be BELOW the per-stage limit — that is the whole point.
    for stmt in clamps:
        value = int("".join(ch for ch in stmt.split("=")[-1] if ch.isdigit()))
        assert 0 < value < 3000, f"clamp {value}ms did not tighten the 3000ms per-stage limit"


def test_a_generous_budget_changes_nothing(api_module, offline_search, monkeypatch):
    """Guard against the budget silently truncating healthy searches."""
    monkeypatch.setattr(api_module, "SEARCH_TOTAL_BUDGET_MS", 60_000)
    conn = SlowConn(per_fetch_s=0.0)

    out = _run_search(api_module, conn, "healthy query")

    assert out["degraded"] == [], f"a fast search was marked degraded: {out['degraded']}"
    assert not [s for s in conn.executes if "statement_timeout" in s], (
        "clamped a stage that had the whole budget available"
    )


def test_search_executes_the_project_scoped_cached_embedding_path(api_module, monkeypatch):
    """Prove the production call path by execution, not source-string matching."""
    calls: list[tuple[str, str]] = []

    async def cached_embedding(query, project=""):
        calls.append((query, project))
        return None

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_query_cached", cached_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)
    _run_search(api_module, RecordingConn(), "cached path probe")

    assert calls == [("cached path probe", "kaidera")]


def test_a_slow_provider_cannot_outlive_the_request_budget(api_module, monkeypatch):
    """The regression: the budget bounded the DATABASE and nothing else.

    embed_text's own ceiling is 15s and rerank's is 2.5s, both outside the request
    deadline, so a 4.5s budget still served 7.6s and 8.2s on marlow.
    """
    monkeypatch.setattr(api_module, "SEARCH_TOTAL_BUDGET_MS", 300)

    async def glacial_embedding(_query, _project=""):
        await asyncio.sleep(30)  # a provider having a bad day
        return [0.1] * 8

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_query_cached", glacial_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)
    conn = RecordingConn()

    start = time.monotonic()
    out = _run_search(api_module, conn, "slow provider probe")
    elapsed = time.monotonic() - start

    assert elapsed < 3.0, f"a 30s provider dragged the request to {elapsed:.1f}s"
    assert "embedding" in out["degraded"], "a dropped embedding must be reported, not hidden"


def test_external_search_cancellation_is_never_swallowed(api_module, monkeypatch):
    """A disconnected/cancelled caller must stop provider and database work."""

    async def cancelled_embedding(_query, _project=""):
        raise asyncio.CancelledError

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_query_cached", cancelled_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)

    with pytest.raises(asyncio.CancelledError):
        _run_search(api_module, RecordingConn(), "cancel this search")


def test_a_slow_reranker_is_dropped_rather_than_waited_on(api_module, monkeypatch):
    """Rerank improves ORDER, not correctness — the right thing to shed under pressure.

    HONEST LIMITATION: this passes against the pre-fix code too, because with no
    result rows the rerank stage is skipped entirely, so it does not currently prove
    the clamp the way the embedding test does. Kept as a REGRESSION GUARD (it would
    catch a future change that made rerank block), not as evidence. The embedding
    test is the real positive control: it takes 30s against the unfixed code.
    """
    monkeypatch.setattr(api_module, "SEARCH_TOTAL_BUDGET_MS", 300)

    async def no_embedding(_query, _project=""):
        return None

    async def no_graph(*_args, **_kwargs):
        return []

    async def glacial_rerank(_query, _docs):
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(api_module, "embed_query_cached", no_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)
    monkeypatch.setattr(api_module, "rerank_results", glacial_rerank)
    conn = RecordingConn(rows_for=lambda _sql: [])

    # _run_search pins rerank=False, so call through directly to exercise that stage.
    start = time.monotonic()
    asyncio.run(
        api_module.execute_search(
            conn, "kaidera", "slow rerank probe", rerank=True, graph=False, limit=5
        )
    )
    elapsed = time.monotonic() - start

    assert elapsed < 3.0, f"a 30s reranker dragged the request to {elapsed:.1f}s"
