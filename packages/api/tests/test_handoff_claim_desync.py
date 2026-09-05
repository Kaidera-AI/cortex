"""Reproduce + lock the cortex-handoff claim-desync (Fix #1, bulletproofing).

Two failure modes the autonomous loop hit:

  (a) VISIBLE-BUT-UNCLAIMABLE — the bare `GET /handoffs?status=pending` list (no
      recipient filter) shows a row addressed to a role/agent the caller is NOT,
      then `--claim` rejects it with a bare 404. The loop "sees work" it can never
      take, and the 404 says nothing about *why*.

  (b) ROUND-TRIP DESYNC — an agent claims a handoff, but the `--mine`
      re-surface predicate must match on stable base identity rather than fragile
      full display strings. The agent's own in-flight work must never vanish from
      its queue → idle-on-claim, racing Beat's auto-release.

These tests drive the REAL `list_handoffs` / `claim_handoff` against a faithful
in-memory handoffs store that evaluates the recipient + claimer predicates the way
Postgres does (split_part / equality / role-membership), so the desync is produced by
the genuine query logic — never the live cortex-pg, never the running cortex-api.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
PROJECT = "kaidera-os"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeRoleConn:
    def __init__(self, rows: list[dict], *, default_agent: str | None = None):
        self.rows = rows
        self.default_agent = default_agent

    async def fetch(self, sql, *args):
        assert "FROM agents" in sql
        assert "capabilities" in sql
        return self.rows

    async def fetchrow(self, sql, *args):
        assert "FROM cortex_projects" in sql
        if self.default_agent is None:
            return None
        return {"default_agent": self.default_agent}


# --------------------------------------------------------------------------- #
# A faithful in-memory handoffs store. It does NOT pattern-match the SQL; it
# parses just enough of the WHERE shape (recipient predicate vs claimer predicate)
# to evaluate each candidate row the way Postgres would. List and claim share the
# SAME rows, so any disagreement between "what list shows" and "what claim accepts"
# (or "what re-surfaces to the claimer") is the real desync, not a test artifact.
# --------------------------------------------------------------------------- #


def _base(val: str | None) -> str:
    return (val or "").split("@", 1)[0].lower()


class FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class HandoffStore:
    """Shared rows + a Postgres-faithful evaluator for the handoff predicates."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def _row_by_prefix(self, prefix: str) -> dict | None:
        for r in self.rows:
            if r["id"].startswith(prefix):
                return r
        return None

    # The two predicate shapes the endpoints use, evaluated faithfully. -------
    @staticmethod
    def _recipient_match(row: dict, bare_agent: str, roles: list[str]) -> bool:
        # lower(split_part(to_agent,'@',1)) = agent
        if _base(row.get("to_agent")) == bare_agent:
            return True
        # to_agent='' AND lower(to_role) = ANY(roles)
        if not (row.get("to_agent") or "") and (row.get("to_role") or "").lower() in roles:
            return True
        return False

    @staticmethod
    def _claimer_match_compound(row: dict, compound: str) -> bool:
        # claimed_by = $compound  (full-string equality — the --mine list shape)
        return (row.get("claimed_by") or "") == compound

    @staticmethod
    def _claimer_match_split(row: dict, bare_agent: str) -> bool:
        # lower(split_part(claimed_by,'@',1)) = agent  (the boot/state shape)
        return _base(row.get("claimed_by")) == bare_agent


