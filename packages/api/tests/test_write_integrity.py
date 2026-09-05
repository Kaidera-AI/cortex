import copy
import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from conftest import _NotARosterRead, roster_fetch, roster_fetchrow


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_write_integrity_test",
        API_MAIN_PATH,
    )
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


class FakeTransaction:
    def __init__(self, conn=None):
        self.conn = conn

    async def __aenter__(self):
        if self.conn is not None:
            self.conn._begin_transaction()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.conn is not None:
            self.conn._end_transaction(rollback=exc_type is not None)
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class IntegrityConn:
    def __init__(
        self,
        *,
        truncate_decision_summary: bool = False,
        truncate_handoff_summary: bool = False,
        truncate_team_event_summary: bool = False,
        fail_team_event_insert: bool = False,
    ):
        self.truncate_decision_summary = truncate_decision_summary
        self.truncate_handoff_summary = truncate_handoff_summary
        self.truncate_team_event_summary = truncate_team_event_summary
        self.fail_team_event_insert = fail_team_event_insert
        self.decisions: dict[str, dict] = {}
        self.lessons: dict[str, dict] = {}
        self.handoffs: dict[str, dict] = {}
        self.team_events: dict[int, dict] = {}
        self.operations: list[str] = []
        self.advisory_locks: list[str] = []
        self._transaction_snapshots: list[tuple[dict, dict, dict, dict]] = []

    def transaction(self):
        return FakeTransaction(self)

    def _begin_transaction(self):
        self._transaction_snapshots.append(
            (
                copy.deepcopy(self.decisions),
                copy.deepcopy(self.lessons),
                copy.deepcopy(self.handoffs),
                copy.deepcopy(self.team_events),
            )
        )

    def _end_transaction(self, *, rollback: bool):
        snapshot = self._transaction_snapshots.pop()
        if rollback:
            self.decisions, self.lessons, self.handoffs, self.team_events = snapshot

    async def fetchrow(self, sql, *args):
        try:  # E006 Inc04: registry resolver's cortex_projects.metadata read
            return roster_fetchrow(sql, args)
        except _NotARosterRead:
            pass
        if "id::text AS project_id" in sql and "FROM cortex_projects" in sql:
            project = args[0]
            return {
                "project_key": project,
                "project_id": "22222222-2222-4222-8222-222222222222",
                "display_name": project,
                "default_agent": "kai",
                "repo_root": "/tmp/kaidera-os",
                "repo_type": "repo",
                "status": "active",
            }
        if "FROM decisions" in sql:
            row = self.decisions.get(str(args[0]))
            if row and row["project"] == args[1]:
                return row
            return None
        if "FROM lessons" in sql:
            row = self.lessons.get(str(args[0]))
            if row and row["project"] == args[1]:
                return row
            return None
        if "FROM handoffs" in sql and "status = ANY" in sql:
            self.operations.append("dedupe_lookup")
            (
                project,
                open_statuses,
                from_agent,
                from_role,
                to_role,
                to_agent,
                priority,
                summary,
                branch,
                files_changed,
                verification,
                next_steps,
                context,
                parent_goal_id,
                acceptance,
                evidence,
                retry,
                escalation,
            ) = args
            for row in sorted(self.handoffs.values(), key=lambda r: (r.get("created_at") or "", r["id"])):
                if (
                    row.get("project") == project
                    and row.get("status") in set(open_statuses)
                    and row.get("invalidated_at") is None
                    and row.get("from_agent") == from_agent
                    and row.get("from_role") == from_role
                    and row.get("to_role") == to_role
                    and row.get("to_agent") == to_agent
                    and row.get("priority") == priority
                    and row.get("summary") == summary
                    and row.get("branch") == branch
                    and row.get("files_changed") == files_changed
                    and row.get("verification") == verification
                    and row.get("next_steps") == next_steps
                    and row.get("context") == context
                    and row.get("parent_goal_id") == parent_goal_id
                    and row.get("acceptance") == json.loads(acceptance)
                    and row.get("evidence") == json.loads(evidence)
                    and row.get("retry") == json.loads(retry)
                    and row.get("escalation") == json.loads(escalation)
                ):
                    return row
            return None
        if "FROM handoffs" in sql:
            needle = str(args[0])
            row = self.handoffs.get(needle)
            if row is None:
                row = next((candidate for key, candidate in self.handoffs.items() if key.startswith(needle)), None)
            if row and row["project"] == args[1]:
                return row
            return None
        if "FROM team_events" in sql:
            row = self.team_events.get(int(args[0]))
            if row and row["project"] == args[1]:
                return row
            return None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        try:  # E006 Inc04: registry resolver's agents/roles reads
            return roster_fetch(sql, args)
        except _NotARosterRead:
            pass
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "INSERT INTO decisions" in sql:
            row_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
            summary = args[2][:120] if self.truncate_decision_summary else args[2]
            self.decisions[str(row_id)] = {
                "id": str(row_id),
                "project": args[0],
                "agent_name": args[1],
                "summary": summary,
                "category": args[3],
                "metadata": args[-1],
            }
            return row_id
        if "INSERT INTO lessons" in sql:
            row_id = uuid.UUID("11111111-1111-4111-8111-111111111112")
            self.lessons[str(row_id)] = {
                "id": str(row_id),
                "project": args[0],
                "agent_name": args[1],
                "summary": args[2],
                "category": args[3],
                "metadata": args[-1],
            }
            return row_id
        if "INSERT INTO handoffs" in sql:
            self.operations.append("handoff_insert")
            row_id = uuid.UUID(f"22222222-2222-4222-8222-{len(self.handoffs) + 1:012d}")
            summary = args[6][:120] if self.truncate_handoff_summary else args[6]
            self.handoffs[str(row_id)] = {
                "id": str(row_id),
                "project": args[0],
                "from_agent": args[1],
                "from_role": args[2],
                "to_role": args[3],
                "to_agent": args[4],
                "priority": args[5],
                "summary": summary,
                "branch": args[7],
                "files_changed": args[8],
                "verification": args[9],
                "next_steps": args[10],
                "context": args[11],
                "parent_goal_id": args[12],
                "acceptance": json.loads(args[13]),
                "evidence": json.loads(args[14]),
                "retry": json.loads(args[15]),
                "escalation": json.loads(args[16]),
                "status": "pending",
                "invalidated_at": None,
                "created_at": f"2026-06-03T00:00:0{len(self.handoffs) + 1}Z",
            }
            return row_id
        if "INSERT INTO team_events" in sql:
            if self.fail_team_event_insert:
                raise RuntimeError("team event insert failed")
            event_id = len(self.team_events) + 1
            summary = args[3][:120] if self.truncate_team_event_summary else args[3]
            self.team_events[event_id] = {
                "id": event_id,
                "project": args[0],
                "agent_name": args[1],
                "event_type": args[2],
                "summary": summary,
                "detail": args[4],
                "files": args[5],
                "sprint_id": args[6],
                "related_decision_id": args[7],
            }
            return event_id
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def execute(self, sql, *args):
        if "set_config('cortex.project'" in sql:
            return "SELECT 1"
        if "pg_advisory_xact_lock" in sql:
            self.operations.append("dedupe_lock")
            self.advisory_locks.append(str(args[0]))
            return "SELECT 1"
        if "pg_notify" in sql:
            return "SELECT 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")


