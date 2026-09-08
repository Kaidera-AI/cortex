"""Real return endpoint; finite predicate-aware storage, not a PostgreSQL proof.

The old routing fixture supplies identity reads and event serialization only.
Handoff reads return detached snapshots. The five UPDATE shapes honor id,
project and status predicates; injected command tags model an anomalous write
result, not a claim that a UUID primary key can normally match two rows.
Transactions restore stored rows/receipts on failure. Attempt journals never
roll back, so an event emitted before an eventual refusal cannot look safe.
No startup, service, subprocess, credentials or network is selected here.
"""

import asyncio
import base64
from copy import deepcopy
import hashlib
import hmac
import json

import httpx
import pytest

import test_handoff_return_routes as routes


api_module = routes.api_module
PARENT_ID = routes.PARENT_ID
HANDBACK_ID = routes.HANDBACK_ID
PROJECT = "kaidera-os"
NONCLAIMED = ("pending", "returned", "released", "abandoned", "failed", "archived")
SITES = ("child_complete", "parent_rework", "parent_accept", "human_complete", "task_return")


class Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.before = self.conn.snapshot()
        return self

    async def __aexit__(self, kind, exc, tb):
        if kind is not None:
            self.conn.rows, self.conn.events, self.conn.handback_insert_count = self.before
        return False


class PredicateConn(routes.FakeReturnConn):
    def __init__(self, parent=None, **kwargs):
        super().__init__(deepcopy(parent) if parent is not None else None, **kwargs)
        self.events = [{"event_type": "historical_receipt", "detail": {"preserve": True}}]
        self.event_attempts = []
        self.update_attempts = []
        self.lock_reads = []
        self.insert_attempts = 0
        self.bad_site = None
        self.bad_tag = None
        self.missing_insert_id = False

    def snapshot(self):
        return deepcopy((self.rows, self.events, self.handback_insert_count))

    def transaction(self):
        return Transaction(self)

    async def fetch(self, sql, *args):
        return deepcopy(await super().fetch(sql, *args))

    def active_review(self, project, parent_id):
        return next((row for row in self.rows.values()
                     if row["project"] == project
                     and row["kind"] == "completion_handback"
                     and row["reply_to_handoff_id"] == parent_id
                     and row.get("invalidated_at") is None
                     and row["status"] in {"pending", "claimed"}), None)

    async def fetchrow(self, sql, *args):
        query = " ".join(sql.split())
        if "FROM handoffs" in query and "FOR UPDATE" in query:
            assert "WHERE id = $1::uuid AND project = $2" in query
            row = self.rows.get(str(args[0]))
            self.lock_reads.append(str(args[0]))
            return deepcopy(row) if row and row["project"] == args[1] else None
        if "kind = 'completion_handback'" in query and "LIMIT 1" in query:
            assert "WHERE project = $1" in query
            assert "reply_to_handoff_id = $2::uuid" in query
            assert "invalidated_at IS NULL" in query
            assert "status IN ('pending', 'claimed')" in query
            row = self.active_review(*args[:2])
            return {"id": row["id"], "status": row["status"]} if row else None
        return deepcopy(await super().fetchrow(sql, *args))

    async def execute(self, sql, *args):
        query = " ".join(sql.split())
        if not query.startswith("UPDATE handoffs"):
            return await super().execute(sql, *args)
        if "completion_report = $1" in query:
            row_id, project = str(args[1]), args[2]
            expected_status = "claimed"
            if "SET status = 'returned'" in query:
                site = "task_return"
            elif "returned_at = NOW()" in query:
                site = "human_complete"
            else:
                site = "child_complete"
            assert "WHERE id = $2::uuid AND project = $3 AND status = 'claimed'" in query
        else:
            row_id, project = str(args[0]), args[1]
            expected_status = "returned"
            site = "parent_rework" if "retry_count" in query else "parent_accept"
            assert "WHERE id = $1::uuid AND project = $2 AND status = 'returned'" in query
        row = self.rows.get(row_id)
        matches = bool(row and row["project"] == project and row["status"] == expected_status)
        before = deepcopy(row)
        tag = self.bad_tag if site == self.bad_site else ("UPDATE 1" if matches else "UPDATE 0")
        # A zero-row result changes nothing. Other anomalous tags deliberately
        # leave an earlier/local write for the enclosing transaction to undo.
        if matches and tag != "UPDATE 0":
            await super().execute(sql, *args)
        self.update_attempts.append({"site": site, "tag": tag,
                                     "before": before, "after": deepcopy(row)})
        return tag

    async def fetchval(self, sql, *args):
        if "INSERT INTO handoffs" in sql:
            self.insert_attempts += 1
            if self.missing_insert_id:
                return None
            existing = self.active_review(*args[:2])
            if existing is not None:
                return existing["id"]
        return await super().fetchval(sql, *args)