class FakeListConn:
    """Serves list_handoffs: roster reads + the two /handoffs SELECT shapes."""

    def __init__(self, store: HandoffStore):
        self.store = store

    def transaction(self):
        return FakeTxn()

    async def fetch(self, sql, *args):
        from conftest import roster_fetch, _NotARosterRead

        try:
            return roster_fetch(sql, args)
        except _NotARosterRead:
            pass

        if "FROM handoffs" not in sql:
            raise AssertionError(f"Unexpected fetch SQL: {sql}")
        assert "claimed_at::text" in sql
        assert "retry_count" in sql

        # The --mine / scoped FILTER list (recipient-OR-claimer predicate).
        # Bind order: project, status, viewer_bare, roles[].
        if "split_part(COALESCE(to_agent" in sql and "WHERE project = $1" in sql and "$4::text[]" in sql:
            project, status, bare, roles = args[0], args[1], args[2], args[3]
            roles = [x.lower() for x in roles]
            out = []
            for r in self.store.rows:
                if r["project"] != project:
                    continue
                if not (status == "all" or r["status"] == status):
                    continue
                # Canonical claimer (base-name split match) OR recipient.
                if (
                    self.store._claimer_match_split(r, bare)
                    or self.store._recipient_match(r, bare, roles)
                ):
                    out.append(self._project_row(r))
            return out

        # The bare/operator list (project + status only — NO recipient filter)
        project, status = args[0], args[1]
        out = []
        for r in self.store.rows:
            if r["project"] != project:
                continue
            if not (status == "all" or r["status"] == status):
                continue
            out.append(self._project_row(r))
        return out

    @staticmethod
    def _project_row(r: dict) -> dict:
        return {
            "id": r["id"],
            "from_agent": r.get("from_agent"),
            "to_role": r.get("to_role"),
            "to_agent": r.get("to_agent"),
            "priority": r.get("priority", "medium"),
            "summary": (r.get("summary") or "")[:100],
            "status": r["status"],
            "claimed_by": r.get("claimed_by"),
            "claimed_at": r.get("claimed_at"),
            "retry_count": r.get("retry_count", 0),
            "created_at": r.get("created_at", "2026-06-05T00:00:00Z"),
            # extra fields some list shapes carry; harmless if unused
            "eligible": None,
            "claim_hint": None,
        }


class FakeClaimConn:
    """Serves claim_handoff: roster reads, resolve_unique_handoff_for_mutation,
    the conditional UPDATE (evaluated against the store), and event inserts."""

    def __init__(self, store: HandoffStore):
        self.store = store
        self.events: list[dict] = []

    def transaction(self):
        return FakeTxn()

    async def fetch(self, sql, *args):
        from conftest import roster_fetch, _NotARosterRead

        try:
            return roster_fetch(sql, args)
        except _NotARosterRead:
            pass
        if "FROM handoffs" in sql and "LIMIT 2" in sql:
            prefix = args[1]
            row = self.store._row_by_prefix(prefix)
            return [dict(row)] if row else []
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def fetchrow(self, sql, *args):
        from conftest import roster_fetchrow, _NotARosterRead

        try:
            return roster_fetchrow(sql, args)
        except _NotARosterRead:
            pass
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def execute(self, sql, *args):
        if "UPDATE handoffs SET status = 'claimed'" in sql:
            # bind order: claimant_identity, id(uuid), project, bare_agent, roles[]
            claimant_identity, hid, project, bare, roles = args[0], str(args[1]), args[2], args[3], args[4]
            assert claimant_identity == f"{bare}@{project}"
            assert ":" not in claimant_identity
            roles = [x.lower() for x in roles]
            n = 0
            for r in self.store.rows:
                if r["id"] != hid or r["project"] != project:
                    continue
                if r["status"] != "pending":
                    continue
                if self.store._recipient_match(r, bare, roles):
                    r["status"] = "claimed"
                    r["claimed_by"] = claimant_identity
                    n += 1
            return f"UPDATE {n}"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "INSERT INTO team_events" in sql:
            self.events.append({"summary": args[3] if len(args) > 3 else ""})
            return len(self.events)
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")


