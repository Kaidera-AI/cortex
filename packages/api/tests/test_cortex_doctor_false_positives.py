"""Two doctor checks fired on every deployment regardless of state.

A monitor that always warns is a monitor nobody reads, which is exactly what happened:
marlow sat at `critical` with two of five warnings unfalsifiable.

1. transcript_write_pressure scanned pg_stat_activity for a query matching BOTH
   '%vacuum%' and '%messages%' -- and the scanning query's own text contains both, so it
   matched itself, and a non-empty result forces warn. marlow reported it with
   messages_5m/1h/2h all at 0 and the "vacuum" found at age_seconds -0.0.

2. contract_enum_drift reads `.agents/scripts/cortex-log` at a CWD-relative path.
   cortex-api runs from /app in a container with no source tree, so it always failed to
   read and always warned. "Cannot inspect" is not "drifted".
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


class IdleConn:
    """A database with no transcript traffic and no vacuum running."""

    def __init__(self):
        self.queries = []

    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        if "messages_5m" in sql:
            return {"messages_5m": 0, "messages_1h": 0, "messages_2h": 0}
        return None

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        return []

    async def fetchval(self, sql, *args):
        self.queries.append(sql)
        return None


@pytest.mark.asyncio
async def test_transcript_pressure_is_ok_on_a_completely_idle_database():
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_fp_pressure")
    result = await module.cortex_doctor_transcript_pressure_check(IdleConn())
    assert result["status"] == "ok", (
        f"idle database still warns: {result['evidence'].get('issues')}"
    )


@pytest.mark.asyncio
async def test_vacuum_probe_excludes_its_own_backend():
    """The self-match guard must be in the SQL, not just absent from the result.

    Asserting on the emitted SQL is deliberate: a fake connection returns [] no matter
    what, so an idle-database pass alone would not catch the predicate regressing.
    """
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_fp_sql")
    conn = IdleConn()
    await module.cortex_doctor_transcript_pressure_check(conn)
    vacuum_sql = [q for q in conn.queries if "pg_stat_activity" in q]
    assert vacuum_sql, "the vacuum probe no longer queries pg_stat_activity"
    probe = vacuum_sql[0]
    assert "pg_backend_pid()" in probe, "probe can match its own backend again"
    assert "not like '%pg_stat_activity%'" in probe.lower(), (
        "probe can match another backend that is also reading pg_stat_activity"
    )


def test_contract_drift_reports_unknown_when_the_source_tree_is_absent(monkeypatch):
    """A container with no source tree must not be reported as drift."""
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_fp_contract_absent")
    monkeypatch.setattr(module, "_read_cortex_log_valid_types", lambda: None)
    result = module.cortex_doctor_contract_check()
    assert result["status"] == "unknown", f"got {result['status']}: {result['summary']}"
    assert result["evidence"]["source_tree_readable"] is False
    assert result["evidence"]["issues"] == []


def test_contract_drift_still_warns_on_a_real_mismatch(monkeypatch):
    """The fix must not silence the case the check exists for."""
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_fp_contract_drift")
    monkeypatch.setattr(
        module, "_read_cortex_log_valid_types", lambda: {"decision", "not-a-real-type"}
    )
    result = module.cortex_doctor_contract_check()
    assert result["status"] == "warn"
    assert any("VALID_TYPES differ" in issue for issue in result["evidence"]["issues"])


def test_contract_drift_is_ok_when_the_tree_matches(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_fp_contract_ok")
    monkeypatch.setattr(
        module,
        "_read_cortex_log_valid_types",
        lambda: set(module.CORTEX_LOG_EVENT_TYPES),
    )
    result = module.cortex_doctor_contract_check()
    assert result["status"] == "ok"
    assert result["evidence"]["source_tree_readable"] is True


def test_unknown_ranks_below_warn():
    """`unknown` must not mask a real warning when the overall status is rolled up."""
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_fp_rank")
    rank = module.CORTEX_DOCTOR_STATUS_RANK
    assert rank["ok"] < rank["unknown"] < rank["warn"] < rank["critical"]
    rolled = module.cortex_doctor_overall_status(
        [{"status": "unknown"}, {"status": "warn"}]
    )
    assert rolled == "warn"