def review_row(**changes):
    return routes.task_row(
        **{"id": HANDBACK_ID, "kind": "completion_handback",
           "reply_to_handoff_id": PARENT_ID, "from_agent": "kai@kaidera-os",
           "to_agent": "ren@kaidera-os", "claimed_by": "ren@kaidera-os",
           "invalidated_at": None, **changes}
    )


def reviewing(parent_status="returned", **child_changes):
    conn = PredicateConn(routes.task_row(
        status=parent_status, returned_at="original-return",
        completion_report={"outcome": "completed", "summary": "original receipt"},
    ))
    conn.rows[HANDBACK_ID] = review_row(**child_changes)
    return conn


@pytest.fixture
def bind(api_module, monkeypatch):
    original_emit = api_module.emit_handoff_lifecycle_event

    def install(conn):
        monkeypatch.setattr(api_module, "acquire_scoped", lambda project: routes.FakeAcquire(conn))

        async def observed_emit(checked_conn, **kwargs):
            assert checked_conn is conn
            conn.event_attempts.append(deepcopy(kwargs))
            return await original_emit(checked_conn, **kwargs)

        monkeypatch.setattr(api_module, "emit_handoff_lifecycle_event", observed_emit)
        return conn

    return install


async def invoke(api, *, child=False, decision=None, actor=None):
    return await api.return_handoff(
        HANDBACK_ID if child else PARENT_ID,
        routes.report(api, decision=decision), request=routes.http_request(),
        x_agent=actor or ("ren" if child else "kai"), x_project=PROJECT,
    )


async def refused(api, conn, *, status=409, **call):
    before = conn.snapshot()
    attempts = deepcopy(conn.event_attempts)
    with pytest.raises(api.HTTPException) as caught:
        await invoke(api, **call)
    assert caught.value.status_code == status
    assert isinstance(caught.value.detail, str) and caught.value.detail.strip()
    assert conn.snapshot() == before
    assert conn.event_attempts == attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("child_status", NONCLAIMED)
@pytest.mark.parametrize("decision", ("accept", "rework"))
async def test_nonclaimed_review_refused_before_parent_read(api_module, bind, child_status, decision):
    conn = bind(reviewing(status=child_status))
    await refused(api_module, conn, child=True, decision=decision)
    assert conn.lock_reads == [HANDBACK_ID]
    assert conn.update_attempts == []
    assert conn.insert_attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("tag", ("UPDATE 0", "UPDATE 2", "UPDATE 01", None))
async def test_each_required_update_needs_exact_one_row(api_module, bind, site, tag):
    call = {}
    if site in {"child_complete", "parent_rework", "parent_accept"}:
        conn = reviewing()
        call = {"child": True, "decision": "rework" if site == "parent_rework" else "accept"}
    elif site == "human_complete":
        conn = PredicateConn(routes.task_row(
            from_agent="ren@kaidera-os", to_agent="ren@kaidera-os",
            claimed_by="ren@kaidera-os"), actors={"ren": ["human"]})
        call = {"actor": "ren"}
    else:
        conn = PredicateConn()
    conn.bad_site, conn.bad_tag = site, tag
    bind(conn)
    await refused(api_module, conn, **call)
    expected = ["child_complete", site] if site.startswith("parent_") else [site]
    assert [attempt["site"] for attempt in conn.update_attempts] == expected
    assert conn.update_attempts[-1]["tag"] == tag
    if site.startswith("parent_"):
        # The child really changed before the parent fault, then was rolled back.
        assert conn.update_attempts[0]["after"]["status"] == "completed"
        assert conn.rows[HANDBACK_ID]["status"] == "claimed"
    assert conn.insert_attempts == 0