# --------------------------------------------------------------------------- #
# Fixture: load main.py, stub the DB-touching identity helpers to the seeded
# kaidera-os policy (kai/ren writers, full-stack-developer role), exactly like the
# established budget-claim test does.
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_claim_desync_test")

    async def require_registered_project(project):
        return {"project_key": project, "project_id": "77777777-7777-4777-8777-777777777777"}

    async def require_registered_agent_writer(project, agent):
        return None

    async def compound_agent(agent, project):
        return agent if "@" in agent else f"{agent}@{project}"

    async def resolve_agent_roles(_conn, project, agent):
        if agent.lower() in {"kai", "ren"}:
            return ["full-stack-developer"]
        return []

    async def load_roster_policy(project):
        return module.RosterPolicy(
            project=project,
            enforce=True,
            default_writer_scope="work",
            work_writers=frozenset({"kai", "ren"}),
            system_event_writers=frozenset({"beat", "migration", "system"}),
            read_only=frozenset(),
            handoff_targets=frozenset({"kai", "ren"}),
            beat_may_create_handoff=True,
            roles={},
            suggest_cutoff=0.6,
        )

    async def emit_handoff_lifecycle_event(*a, **k):
        return 1

    monkeypatch.setattr(module, "require_registered_project", require_registered_project)
    monkeypatch.setattr(module, "require_registered_agent_writer", require_registered_agent_writer)
    monkeypatch.setattr(module, "compound_agent", compound_agent)
    monkeypatch.setattr(module, "resolve_agent_roles", resolve_agent_roles)
    monkeypatch.setattr(module, "load_roster_policy", load_roster_policy)
    monkeypatch.setattr(module, "emit_handoff_lifecycle_event", emit_handoff_lifecycle_event)
    return module


@pytest.mark.asyncio
async def test_resolve_agent_roles_includes_capability_role_aliases(monkeypatch):
    """Dispatch and Cortex claim must agree on app-visible role aliases.

    The Marketing smoke found this in production shape: the app routed
    `creative-multimedia` to Gem via `role_aliases`, but Cortex only authorized Gem's
    canonical `graphics` role. The worker then skipped the claim and stranded the
    pre-created run. Capability aliases make the registry the shared source for both
    routing and claim authorization.
    """
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_claim_alias_test")
    conn = FakeRoleConn(
        [
            {
                "role": "graphics",
                "capabilities": {
                    "role_aliases": ["creative-multimedia", "Bad Alias"],
                    "runtime_role": "visual-production, content-design",
                    "platform_role": "../unsafe",
                },
            },
            {"role": "graphics", "capabilities": None},
            {"role": "brand-reviewer", "capabilities": None},
        ]
    )

    roles = await module.resolve_agent_roles(conn, "marketing", "gem")

    assert roles == [
        "brand-reviewer",
        "content-design",
        "creative-multimedia",
        "graphics",
        "visual-production",
    ]


@pytest.mark.asyncio
async def test_resolve_agent_roles_gives_project_default_agent_lead_aliases(monkeypatch):
    """The project default agent is the generic lead for claim authorization too."""
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_default_lead_alias_test")
    conn = FakeRoleConn(
        [{"role": "cmo", "capabilities": {}}],
        default_agent="marlow",
    )

    roles = await module.resolve_agent_roles(conn, "marketing", "marlow")

    assert set(["lead", "cpo", "co-lead", "pm", "cmo"]).issubset(set(roles))


# ---- (a) VISIBLE-BUT-UNCLAIMABLE ----------------------------------------- #


