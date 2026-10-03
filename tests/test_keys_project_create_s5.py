"""S5/S5c contract: private atomic create, honest replay, and preserved registry.

Kai r31/r25 and design53 r2.6. TestClient exercises the private control
factory; it does not prove an OS peer, KOS profile or recipient-store boundary.
Those remain separate integrated acceptance gates. Every DB URL is supplied
by the isolated test harness; this file has no localhost/live-DB fallback.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from fastapi.testclient import TestClient

from cortex_v2 import identity
from cortex_v2.app import create_app, create_bootstrap_app
from cortex_v2.store import ApiProblem, token_digest
from test_keys_create_right import _allow_create
from test_keys_issuance_t21 import _settings
from test_keys_project_authority import _app, _seed, _url

pytestmark = pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL")
    or not os.getenv("CORTEX_V2_D53_MIGRATOR_DATABASE_URL"),
    reason="explicit isolated pgvector database required; no default DB",
)


async def _credential(principal: uuid.UUID, pepper: bytes, token: str) -> None:
    conn = await asyncpg.connect(_url("MIGRATOR"))
    try:
        await conn.execute(
            "INSERT INTO cortex_auth.credentials(credential_id,principal_id,token_hash,"
            "generation,expires_at) VALUES($1,$2,$3,1,now()+interval '365 days')",
            uuid.uuid4(), principal, token_digest(token, pepper),
        )
    finally:
        await conn.close()


def _request(source: str | None) -> dict:
    key = "s5-" + uuid.uuid4().hex[:16]
    return {
        "source_project": source, "project_key": key,
        "display_name": "S5 native project", "lead_name": "mia",
        "lead_responsibility": "Lead the project and review its work",
        "lead_key_manager": "user", "with_console": True,
        "parent_project_key": None, "repo_root": "/s5/" + key,
        "roots": [
            {"path": "/s5/" + key, "kind": "primary", "repo_type": "git"},
            {"path": "/s5/shared", "kind": "reference", "label": "shared originals"},
        ],
    }


def _headers(token: str, key: str | None = None) -> dict:
    return {"Authorization": "Bearer " + token, "Idempotency-Key": key or uuid.uuid4().hex}


def _context(monkeypatch, caller: str = "owner", *, allow: bool = False):
    r = asyncio.run(_seed())
    pepper, token = secrets.token_bytes(32), secrets.token_urlsafe(32)
    principal = getattr(r, caller)
    asyncio.run(_credential(principal, pepper, token))
    _settings(monkeypatch, pepper)
    if allow:
        async def grant():
            conn = await _app()
            try:
                await _allow_create(conn, r.owner, f"d53-{r.project}", r.lead, True)
            finally:
                await conn.close()
        asyncio.run(grant())
    # A real successful auth read distinguishes the missing create route from
    # a broken setup or a fabricated caller identity.
    with TestClient(create_app()) as public:
        response = public.get("/v1/auth/principal", headers=_headers(token))
        assert response.status_code == 200
        assert response.json()["data"]["principal_id"] == str(principal)
    return r, pepper, token


@pytest.mark.parametrize("caller", ("owner", "lead", "member"))
def test_public_listener_cannot_deliver_project_credentials(monkeypatch, caller):
    r, _, token = _context(monkeypatch, caller, allow=caller == "lead")
    with TestClient(create_app()) as public:
        result = public.post("/v1/projects", json=_request(f"d53-{r.project}"),
                             headers=_headers(token))
    assert result.status_code == 404


@pytest.mark.parametrize("caller", ("owner", "lead"))
def test_private_create_is_atomic_and_issues_365_day_recipient_keys(monkeypatch, caller):
    r, _, token = _context(monkeypatch, caller, allow=caller == "lead")
    payload = _request(f"d53-{r.project}")
    with TestClient(create_bootstrap_app()) as private:
        result = private.post("/v1/projects", json=payload, headers=_headers(token))
    assert result.status_code == 201
    data = result.json()["data"]
    assert data["project_key"] == payload["project_key"]
    assert data["delivery_state"] == "issued_once"
    assert data["lead_token"] != data["console_token"]
    scope = uuid.UUID(data["project_id"])

    async def check():
        conn = await asyncpg.connect(_url("MIGRATOR"))
        try:
            registry = await conn.fetchrow(
                "SELECT * FROM cortex_core.project_registry WHERE project_scope_id=$1", scope)
            assert registry["original_project_id"] == str(scope)
            assert registry["default_agent"] == payload["lead_name"]
            assert registry["repo_root"] == payload["repo_root"]
            assert json.loads(registry["roots"]) == payload["roots"]
            members = await conn.fetch(
                "SELECT p.principal_id,m.membership_role,m.responsibility,a.actor_kind "
                "FROM cortex_auth.memberships m JOIN cortex_auth.actor_bindings b "
                "ON b.actor_id=m.actor_id JOIN cortex_auth.principals p "
                "ON p.principal_id=b.principal_id JOIN cortex_auth.actors a "
                "ON a.actor_id=m.actor_id WHERE m.scope_id=$1", scope)
            assert {row["membership_role"] for row in members} == {"owner", "lead", "member"}
            lead = next(row for row in members if row["membership_role"] == "lead")
            console = next(row for row in members if row["membership_role"] == "member")
            assert lead["responsibility"] == payload["lead_responsibility"]
            assert console["actor_kind"] == "service"
            for principal, manager in ((lead["principal_id"], "user"),
                                       (console["principal_id"], "kos")):
                rows = await conn.fetch(
                    "SELECT c.created_at,c.expires_at,m.manager FROM cortex_auth.credentials c "
                    "JOIN cortex_auth.credential_managers m USING(credential_id) "
                    "WHERE c.principal_id=$1", principal)
                assert len(rows) == 1
                assert rows[0]["expires_at"] - rows[0]["created_at"] == timedelta(days=365)
                assert rows[0]["manager"] == manager
            assert await conn.fetchval(
                "SELECT count(*) FROM cortex_auth.project_create_rights "
                "WHERE project_scope_id=$1 AND revoked_at IS NULL", scope) == 0
            if caller == "lead":
                assert await conn.fetchval(
                    "SELECT count(*) FROM cortex_auth.scope_grants "
                    "WHERE scope_id=$1 AND principal_id=$2", scope, r.lead) == 0
                assert await conn.fetchval(
                    "SELECT sponsor_principal_id FROM cortex_auth.project_sponsors "
                    "WHERE project_scope_id=$1", scope) == r.lead
        finally:
            await conn.close()
    asyncio.run(check())
    # The member Console can authenticate but cannot acquire create authority.
    with TestClient(create_app()) as public:
        assert public.get("/v1/auth/principal", headers=_headers(data["console_token"])).status_code == 200
        assert public.post("/v1/projects", json=payload,
                           headers=_headers(data["console_token"])).status_code == 404


@pytest.mark.parametrize("caller,demote", (("member", False), ("lead", False), ("lead", True)))
def test_private_create_requires_a_live_source_create_right(monkeypatch, caller, demote):
    r, _, token = _context(monkeypatch, caller, allow=demote)
    if demote:
        async def change():
            conn = await asyncpg.connect(_url("MIGRATOR"))
            try:
                await conn.execute("UPDATE cortex_auth.memberships SET membership_role='member' "
                                   "WHERE scope_id=$1 AND actor_id=$2", r.project, r.lead)
            finally:
                await conn.close()
        asyncio.run(change())
    with TestClient(create_bootstrap_app()) as private:
        result = private.post("/v1/projects", json=_request(f"d53-{r.project}"),
                              headers=_headers(token))
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "project_create_not_allowed"


@pytest.mark.parametrize("source", ("not-registered", "ungranted"))
def test_source_alias_refusal_does_not_enumerate_projects(monkeypatch, source):
    r, _, token = _context(monkeypatch, "lead", allow=True)
    selected = source if source == "not-registered" else f"d53-{r.other_project}"
    with TestClient(create_bootstrap_app()) as private:
        response = private.post("/v1/projects", json=_request(selected), headers=_headers(token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "project_create_not_allowed"


@pytest.mark.parametrize("changes", (
    {"lead_key_manager": "unregistered-manager"}, {"roots": []},
    {"repo_root": "/not-the-primary"}, {"actor": "owner"},
))
def test_create_refuses_invalid_metadata_or_a_client_supplied_actor(monkeypatch, changes):
    r, _, token = _context(monkeypatch)
    payload = {**_request(f"d53-{r.project}"), **changes}
    with TestClient(create_bootstrap_app()) as private:
        result = private.post("/v1/projects", json=payload, headers=_headers(token))
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "invalid_request"


def test_create_replay_is_metadata_only_and_changed_request_conflicts(monkeypatch):
    r, _, token = _context(monkeypatch)
    payload, headers = _request(f"d53-{r.project}"), _headers(token)
    with TestClient(create_bootstrap_app()) as private:
        first = private.post("/v1/projects", json=payload, headers=headers)
        assert first.status_code == 201
        data = first.json()["data"]
        replay = private.post("/v1/projects", json=payload, headers=headers)
        assert replay.status_code == 200
        assert replay.headers["Idempotent-Replay"] == "true"
        receipt = replay.json()["data"]
        assert receipt["project_id"] == data["project_id"]
        assert receipt["operation_id"] == data["operation_id"]
        assert receipt["delivery_state"] == "reissue_required"
        assert "lead_token" not in receipt and "console_token" not in receipt
        assert data["lead_token"] not in replay.text and data["console_token"] not in replay.text
        conflict = private.post("/v1/projects", json={**payload, "display_name": "Changed"}, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_key_reused"
    async def saved():
        conn = await asyncpg.connect(_url("MIGRATOR"))
        try:
            return await conn.fetchval("SELECT receipt::text FROM cortex_core.command_receipts "
                                      "WHERE operation='project.create' AND idempotency_key=$1",
                                      headers["Idempotency-Key"])
        finally:
            await conn.close()
    stored = asyncio.run(saved())
    assert data["lead_token"] not in stored and data["console_token"] not in stored


def test_receipt_failure_rolls_back_all_project_identity_grant_and_audit_rows(monkeypatch):
    r, _, token = _context(monkeypatch)
    payload = _request(f"d53-{r.project}")
    tables = ("cortex_core.scopes", "cortex_auth.principals", "cortex_auth.actors",
              "cortex_auth.actor_bindings", "cortex_auth.credentials", "cortex_auth.scope_grants",
              "cortex_auth.memberships", "cortex_auth.project_installations",
              "cortex_auth.project_sponsors", "cortex_auth.privileged_actions",
              "cortex_core.command_receipts")
    async def counts():
        conn = await asyncpg.connect(_url("MIGRATOR"))
        try:
            return [await conn.fetchval("SELECT count(*) FROM " + table) for table in tables]
        finally:
            await conn.close()
    before = asyncio.run(counts())
    async def fail(*args, **kwargs):
        raise ApiProblem(503, "injected_receipt_failure", "Synthetic receipt failure.")
    monkeypatch.setattr(identity, "commit_receipt", fail)
    with TestClient(create_bootstrap_app()) as private:
        response = private.post("/v1/projects", json=payload, headers=_headers(token))
    assert response.status_code == 503
    assert asyncio.run(counts()) == before


def test_operation_id_reissue_replaces_lost_keys_without_second_project(monkeypatch):
    r, _, token = _context(monkeypatch)
    payload = _request(f"d53-{r.project}")
    with TestClient(create_bootstrap_app()) as private:
        first = private.post("/v1/projects", json=payload, headers=_headers(token))
        assert first.status_code == 201
        initial = first.json()["data"]
        headers = _headers(token)
        new = private.post("/v1/projects/" + initial["operation_id"] + ":reissue-keys",
                           json={"reason": "recipient_store_failed"}, headers=headers)
        assert new.status_code == 201
        replacement = new.json()["data"]
        assert replacement["project_id"] == initial["project_id"]
        assert replacement["lead_token"] != initial["lead_token"]
        assert replacement["console_token"] != initial["console_token"]
        replay = private.post("/v1/projects/" + initial["operation_id"] + ":reissue-keys",
                              json={"reason": "recipient_store_failed"}, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["data"]["delivery_state"] == "reissue_required"
        assert "lead_token" not in replay.json()["data"]
    with TestClient(create_app()) as public:
        for name in ("lead_token", "console_token"):
            assert public.get("/v1/auth/principal", headers=_headers(initial[name])).status_code == 401
            assert public.get("/v1/auth/principal", headers=_headers(replacement[name])).status_code == 200


def test_r25_registry_projection_has_preservation_tables_and_native_rls():
    async def check():
        conn = await asyncpg.connect(_url("MIGRATOR"))
        try:
            for name in ("project_registry", "project_identities", "project_profiles"):
                row = await conn.fetchrow(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
                    "WHERE oid=to_regclass($1)", "cortex_core." + name)
                assert row is not None, "S5/0015 canonical preservation table is missing: " + name
                assert row["relrowsecurity"] and row["relforcerowsecurity"]
        finally:
            await conn.close()
    asyncio.run(check())


def test_only_first_project_lead_automatically_gets_create_right(monkeypatch):
    installation, owner = uuid.uuid4(), uuid.uuid4()
    pepper, token = secrets.token_bytes(32), secrets.token_urlsafe(32)
    async def seed_owner():
        conn = await asyncpg.connect(_url("MIGRATOR"))
        try:
            await conn.execute("INSERT INTO cortex_auth.installations(installation_id,display_name) "
                               "VALUES($1,'S5 fresh owner fixture')", installation)
            await conn.execute("INSERT INTO cortex_auth.principals(principal_id,installation_id,principal_name,status) "
                               "VALUES($1,$2,'owner','active')", owner, installation)
            await conn.execute("INSERT INTO cortex_auth.actors(actor_id,installation_id,actor_kind,display_name) "
                               "VALUES($1,$2,'human','owner')", owner, installation)
            await conn.execute("INSERT INTO cortex_auth.actor_bindings(actor_id,principal_id,bound_by_principal_id) "
                               "VALUES($1,$1,$1)", owner)
            await conn.execute("INSERT INTO cortex_auth.installation_owners(installation_id,principal_id) "
                               "VALUES($1,$2)", installation, owner)
        finally:
            await conn.close()
        await _credential(owner, pepper, token)
    asyncio.run(seed_owner())
    _settings(monkeypatch, pepper)
    with TestClient(create_bootstrap_app()) as private:
        first = private.post("/v1/projects", json=_request(None), headers=_headers(token))
        assert first.status_code == 201
        data = first.json()["data"]
        second = private.post("/v1/projects", json=_request(data["project_key"]),
                              headers=_headers(data["lead_token"]))
        assert second.status_code == 201
        next_data = second.json()["data"]
        third = private.post("/v1/projects", json=_request(next_data["project_key"]),
                             headers=_headers(next_data["lead_token"]))
        assert third.status_code == 403
        assert third.json()["error"]["code"] == "project_create_not_allowed"


def test_r25_projection_preserves_original_ids_roots_records_counts_and_rls(monkeypatch):
    r, _, token = _context(monkeypatch, "member")
    original_id = str(uuid.uuid4())
    roots = [{"path": "/source/project", "kind": "primary", "repo_type": "git"},
             {"path": "/source/kept", "kind": "reference", "label": "Ω original"}]
    source_agents = [
        {"id": "source-lead", "name": "real-lead", "status": "active",
         "capabilities": {"visibility": "active"}, "retained_extra": {"x": 1}},
        {"id": "source-hidden", "name": "hidden", "status": "retired",
         "capabilities": {"visibility": "history-only", "keep_visible": True}},
        {"id": "source-visible", "name": "visible-override", "status": "active",
         "capabilities": {"keep_visible": True}, "retained_extra": None},
    ]
    source_profiles = [{"id": "p1", "agent_name": "real-lead", "body": "Full lead profile Ω"},
                       {"id": "p2", "agent_name": "hidden", "body": "Kept retired profile"},
                       {"id": "p3", "agent_name": "orphan-profile", "body": "Original unbound profile"}]
    created = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    updated = datetime(2021, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    async def import_synthetic_rows():
        conn = await asyncpg.connect(_url("MIGRATOR"))
        try:
            assert await conn.fetchval("SELECT to_regclass('cortex_core.project_registry')") is not None
            await conn.execute(
                "INSERT INTO cortex_core.project_registry(project_scope_id,original_project_id,"
                "display_name,default_agent,status,parent_project_key,repo_root,roots,created_at,updated_at) "
                "VALUES($1,$2,'Preserved project','real-lead','active',$3,'/source/project',$4::jsonb,$5,$6)",
                r.project, original_id, f"d53-{r.other_project}", json.dumps(roots), created, updated)
            for record in source_agents:
                await conn.execute(
                    "INSERT INTO cortex_core.project_identities(identity_id,project_scope_id,original_identity_id,"
                    "identity_name,identity_kind,original_record) VALUES($1,$2,$3,$4,'agent',$5::jsonb)",
                    uuid.uuid4(), r.project, record["id"], record["name"], json.dumps(record))
            for record in source_profiles:
                await conn.execute(
                    "INSERT INTO cortex_core.project_profiles(profile_id,project_scope_id,original_profile_id,"
                    "agent_name,original_record) VALUES($1,$2,$3,$4,$5::jsonb)",
                    uuid.uuid4(), r.project, record["id"], record["agent_name"], json.dumps(record))
            def fingerprint(records):
                payload = json.dumps(sorted(records, key=lambda row: row["id"]),
                                     sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                return hashlib.sha256(payload.encode()).hexdigest()
            for name, source in (("project_identities", source_agents), ("project_profiles", source_profiles)):
                target = [json.loads(row["original_record"]) for row in await conn.fetch(
                    "SELECT original_record FROM cortex_core." + name + " WHERE project_scope_id=$1", r.project)]
                assert len(target) == len(source)
                assert fingerprint(target) == fingerprint(source)
        finally:
            await conn.close()
        app = await _app()
        try:
            async with app.transaction():
                await app.execute("SELECT set_config('cortex.principal_id',$1,true)", str(r.other_lead))
                # Forging the requested read set still cannot bypass the live grant.
                await app.execute("SELECT set_config('cortex.read_scope_ids',$1,true)", str(r.project))
                assert await app.fetchval("SELECT count(*) FROM cortex_core.project_registry "
                                          "WHERE project_scope_id=$1", r.project) == 0
                assert await app.fetchval("SELECT count(*) FROM cortex_core.project_identities "
                                          "WHERE project_scope_id=$1", r.project) == 0
                assert await app.fetchval("SELECT count(*) FROM cortex_core.project_profiles "
                                          "WHERE project_scope_id=$1", r.project) == 0
        finally:
            await app.close()
    asyncio.run(import_synthetic_rows())
    with TestClient(create_app()) as public:
        response = public.get("/projects", headers={**_headers(token), "X-Agent-Name": "owner",
                                                  "X-Project": f"d53-{r.other_project}"})
    assert response.status_code == 200
    rows = response.json()["projects"]
    assert len(rows) == 1
    row = rows[0]
    assert row["project_id"] == original_id
    assert row["project_key"] == f"d53-{r.project}"
    assert row["display_name"] == "Preserved project"
    assert row["default_agent"] == "real-lead"
    assert row["parent_project_key"] == f"d53-{r.other_project}"
    assert row["roots"] == roots and row["repo_root"] == "/source/project"
    assert datetime.fromisoformat(row["created_at"]) == created
    assert datetime.fromisoformat(row["updated_at"]) == updated
    # Preserve the legacy visible-agent predicate: history-only is excluded;
    # otherwise a profile or explicit keep_visible makes the row visible.
    assert row["agent_count"] == 2
    assert row["profile_count"] == 3