@pytest.mark.asyncio
async def test_missing_insert_receipt_refuses_and_rolls_back_parent(api_module, bind):
    conn = bind(PredicateConn())
    conn.missing_insert_id = True
    await refused(api_module, conn)
    assert conn.insert_attempts == 1
    assert conn.update_attempts[0]["after"]["status"] == "returned"
    assert conn.rows[PARENT_ID]["status"] == "claimed"
    assert HANDBACK_ID not in conn.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("child_status,invalidated,child_project", [
    (None, None, PROJECT),
    ("pending", "invalidated-receipt", PROJECT),
    ("claimed", "invalidated-receipt", PROJECT),
    *((status, None, PROJECT) for status in ("returned", "completed", "released", "abandoned", "failed", "archived")),
    ("pending", None, "another-project"),
])
async def test_returned_parent_without_active_review_is_not_delivery(api_module, bind, child_status, invalidated, child_project):
    conn = reviewing()
    if child_status is None:
        del conn.rows[HANDBACK_ID]
    else:
        conn.rows[HANDBACK_ID].update(status=child_status, invalidated_at=invalidated, project=child_project)
    bind(conn)
    await refused(api_module, conn)
    assert conn.update_attempts == []
    assert conn.insert_attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ("accept", "rework"))
@pytest.mark.parametrize("parent_state,status", (("archived", 409), ("absent", 404), ("foreign", 404)))
async def test_unavailable_parent_never_closes_review(api_module, bind, decision, parent_state, status):
    conn = reviewing(parent_status="archived" if parent_state == "archived" else "returned")
    if parent_state == "absent":
        del conn.rows[PARENT_ID]
    elif parent_state == "foreign":
        conn.rows[PARENT_ID]["project"] = "another-project"
    bind(conn)
    await refused(api_module, conn, child=True, decision=decision, status=status)
    assert conn.rows[HANDBACK_ID]["status"] == "claimed"
    assert conn.insert_attempts == 0


@pytest.mark.asyncio
async def test_healthy_task_return_and_dedup_keep_original_receipts(api_module, bind):
    conn = bind(PredicateConn())
    first = await invoke(api_module)
    before, attempts = conn.snapshot(), deepcopy(conn.event_attempts)
    second = await invoke(api_module)
    assert first["status"] == "returned" and first["deduped"] is False
    assert first["handback_id"] == HANDBACK_ID
    assert second["deduped"] is True and second["handback_id"] == HANDBACK_ID
    assert conn.snapshot() == before and conn.event_attempts == attempts
    assert conn.rows[HANDBACK_ID]["to_agent"] == "ren@kaidera-os"
    assert [event["action"] for event in attempts] == ["returned", "created"]
    assert conn.insert_attempts == conn.handback_insert_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,parent_state,actions", [
    ("accept", "completed", ["accepted", "completed"]),
    ("rework", "pending", ["rework_requested", "completed"]),
])
async def test_healthy_review_and_completed_replay_preserve_decision(api_module, bind, decision, parent_state, actions):
    conn = bind(reviewing())
    first = await invoke(api_module, child=True, decision=decision)
    assert first["deduped"] is False
    assert conn.rows[PARENT_ID]["status"] == parent_state
    assert conn.rows[HANDBACK_ID]["status"] == "completed"
    assert conn.rows[HANDBACK_ID]["completion_report"]["decision"] == decision
    assert conn.rows[PARENT_ID]["retry_count"] == (1 if decision == "rework" else 0)
    assert [event["action"] for event in conn.event_attempts] == actions
    before, attempts = conn.snapshot(), deepcopy(conn.event_attempts)
    reads = list(conn.lock_reads)
    # Replay must come before current claimant/parent-state checks. Even a
    # different caller and opposite new decision replay the stored decision.
    replay = await invoke(api_module, child=True, actor="kai",
                          decision="rework" if decision == "accept" else "accept")
    assert replay["deduped"] is True
    assert replay["status"] == parent_state
    assert replay.get("accepted") is (True if decision == "accept" else None)
    assert replay.get("rework_requested") is (True if decision == "rework" else None)
    assert conn.snapshot() == before and conn.event_attempts == attempts
    assert conn.lock_reads == reads + [HANDBACK_ID]
    assert conn.insert_attempts == 0