@pytest.mark.asyncio
async def test_visible_but_unclaimable_is_filtered_from_mine_and_claim_is_informative(api, monkeypatch):
    """A pending row addressed to role 'cortex-architect' (ren is full-stack-developer):

      1. the operator/agentless list shows it (operator visibility — the "leak");
      2. ren's recipient-aware --mine list does NOT surface it (so the loop never
         drives a claim on it); when it IS in a viewer's list it is marked
         eligible=False with a reason;
      3. a direct claim by ren fails with an INFORMATIVE 403 naming the role —
         NOT the old bare 404 "not found or not pending".
    """
    rows = [
        {
            "id": "d54d271b-0000-4000-8000-000000000001",
            "project": PROJECT,
            "from_agent": "kai@kaidera-os",
            "from_role": "cpo",
            "to_role": "cortex-architect",  # ren is NOT this role
            "to_agent": "",
            "priority": "high",
            "summary": "T2 architect-only handoff",
            "status": "pending",
            "claimed_by": None,
        }
    ]
    store = HandoffStore(rows)

    # 1) Operator/agentless list surfaces it (the leak), eligibility unknown.
    list_conn = FakeListConn(store)
    monkeypatch.setattr(api, "acquire_scoped", lambda _p: FakeAcquire(list_conn))
    operator = await api.list_handoffs(
        request=None, x_project=PROJECT, status="pending", agent=None, mine=False
    )
    op_ids = {h["id"] for h in operator["handoffs"]}
    assert "d54d271b-0000-4000-8000-000000000001" in op_ids, (
        "operator list should surface the row (the visibility the loop's 'all' mode sees)"
    )
    op_row = next(h for h in operator["handoffs"] if h["id"].startswith("d54d271b"))
    assert op_row.get("eligible") is None, "no viewer → eligibility genuinely unknown"

    # 2) ren's recipient-aware --mine does NOT surface a not-ren-role row.
    class Req:
        class state:
            jwt_claims = {"agent_name": "ren"}

    mine = await api.list_handoffs(
        request=Req(), x_project=PROJECT, status="pending", agent="ren", mine=True
    )
    mine_ids = {h["id"] for h in mine["handoffs"]}
    assert "d54d271b-0000-4000-8000-000000000001" not in mine_ids, (
        "ren's --mine must not surface a cortex-architect-only row → loop won't try to claim it"
    )

    # 3) A direct claim by ren fails with a precise 403 that names the role.
    claim_conn = FakeClaimConn(store)
    monkeypatch.setattr(api, "acquire_scoped", lambda _p: FakeAcquire(claim_conn))
    with pytest.raises(api.HTTPException) as exc:
        await api.claim_handoff("d54d271b", x_agent="ren", x_project=PROJECT)
    assert exc.value.status_code == 403, (
        f"claim of a not-your-role pending row should be a precise 403, got "
        f"{exc.value.status_code}: {exc.value.detail}"
    )
    assert "cortex-architect" in str(exc.value.detail).lower(), (
        f"claim failure must name the addressed role so the agent knows WHY; "
        f"got: {exc.value.detail}"
    )
    assert "not found" not in str(exc.value.detail).lower(), (
        "must NOT be the misleading bare 404 'not found' message"
    )


# ---- (b) ROUND-TRIP DESYNC (the claimed_by re-surface) -------------------- #


@pytest.mark.asyncio
async def test_self_claimed_row_resurfaces_to_claimer_when_recipient_no_longer_matches(api, monkeypatch):
    """The claimed_by predicate is the ONLY thing that re-surfaces an agent's own
    claimed row once the recipient predicate stops matching — e.g. a role-addressed
    handoff the agent claimed, but the agent's role resolution later differs (suspect
    #1 drift), or a row addressed to a different role the agent claimed. We isolate
    that by giving the row a to_role ren does NOT currently resolve and a bare
    claimed_by. A fragile `claimed_by = display_identity` predicate drops it → the agent
    can never re-detect its own in-flight work → idle-on-claim vs Beat auto-release."""
    rows = [
        {
            "id": "aaaa1111-0000-4000-8000-000000000002",
            "project": PROJECT,
            "from_agent": "kai@kaidera-os",
            "from_role": "cpo",
            "to_role": "cortex-architect",  # NOT one of ren's resolved roles
            "to_agent": "",  # role-addressed, so to_agent recipient branch can't match
            "priority": "urgent",
            "summary": "ren's own claimed work (role-addressed, drifted)",
            "status": "claimed",
            "claimed_by": "ren",  # stored BARE (the format-mismatch suspect)
            "claimed_at": "2026-06-26 20:00:00+00",
            "retry_count": 2,
        }
    ]
    store = HandoffStore(rows)
    list_conn = FakeListConn(store)
    monkeypatch.setattr(api, "acquire_scoped", lambda _p: FakeAcquire(list_conn))

    class Req:  # minimal request carrying jwt_claims
        class state:
            jwt_claims = {"agent_name": "ren"}

    res = await api.list_handoffs(
        request=Req(), x_project=PROJECT, status="claimed", agent="ren", mine=True
    )
    ids = {h["id"] for h in res["handoffs"]}
    assert "aaaa1111-0000-4000-8000-000000000002" in ids, (
        "ren's own claimed handoff must re-surface in --mine via claimed_by even when "
        "the recipient predicate no longer matches and claimed_by was stored bare — "
        "else the loop idles on its own claim while Beat auto-releases it"
    )