@pytest.fixture
def cortex_api(monkeypatch):
    module = load_api_module()

    async def fake_embed_text(_text):
        return None

    monkeypatch.setattr(module, "embed_text", fake_embed_text)
    return module


def install_conn(module, conn):
    fake_pool = FakePool(conn)
    module.pool = fake_pool
    module.pool_app = fake_pool
    module.pool_admin = fake_pool
    return conn


@pytest.mark.asyncio
async def test_log_decision_write_fidelity_preserves_long_special_summary(cortex_api):
    conn = install_conn(cortex_api, IntegrityConn())
    summary = "[WRITE-INTEGRITY] " + "quoted \"value\" — apostrophe's value " + ("x" * 260)

    result = await cortex_api.log_event(
        cortex_api.LogRequest(
            event_type="decision",
            summary=summary,
            files_affected=["Program/Release v0.1.0/spec with spaces.md"],
            metadata={"source": "test"},
        ),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result["verified"] is True
    assert result["team_event_id"] == 1
    assert conn.decisions[result["id"]]["summary"] == summary
    assert conn.team_events[1]["summary"] == summary
    assert conn.team_events[1]["files"] == ["Program/Release v0.1.0/spec with spaces.md"]
    assert len(conn.team_events[1]["summary"]) > 200


@pytest.mark.asyncio
async def test_log_generic_write_fidelity_preserves_long_special_summary(cortex_api):
    conn = install_conn(cortex_api, IntegrityConn())
    summary = "[WRITE-INTEGRITY-GENERIC] " + "quoted \"value\" — apostrophe's value " + ("x" * 260)

    result = await cortex_api.log_event(
        cortex_api.LogRequest(
            event_type="bug",
            summary=summary,
            files_affected=["Program/Release v0.1.0/spec with spaces.md"],
            metadata={"source": "test"},
        ),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result == {"logged": True, "id": "1", "event_type": "bug", "verified": True}
    assert conn.team_events[1]["summary"] == summary
    assert conn.team_events[1]["files"] == ["Program/Release v0.1.0/spec with spaces.md"]


@pytest.mark.asyncio
async def test_log_decision_write_fidelity_hard_errors_on_truncated_row(cortex_api):
    install_conn(cortex_api, IntegrityConn(truncate_decision_summary=True))
    summary = "[WRITE-INTEGRITY-FAIL] " + ("x" * 260)

    with pytest.raises(HTTPException) as excinfo:
        await cortex_api.log_event(
            cortex_api.LogRequest(event_type="decision", summary=summary),
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert excinfo.value.status_code == 500
    assert "write fidelity check failed" in excinfo.value.detail
    assert "summary" in excinfo.value.detail


@pytest.mark.asyncio
async def test_log_decision_write_fidelity_hard_errors_on_truncated_team_event(cortex_api):
    install_conn(cortex_api, IntegrityConn(truncate_team_event_summary=True))
    summary = "[WRITE-INTEGRITY-TEAM-EVENT-FAIL] " + ("x" * 260)

    with pytest.raises(HTTPException) as excinfo:
        await cortex_api.log_event(
            cortex_api.LogRequest(event_type="decision", summary=summary),
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert excinfo.value.status_code == 500
    assert "team_event write fidelity check failed" in excinfo.value.detail
    assert "summary" in excinfo.value.detail


@pytest.mark.asyncio
async def test_log_decision_rolls_back_memory_row_when_team_event_insert_fails(cortex_api):
    conn = install_conn(cortex_api, IntegrityConn(fail_team_event_insert=True))
    summary = "[WRITE-INTEGRITY-ATOMIC-FAIL] companion team event failure must roll back"

    with pytest.raises(RuntimeError, match="team event insert failed"):
        await cortex_api.log_event(
            cortex_api.LogRequest(event_type="decision", summary=summary),
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert conn.decisions == {}
    assert conn.team_events == {}


@pytest.mark.asyncio
async def test_log_generic_write_fidelity_hard_errors_on_truncated_team_event(cortex_api):
    install_conn(cortex_api, IntegrityConn(truncate_team_event_summary=True))
    summary = "[WRITE-INTEGRITY-GENERIC-FAIL] " + ("x" * 260)

    with pytest.raises(HTTPException) as excinfo:
        await cortex_api.log_event(
            cortex_api.LogRequest(event_type="bug", summary=summary),
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert excinfo.value.status_code == 500
    assert "team_event write fidelity check failed" in excinfo.value.detail
    assert "summary" in excinfo.value.detail


@pytest.mark.asyncio
async def test_handoff_create_write_fidelity_preserves_full_fields(cortex_api):
    conn = install_conn(cortex_api, IntegrityConn())
    summary = "[WRITE-INTEGRITY-HANDOFF] " + "em dash — quotes \"ok\" " + ("y" * 240)

    result = await cortex_api.create_handoff(
        cortex_api.HandoffCreate(
            from_role="full-stack-developer",
            to_role="full-stack-developer",
            to_agent="ren",
            priority="high",
            summary=summary,
            branch="feature/write-integrity",
            files_changed=[".agents/scripts/cortex-log", ".agents/scripts/cortex-handoff"],
            verification="python3 -m pytest -> passed — exact",
            next_steps="Review \"round-trip\" evidence and complete.",
            context="Context has apostrophe's text and — dash.",
        ),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result["verified"] is True
    assert result["deduped"] is False
    row = conn.handoffs[result["id"]]
    assert row["summary"] == summary
    assert row["verification"] == "python3 -m pytest -> passed — exact"
    assert row["next_steps"] == "Review \"round-trip\" evidence and complete."
    assert row["context"] == "Context has apostrophe's text and — dash."


@pytest.mark.asyncio
async def test_handoff_create_write_fidelity_preserves_execution_policy_fields(cortex_api):
    conn = install_conn(cortex_api, IntegrityConn())
    body = cortex_api.HandoffCreate(
        from_role="pm",
        to_role="knowledge-keeper",
        priority="high",
        summary="Capture execution policy contract",
        acceptance={"criteria": ["tests pass", "docs updated"], "required": True},
        evidence={"required": ["pytest output", "diff summary"]},
        retry={"max_attempts": 2, "backoff": "manual-review"},
        escalation={"after_attempts": 2, "to_role": "lead"},
    )

    result = await cortex_api.create_handoff(body, x_agent="kai", x_project="kaidera-os")

    row = conn.handoffs[result["id"]]
    assert row["acceptance"] == body.acceptance
    assert row["evidence"] == body.evidence
    assert row["retry"] == body.retry
    assert row["escalation"] == body.escalation
    event_detail = json.loads(conn.team_events[1]["detail"])
    assert event_detail["acceptance"] == body.acceptance
    assert event_detail["retry"] == body.retry


@pytest.mark.asyncio
async def test_handoff_create_returns_existing_equal_open_handoff_without_second_insert(cortex_api):
    conn = install_conn(cortex_api, IntegrityConn())
    body = cortex_api.HandoffCreate(
        from_role="pm",
        to_role="knowledge-keeper",
        priority="high",
        summary="bob@kaidera-os capture byte-identical handoff dedupe evidence",
        branch="main",
        files_changed=["notes/dupe.md"],
        verification="same verify bytes",
        next_steps="same next bytes",
        context="same context bytes",
        parent_goal_id="goal-123",
    )

    first = await cortex_api.create_handoff(body, x_agent="kai", x_project="kaidera-os")
    second = await cortex_api.create_handoff(body, x_agent="kai", x_project="kaidera-os")

    assert second["id"] == first["id"]
    assert second["status"] == "pending"
    assert second["deduped"] is True
    assert len(conn.handoffs) == 1
    assert len(conn.team_events) == 1  # duplicate did not emit another created event
    assert conn.operations == [
        "dedupe_lock", "dedupe_lookup", "handoff_insert",
        "dedupe_lock", "dedupe_lookup",
    ]
    assert len(conn.advisory_locks) == 2
    assert conn.advisory_locks[0] == conn.advisory_locks[1]


@pytest.mark.asyncio
async def test_handoff_create_allows_equal_closed_handoff_new_insert(cortex_api):
    conn = install_conn(cortex_api, IntegrityConn())
    body = cortex_api.HandoffCreate(
        from_role="pm",
        to_role="knowledge-keeper",
        priority="high",
        summary="closed duplicates do not block new work",
    )

    first = await cortex_api.create_handoff(body, x_agent="kai", x_project="kaidera-os")
    conn.handoffs[first["id"]]["status"] = "completed"

    second = await cortex_api.create_handoff(body, x_agent="kai", x_project="kaidera-os")

    assert second["id"] != first["id"]
    assert second["deduped"] is False
    assert len(conn.handoffs) == 2
    assert len(conn.team_events) == 2


@pytest.mark.asyncio
async def test_handoff_create_write_fidelity_hard_errors_on_truncated_row(cortex_api):
    install_conn(cortex_api, IntegrityConn(truncate_handoff_summary=True))
    summary = "[WRITE-INTEGRITY-HANDOFF-FAIL] " + ("z" * 260)

    with pytest.raises(HTTPException) as excinfo:
        await cortex_api.create_handoff(
            cortex_api.HandoffCreate(
                to_role="full-stack-developer",
                to_agent="ren",
                summary=summary,
            ),
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert excinfo.value.status_code == 500
    assert "write fidelity check failed" in excinfo.value.detail
    assert "summary" in excinfo.value.detail


@pytest.mark.asyncio
async def test_verify_write_returns_full_handoff_row(cortex_api):
    install_conn(cortex_api, IntegrityConn())
    summary = "[VERIFY-WRITE] full row"
    created = await cortex_api.create_handoff(
        cortex_api.HandoffCreate(
            to_role="full-stack-developer",
            to_agent="ren",
            summary=summary,
            files_changed=["a.py", "b.py"],
        ),
        x_agent="kai",
        x_project="kaidera-os",
    )

    result = await cortex_api.verify_write(
        kind="handoff",
        write_id=created["id"],
        x_project="kaidera-os",
    )

    assert result["verified"] is True
    assert result["row"]["summary"] == summary
    assert result["row"]["files_changed"] == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# REN-ARCH-02: RLS-enforcement detection (fail-loud startup assertion source)
# ---------------------------------------------------------------------------


class FakeRoleConn:
    def __init__(self, row, *, boom=False):
        self._row = row
        self._boom = boom

    async def fetchrow(self, *args, **kwargs):
        if self._boom:
            raise RuntimeError("probe failed")
        return self._row


@pytest.mark.asyncio
async def test_detect_rls_enforced_true_for_restricted_role():
    api = load_api_module()
    pool = FakePool(FakeRoleConn({"rolsuper": False, "rolbypassrls": False}))
    assert await api.detect_rls_enforced(pool) is True


@pytest.mark.asyncio
async def test_detect_rls_enforced_false_for_superuser():
    api = load_api_module()
    pool = FakePool(FakeRoleConn({"rolsuper": True, "rolbypassrls": False}))
    assert await api.detect_rls_enforced(pool) is False


@pytest.mark.asyncio
async def test_detect_rls_enforced_false_for_bypassrls():
    api = load_api_module()
    pool = FakePool(FakeRoleConn({"rolsuper": False, "rolbypassrls": True}))
    assert await api.detect_rls_enforced(pool) is False


@pytest.mark.asyncio
async def test_detect_rls_enforced_none_when_probe_fails():
    api = load_api_module()
    pool = FakePool(FakeRoleConn(None, boom=True))
    assert await api.detect_rls_enforced(pool) is None