@pytest.mark.asyncio
async def test_claimed_accept_with_already_completed_parent_is_deduped(api_module, bind):
    conn = bind(reviewing(parent_status="completed"))
    result = await invoke(api_module, child=True, decision="accept")
    assert result["accepted"] is True and result["deduped"] is True
    assert [attempt["site"] for attempt in conn.update_attempts] == ["child_complete"]
    assert conn.rows[HANDBACK_ID]["status"] == "completed"


@pytest.mark.asyncio
async def test_completed_task_replay_does_not_create_review(api_module, bind):
    conn = bind(PredicateConn(routes.task_row(status="completed")))
    before = conn.snapshot()
    result = await invoke(api_module, actor="ren")
    assert result["accepted"] is True and result["deduped"] is True
    assert result["handback_id"] is None
    assert conn.snapshot() == before
    assert conn.event_attempts == conn.update_attempts == []
    assert conn.insert_attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("human", "agent", "system", None))
async def test_only_human_self_return_can_auto_accept(api_module, bind, kind):
    conn = bind(PredicateConn(routes.task_row(
        from_agent="ren@kaidera-os", to_agent="ren@kaidera-os",
        claimed_by="ren@kaidera-os"), actors={"ren": [kind] if kind else []}))
    if kind != "human":
        await refused(api_module, conn, actor="ren")
        assert conn.update_attempts == []
    else:
        result = await invoke(api_module, actor="ren")
        assert result["auto_accepted"] is True and result["handback_id"] is None
        assert conn.rows[PARENT_ID]["status"] == "completed"
        assert [event["action"] for event in conn.event_attempts] == ["auto_accepted"]
    assert conn.insert_attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_self_review", (False, True))
async def test_agent_cannot_accept_own_or_human_only_review(api_module, bind, legacy_self_review):
    conn = reviewing()
    conn.actors = {"ren": ["agent"]}
    if legacy_self_review:
        conn.rows[PARENT_ID]["completion_report"]["metadata"] = {"return_routing": "self_review_required"}
    else:
        conn.rows[HANDBACK_ID]["from_agent"] = "ren@kaidera-os"
    bind(conn)
    await refused(api_module, conn, child=True, decision="accept", status=403)
    assert conn.update_attempts == []
    assert conn.insert_attempts == 0


@pytest.mark.asyncio
async def test_unsupported_approval_still_refused_without_mutation(api_module, bind):
    conn = reviewing()
    conn.rows[PARENT_ID]["acceptance"]["approval_policy"] = {"mode": "multi_lead", "min_approvers": 2}
    bind(conn)
    await refused(api_module, conn, child=True, decision="accept")
    assert conn.update_attempts == []
    assert conn.insert_attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("orphan", (False, True))
async def test_http_return_reports_healthy_delivery_or_orphan_conflict(api_module, bind, monkeypatch, orphan):
    conn = bind(PredicateConn(routes.task_row(status="returned" if orphan else "claimed")))
    before = conn.snapshot()
    # Use the unchanged real JWT middleware with a synthetic signing key, not
    # an auth bypass. ASGITransport does not run lifespan or open a socket.
    key = "synthetic-atomicity-jwt-key"
    monkeypatch.setattr(api_module, "CORTEX_AUTH_REQUIRE_JWT", True)
    monkeypatch.setattr(api_module, "CORTEX_JWT_SECRET", key)
    encoded = [base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")
               for value in ({"alg": "HS256"}, {"agent_name": "kai", "project": PROJECT})]
    signed = b".".join(encoded)
    signature = base64.urlsafe_b64encode(hmac.digest(key.encode(), signed, hashlib.sha256)).rstrip(b"=")
    token = (signed + b"." + signature).decode()
    async with asyncio.timeout(2):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app),
                                     base_url="https://cortex.invalid") as client:
            result = await client.post(f"/handoffs/{PARENT_ID}/return", headers={
                "X-Agent-Name": "kai", "X-Project": PROJECT, "Authorization": f"Bearer {token}",
            }, json=routes.report(api_module).model_dump())
    assert result.status_code == (409 if orphan else 200)
    if orphan:
        assert result.json()["detail"]
        assert conn.snapshot() == before
        assert conn.event_attempts == conn.update_attempts == []
        assert conn.insert_attempts == 0
    else:
        assert result.json()["handback_id"] == HANDBACK_ID
        assert result.json()["status"] == "returned"
        assert [event["action"] for event in conn.event_attempts] == ["returned", "created"]
        assert conn.rows[PARENT_ID]["status"] == "returned"