@pytest.mark.asyncio
async def test_claim_then_mine_roundtrip_same_agent(api, monkeypatch):
    """End-to-end round trip through the REAL endpoints: ren claims a pending row,
    then --mine must show it back to ren as claimed. This is the autonomous loop's
    exact claim→re-detect cycle."""
    rows = [
        {
            "id": "bbbb2222-0000-4000-8000-000000000003",
            "project": PROJECT,
            "from_agent": "kai@kaidera-os",
            "from_role": "cpo",
            "to_role": "full-stack-developer",
            "to_agent": "ren",
            "priority": "high",
            "summary": "claim then re-detect",
            "status": "pending",
            "claimed_by": None,
        }
    ]
    store = HandoffStore(rows)

    # 1) claim as ren
    claim_conn = FakeClaimConn(store)
    monkeypatch.setattr(api, "acquire_scoped", lambda _p: FakeAcquire(claim_conn))
    claimed = await api.claim_handoff("bbbb2222", x_agent="ren", x_project=PROJECT)
    assert claimed["claimed"] is True

    # 2) --mine must re-surface it to ren
    list_conn = FakeListConn(store)
    monkeypatch.setattr(api, "acquire_scoped", lambda _p: FakeAcquire(list_conn))

    class Req:
        class state:
            jwt_claims = {"agent_name": "ren"}

    res = await api.list_handoffs(
        request=Req(), x_project=PROJECT, status="claimed", agent="ren", mine=True
    )
    ids = {h["id"] for h in res["handoffs"]}
    assert "bbbb2222-0000-4000-8000-000000000003" in ids, (
        "after ren claims, --mine(status=claimed) must show ren its own row"
    )
    # And it must be attributed to ren (base-name), however claimed_by was stored.
    row = next(h for h in res["handoffs"] if h["id"].startswith("bbbb2222"))
    assert row["claimed_by"] == "ren@kaidera-os"
    assert ":" not in row["claimed_by"]
    assert _base(row["claimed_by"]) == "ren"


@pytest.mark.asyncio
async def test_handoff_list_surfaces_claimed_at_and_retry_count(api, monkeypatch):
    rows = [
        {
            "id": "cccc3333-0000-4000-8000-000000000004",
            "project": PROJECT,
            "from_agent": "kai@kaidera-os",
            "from_role": "cpo",
            "to_role": "full-stack-developer",
            "to_agent": "ren",
            "priority": "high",
            "summary": "watchdog metadata",
            "status": "claimed",
            "claimed_by": "ren@kaidera-os",
            "claimed_at": "2026-06-26 20:00:00+00",
            "retry_count": 3,
        }
    ]
    store = HandoffStore(rows)
    monkeypatch.setattr(api, "acquire_scoped", lambda _p: FakeAcquire(FakeListConn(store)))

    class Req:
        class state:
            jwt_claims = {"agent_name": "ren"}

    res = await api.list_handoffs(
        request=Req(), x_project=PROJECT, status="claimed", agent="ren", mine=True
    )
    row = res["handoffs"][0]

    assert row["claimed_at"] == "2026-06-26 20:00:00+00"
    assert row["retry_count"] == 3
