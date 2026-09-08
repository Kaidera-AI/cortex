"""Pure/domain and transactional fake proofs; never imports main or a live DSN.

The optional real-Postgres proof is in test_service_auth_postgres.py. This fake
asserts every SQL shape it understands; it is not a substitute for SQL-engine
or deployment qualification.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

import service_auth as auth


class MemoryDB:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.calls = []
        self.data = {"state": {"singleton": True, "instance_id": uuid4(), "generation": 1, "initialized_at": None},
                     "principals": {}, "grants": {}, "tokens": {}, "setups": {}, "audit": []}
        self.project_id, self.agent_id, self.actor_id = uuid4(), uuid4(), uuid4()
        self.identity = {"id": self.agent_id, "actor_id": self.actor_id}

    @asynccontextmanager
    async def acquire(self):
        yield MemoryConnection(self)

    def token_row(self, token_id):
        token = self.data["tokens"].get(token_id)
        if token is None or self.data["state"] is None:
            return None
        p = self.data["principals"][token["principal_id"]]
        g = self.data["grants"][p["id"]]
        return dict(token, project_id=p["project_id"], agent_id=p["agent_id"],
                    actor_id=p["actor_id"], installation_id=p["installation_id"],
                    principal_disabled=p["disabled_at"], grant_disabled=g["disabled_at"],
                    grant_scopes=g["scopes"], current_generation=self.data["state"]["generation"],
                    project_key="notes", project_status="active", agent_name="writer",
                    agent_status="available", agent_project_id=p["project_id"],
                    agent_actor_id=p["actor_id"], actor_project_id=p["project_id"], actor_status="active")


class MemoryConnection:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        async with self.db.lock:
            before = deepcopy(self.db.data)
            try:
                yield
            except BaseException:
                self.db.data = before
                raise

    def query(self, sql, args):
        sql = " ".join(sql.split())
        self.db.calls.append((sql, args))
        return sql

    async def fetchrow(self, sql, *args):
        sql = self.query(sql, args)
        d = self.db.data
        if sql.startswith("SELECT * FROM cortex_auth.state"):
            return deepcopy(d["state"])
        if sql.startswith("SELECT t.*"):
            return self.db.token_row(args[0])
        if sql.startswith("SELECT a.id, a.actor_id"):
            return self.db.identity if args == (self.db.agent_id, self.db.project_id) else None
        if sql.startswith("SELECT p.*, g.disabled_at"):
            for p in d["principals"].values():
                if (p["installation_id"], p["project_id"], p["agent_id"]) == args:
                    g = d["grants"][p["id"]]
                    return dict(p, grant_disabled=g["disabled_at"], grant_scopes=g["scopes"])
            return None
        if sql.startswith("SELECT * FROM cortex_auth.setup_grants"):
            return d["setups"].get(args[0])
        if sql.startswith("SELECT principal_id,revoked_at"):
            return d["tokens"].get(args[0])
        if sql.startswith("SELECT p.disabled_at,g.disabled_at"):
            p = d["principals"].get(args[0])
            return None if p is None else dict(p, grant_disabled=d["grants"][args[0]]["disabled_at"])
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        sql = self.query(sql, args)
        d = self.db.data
        if "FROM cortex_auth.tokens WHERE principal_id=$1" in sql:
            return any(t["principal_id"] == args[0] for t in d["tokens"].values())
        if "FROM cortex_auth.tokens WHERE predecessor_id=$1" in sql:
            return any(t["predecessor_id"] == args[0] and t["revoked_at"] is None for t in d["tokens"].values())
        if "FROM cortex_auth.principals WHERE id=$1" in sql:
            return args[0] in d["principals"]
        if "FROM cortex_auth.state WHERE singleton" in sql:
            return d["state"] is not None
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        sql = self.query(sql, args)
        if sql.startswith("SELECT id,principal_id"):
            columns = [c.strip() for c in sql.split(" FROM ")[0].removeprefix("SELECT ").split(",")]
            return [{k: t[k] for k in columns} for t in self.db.data["tokens"].values()
                    if args[0] is None or t["principal_id"] == args[0]]
        if sql.startswith("SELECT t.id,t.principal_id"):
            result = []
            for token in self.db.data["tokens"].values():
                row = self.db.token_row(token["id"])
                if (row["revoked_at"] is None and row["principal_disabled"] is None
                        and row["grant_disabled"] is None and row["generation"] == row["current_generation"]
                        and row["rotate_after"] <= args[0] and row["rotation_started_at"] is None
                        and (row["predecessor_id"] is None or row["acknowledged_at"] is not None)):
                    result.append({k: row[k] for k in ("id", "principal_id", "expires_at", "rotate_after")})
            return result
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        sql = self.query(sql, args)
        d = self.db.data
        if sql.startswith("INSERT INTO cortex_auth.principals"):
            keys = ("id", "project_id", "agent_id", "actor_id", "installation_id", "created_at")
            d["principals"][args[0]] = dict(zip(keys, args), disabled_at=None)
        elif sql.startswith("INSERT INTO cortex_auth.grants"):
            d["grants"][args[0]] = dict(principal_id=args[0], scopes=args[1], created_at=args[2], updated_at=args[2], disabled_at=None)
        elif sql.startswith("INSERT INTO cortex_auth.tokens"):
            keys = ("id", "principal_id", "family_id", "generation", "token_digest", "scopes", "issued_at", "expires_at", "rotate_after", "predecessor_id")
            d["tokens"][args[0]] = dict(zip(keys, args), revoked_at=None, rotation_started_at=None,
                                      overlap_deadline=None, consumer_used_at=None, acknowledged_at=None)
        elif sql.startswith("INSERT INTO cortex_auth.setup_grants"):
            keys = ("id", "purpose", "token_digest", "generation", "created_at", "expires_at")
            d["setups"][args[0]] = dict(zip(keys, args), revoked_at=None, consumed_at=None)
        elif sql.startswith("INSERT INTO cortex_auth.audit"):
            d["audit"].append(dict(zip(("occurred_at", "action", "principal_id", "token_id", "generation"), args)))
        elif sql.startswith("UPDATE cortex_auth.state SET initialized_at"):
            d["state"]["initialized_at"] = args[0]
        elif sql.startswith("UPDATE cortex_auth.state SET generation"):
            d["state"]["generation"] = args[0]
        elif sql.startswith("UPDATE cortex_auth.principals SET disabled_at"):
            d["principals"][args[0]]["disabled_at"] = args[1]
        elif sql.startswith("UPDATE cortex_auth.grants SET disabled_at"):
            d["grants"][args[0]].update(disabled_at=args[1], updated_at=args[1])
        elif sql.startswith("UPDATE cortex_auth.grants SET scopes"):
            d["grants"][args[0]].update(scopes=args[1], updated_at=args[2])
        elif sql.startswith("UPDATE cortex_auth.tokens SET consumer_used_at"):
            d["tokens"][args[0]]["consumer_used_at"] = args[1]
        elif sql.startswith("UPDATE cortex_auth.tokens SET rotation_started_at"):
            d["tokens"][args[0]].update(rotation_started_at=args[1], overlap_deadline=args[2])
        elif sql.startswith("UPDATE cortex_auth.tokens SET acknowledged_at"):
            d["tokens"][args[0]]["acknowledged_at"] = args[1]
        elif sql.startswith("UPDATE cortex_auth.tokens SET revoked_at"):
            for token in d["tokens"].values():
                if "WHERE principal_id=$1" in sql and token["principal_id"] != args[0]:
                    continue
                if "WHERE id=$1" in sql and token["id"] != args[0]:
                    continue
                token["revoked_at"] = token["revoked_at"] or args[-1]
        elif sql.startswith("UPDATE cortex_auth.setup_grants SET consumed_at"):
            d["setups"][args[0]]["consumed_at"] = args[1]
        elif sql.startswith("UPDATE cortex_auth.setup_grants SET revoked_at"):
            for grant in d["setups"].values():
                if "WHERE consumed_at IS NULL" in sql and (grant["consumed_at"] or grant["revoked_at"]):
                    continue
                grant["revoked_at"] = grant["revoked_at"] or args[0]
        else:
            raise AssertionError(sql)
        return "UPDATE 1"


@pytest.fixture
def env():
    db = MemoryDB()
    now = [datetime(2026, 9, 5, tzinfo=timezone.utc)]
    store = auth.ServiceAuthStore(lambda: db, clock=lambda: now[0])
    return store, db, now


async def issue(env, **kwargs):
    store, db, _ = env
    return await store.issue(authority=auth.LOCAL_OWNER, project_id=db.project_id,
                             agent_id=db.agent_id, installation_id="cli-a", scopes=["memory:read", "memory:write"], **kwargs)


@pytest.mark.asyncio
async def test_issuance_hash_only_fixed_lifetime_immutable_context(env):
    store, db, now = env
    issued = await issue(env)
    assert issued.raw_token.startswith("ctx1_")
    assert len(issued.raw_token.split(".")[1]) == 43
    assert issued.raw_token not in repr(issued)
    assert issued.raw_token not in repr(db.calls)
    assert issued.raw_token not in repr(db.data)
    row = db.data["tokens"][issued.context.token_id]
    assert len(row["token_digest"]) == 32
    assert row["expires_at"] == now[0] + timedelta(days=30)
    assert row["rotate_after"] == now[0] + timedelta(days=21)
    assert await store.authenticate(issued.raw_token) == issued.context
    assert issued.context.scopes == frozenset({"memory:read", "memory:write"})
    with pytest.raises(FrozenInstanceError):
        issued.context.agent = "forged"
    with pytest.raises(auth.AuthForbidden):
        issued.context.require("registry:write")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "", "Bearer bad", "ctx1_x.y", " ctx1_" + "a" * 32 + "." + "b" * 43,
                                  "ctx1_" + "a" * 32 + "." + "b" * 44, "ctxs1_" + "a" * 32 + "." + "b" * 43])
async def test_malformed_credentials_never_query_database(env, raw):
    store, db, _ = env
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(raw)
    assert db.calls == []


@pytest.mark.asyncio
async def test_wrong_secret_unknown_id_constant_time_and_no_cache(env, monkeypatch):
    store, db, _ = env
    issued = await issue(env)
    actual_compare = auth.hmac.compare_digest
    comparisons = []
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: comparisons.append((len(a), len(b))) or actual_compare(a, b))
    wrong = issued.raw_token[:-1] + ("a" if issued.raw_token[-1] != "a" else "b")
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(wrong)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(auth._credential()[1])
    assert comparisons == [(32, 32), (32, 32)]
    await store.authenticate(issued.raw_token)
    await store.revoke(issued.context.token_id, authority=auth.LOCAL_OWNER)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(issued.raw_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "overlap_elapsed", "issued_in_future", "generation", "principal_disabled",
                                     "grant_disabled", "retired_actor", "paused_project", "wrong_agent_project", "wrong_actor", "wrong_actor_project"])
async def test_context_rejects_authority_or_registry_drift(env, change, monkeypatch):
    store, db, now = env
    issued = await issue(env)
    original = db.token_row
    def row(token_id):
        result = original(token_id)
        updates = {
            "expired": {"expires_at": now[0]}, "overlap_elapsed": {"overlap_deadline": now[0]},
            "issued_in_future": {"issued_at": now[0] + timedelta(seconds=1)},
            "generation": {"current_generation": 2}, "principal_disabled": {"principal_disabled": now[0]},
            "grant_disabled": {"grant_disabled": now[0]}, "retired_actor": {"actor_status": "retired"},
            "paused_project": {"project_status": "paused"}, "wrong_agent_project": {"agent_project_id": uuid4()},
            "wrong_actor": {"agent_actor_id": uuid4()}, "wrong_actor_project": {"actor_project_id": uuid4()},
        }
        return dict(result, **updates[change])
    monkeypatch.setattr(db, "token_row", row)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(issued.raw_token)


@pytest.mark.asyncio
async def test_store_missing_unavailable_and_bad_scope_fail_closed(env, monkeypatch):
    store, db, _ = env
    issued = await issue(env)
    db.data["state"] = None
    with pytest.raises(auth.AuthStoreUnavailable):
        await store.authenticate(issued.raw_token)
    absent = auth.ServiceAuthStore(lambda: None)
    with pytest.raises(auth.AuthStoreUnavailable):
        await absent.authenticate(issued.raw_token)
    db.data["state"] = {"generation": 1}
    db.data["grants"][issued.context.principal_id]["scopes"] = ["*"]
    with pytest.raises(auth.AuthStoreUnavailable):
        await store.authenticate(issued.raw_token)
    @asynccontextmanager
    async def failed():
        raise asyncpg.UndefinedTableError("do not expose driver parameters")
        yield
    monkeypatch.setattr(db, "acquire", failed)
    with pytest.raises(auth.AuthStoreUnavailable) as caught:
        await store.authenticate(issued.raw_token)
    assert "parameters" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [["*"], ["admin"], [None], "memory:read", [], ["memory:read", "unknown"]])
async def test_scope_allowlist_rejects_unbounded_issuance(env, scopes):
    store, db, _ = env
    with pytest.raises(auth.AuthForbidden):
        await store.issue(authority=auth.LOCAL_OWNER, project_id=db.project_id, agent_id=db.agent_id,
                          installation_id="cli-a", scopes=scopes)
    assert db.calls == []


@pytest.mark.asyncio
async def test_grant_intersection_never_expands_existing_token(env):
    store, _, _ = env
    issued = await issue(env)
    await store.set_grant_scopes(issued.context.principal_id, ["memory:read"], authority=auth.LOCAL_OWNER)
    assert (await store.authenticate(issued.raw_token)).scopes == {"memory:read"}
    await store.set_grant_scopes(issued.context.principal_id, ["memory:read", "registry:write"], authority=auth.LOCAL_OWNER)
    assert (await store.authenticate(issued.raw_token)).scopes == {"memory:read"}
    await store.set_grant_scopes(issued.context.principal_id, [], authority=auth.LOCAL_OWNER)
    assert not (await store.authenticate(issued.raw_token)).scopes


@pytest.mark.asyncio
async def test_owner_capability_required_for_every_management_method(env):
    store, db, _ = env
    issued = await issue(env)
    for authority in [None, True, "LOCAL_OWNER", issued.context, issued.raw_token]:
        operations = [
            store.instance_identity(authority=authority),
            store.issue(authority=authority, project_id=db.project_id, agent_id=db.agent_id, installation_id="x", scopes=["memory:read"]),
            store.create_setup_grant(authority=authority),
            store.consume_setup_grant("unused", authority=authority, resolve_identity=None, installation_id="x", scopes=["memory:read"]),
            store.rotate(issued.context.token_id, authority=authority),
            store.acknowledge_rotation(issued.context.token_id, authority=authority),
            store.revoke(issued.context.token_id, authority=authority),
            store.disable_grant(issued.context.principal_id, authority=authority),
            store.disable_principal(issued.context.principal_id, authority=authority),
            store.set_grant_scopes(issued.context.principal_id, ["memory:read"], authority=authority),
            store.list_tokens(authority=authority), store.due_rotations(authority=authority),
            store.invalidate_restored_generations(authority=authority),
        ]
        for operation in operations:
            with pytest.raises(auth.AuthForbidden):
                await operation


@pytest.mark.asyncio
async def test_owner_instance_identity_is_authoritative_read_only_metadata(env):
    store, db, _ = env
    assert await store.instance_identity(authority=auth.LOCAL_OWNER) == str(db.data['state']['instance_id'])
    assert len(db.calls) == 1 and db.calls[0][0].startswith('SELECT * FROM cortex_auth.state')
    db.data['state'] = None
    with pytest.raises(auth.AuthStoreUnavailable):
        await store.instance_identity(authority=auth.LOCAL_OWNER)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grant", "principal"])
async def test_disabled_grants_stay_disabled_through_reenrollment_recovery_and_renewal(env, kind):
    store, _, _ = env
    issued = await issue(env)
    await getattr(store, f"disable_{kind}")(issued.context.principal_id, authority=auth.LOCAL_OWNER)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(issued.raw_token)
    for recover in [False, True]:
        with pytest.raises(auth.AuthForbidden):
            await issue(env, recover=recover)
    with pytest.raises(auth.AuthInvalid):
        await store.rotate(issued.context.token_id, authority=auth.LOCAL_OWNER)
    with pytest.raises(auth.AuthForbidden):
        await store.set_grant_scopes(issued.context.principal_id, ["memory:read"], authority=auth.LOCAL_OWNER)
    assert await store.due_rotations(authority=auth.LOCAL_OWNER) == []


@pytest.mark.asyncio
async def test_reenrollment_is_not_duplicate_issuance_and_explicit_recovery_revokes_old(env):
    store, db, _ = env
    issued = await issue(env)
    with pytest.raises(auth.AuthForbidden):
        await issue(env)
    assert len(db.data["tokens"]) == len(db.data["principals"]) == 1
    replacement = await issue(env, recover=True)
    assert replacement.context.principal_id == issued.context.principal_id
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(issued.raw_token)
    assert await store.authenticate(replacement.raw_token)


@pytest.mark.asyncio
async def test_bootstrap_atomic_one_use_race_and_never_reopens(env):
    store, db, now = env
    grant = await store.create_setup_grant(authority=auth.LOCAL_OWNER)
    assert grant.expires_at == now[0] + timedelta(minutes=10)
    assert grant.raw_token not in repr(db.data) and grant.raw_token not in repr(grant)
    callbacks = []
    async def resolve(conn):
        callbacks.append(conn)
        assert conn.db.lock.locked()
        await asyncio.sleep(0)
        return db.project_id, db.agent_id
    async def consume():
        return await store.consume_setup_grant(grant.raw_token, authority=auth.LOCAL_OWNER, resolve_identity=resolve,
                                              installation_id="cli-a", scopes=["memory:read"])
    results = await asyncio.gather(consume(), consume(), return_exceptions=True)
    assert sum(isinstance(r, auth.IssuedToken) for r in results) == 1
    assert sum(isinstance(r, auth.AuthInvalid) for r in results) == 1
    assert len(callbacks) == 1
    assert len(db.data["principals"]) == len(db.data["tokens"]) == 1
    for t in db.data["tokens"].values():
        t["revoked_at"] = now[0]
    with pytest.raises(auth.AuthForbidden):
        await store.create_setup_grant(authority=auth.LOCAL_OWNER)


@pytest.mark.asyncio
async def test_bootstrap_callback_failure_rolls_back_everything_and_grant_can_retry(env):
    store, db, _ = env
    grant = await store.create_setup_grant(authority=auth.LOCAL_OWNER)
    before = deepcopy(db.data)
    async def failing(conn):
        conn.db.data["audit"].append({"should_rollback": True})
        raise RuntimeError("registration failed")
    with pytest.raises(RuntimeError):
        await store.consume_setup_grant(grant.raw_token, authority=auth.LOCAL_OWNER, resolve_identity=failing,
                                       installation_id="cli-a", scopes=["memory:read"])
    assert db.data == before


@pytest.mark.asyncio
async def test_bootstrap_issuance_failure_rolls_back_registration_and_consumption(env, monkeypatch):
    store, db, _ = env
    grant = await store.create_setup_grant(authority=auth.LOCAL_OWNER)
    before = deepcopy(db.data)
    async def resolve(conn):
        return db.project_id, db.agent_id
    real_mint = store._mint
    async def failed_mint(*args, **kwargs):
        await real_mint(*args, **kwargs)
        raise asyncpg.InsufficientPrivilegeError("simulated database failure")
    monkeypatch.setattr(store, "_mint", failed_mint)
    with pytest.raises(auth.AuthStoreUnavailable):
        await store.consume_setup_grant(grant.raw_token, authority=auth.LOCAL_OWNER, resolve_identity=resolve,
                                       installation_id="cli-a", scopes=["memory:read"])
    assert db.data == before
    monkeypatch.setattr(store, "_mint", real_mint)
    issued = await store.consume_setup_grant(grant.raw_token, authority=auth.LOCAL_OWNER, resolve_identity=resolve,
                                            installation_id="cli-a", scopes=["memory:read"])
    assert await store.authenticate(issued.raw_token)


@pytest.mark.asyncio
async def test_expired_replaced_and_wrong_purpose_setup_are_rejected_before_callback(env):
    store, db, now = env
    first = await store.create_setup_grant(authority=auth.LOCAL_OWNER)
    second = await store.create_setup_grant(authority=auth.LOCAL_OWNER)
    async def must_not_run(conn):
        pytest.fail("Rejected setup invoked registration")
    for token, purpose in [(first.raw_token, "bootstrap"), (second.raw_token, "recovery")]:
        with pytest.raises(auth.AuthInvalid):
            await store.consume_setup_grant(token, authority=auth.LOCAL_OWNER, resolve_identity=must_not_run,
                                           installation_id="cli-a", scopes=["memory:read"], purpose=purpose)
    now[0] += timedelta(minutes=10)
    with pytest.raises(auth.AuthInvalid):
        await store.consume_setup_grant(second.raw_token, authority=auth.LOCAL_OWNER, resolve_identity=must_not_run,
                                       installation_id="cli-a", scopes=["memory:read"])


@pytest.mark.asyncio
async def test_rotation_real_consumer_use_then_owner_ack_and_fixed_overlap(env):
    store, db, now = env
    issued = await issue(env)
    now[0] += timedelta(days=21)
    assert [t["id"] for t in await store.due_rotations(authority=auth.LOCAL_OWNER)] == [issued.context.token_id]
    candidate = await store.rotate(issued.context.token_id, authority=auth.LOCAL_OWNER)
    assert db.data["tokens"][issued.context.token_id]["overlap_deadline"] == now[0] + timedelta(hours=24)
    assert await store.authenticate(candidate.raw_token)  # helper probe is NOT switchover proof
    with pytest.raises(auth.AuthForbidden):
        await store.acknowledge_rotation(candidate.context.token_id, authority=auth.LOCAL_OWNER)
    assert await store.authenticate(issued.raw_token)
    await store.observe_consumer_use(await store.authenticate(candidate.raw_token))
    await store.acknowledge_rotation(candidate.context.token_id, authority=auth.LOCAL_OWNER)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(issued.raw_token)
    assert await store.authenticate(candidate.raw_token)
    await store.acknowledge_rotation(candidate.context.token_id, authority=auth.LOCAL_OWNER)  # idempotent
    count = len(db.data["audit"])
    await store.observe_consumer_use(candidate.context)
    assert len(db.data["audit"]) == count


@pytest.mark.asyncio
async def test_unacknowledged_orphan_replacement_never_extends_predecessor(env):
    store, db, now = env
    issued = await issue(env)
    candidate = await store.rotate(issued.context.token_id, authority=auth.LOCAL_OWNER)
    deadline = db.data["tokens"][issued.context.token_id]["overlap_deadline"]
    with pytest.raises(auth.AuthForbidden):
        await store.rotate(issued.context.token_id, authority=auth.LOCAL_OWNER)
    with pytest.raises(auth.AuthForbidden):
        await store.rotate(candidate.context.token_id, authority=auth.LOCAL_OWNER)
    now[0] += timedelta(hours=23)
    await store.revoke(candidate.context.token_id, authority=auth.LOCAL_OWNER)
    replacement = await store.rotate(issued.context.token_id, authority=auth.LOCAL_OWNER)
    assert replacement.context.token_id != candidate.context.token_id
    assert db.data["tokens"][issued.context.token_id]["overlap_deadline"] == deadline
    now[0] += timedelta(hours=1)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(issued.raw_token)
    assert await store.authenticate(replacement.raw_token)


@pytest.mark.asyncio
async def test_sleep_past_expiry_needs_owner_not_expired_data_token(env):
    store, db, now = env
    issued = await issue(env)
    now[0] += timedelta(days=31)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(issued.raw_token)
    replacement = await store.rotate(issued.context.token_id, authority=auth.LOCAL_OWNER)
    assert await store.authenticate(replacement.raw_token)
    assert db.data["tokens"][issued.context.token_id]["overlap_deadline"] <= now[0]


@pytest.mark.asyncio
async def test_rotation_current_grant_intersection_cannot_increase_scope(env):
    store, _, _ = env
    issued = await issue(env)
    await store.set_grant_scopes(issued.context.principal_id, ["memory:read", "instance:admin"], authority=auth.LOCAL_OWNER)
    candidate = await store.rotate(issued.context.token_id, authority=auth.LOCAL_OWNER)
    assert candidate.context.scopes == {"memory:read"}


@pytest.mark.asyncio
async def test_restore_invalidates_tokens_setup_and_restart_does_not(env):
    store, db, now = env
    issued = await issue(env)
    setup = await store.create_setup_grant(authority=auth.LOCAL_OWNER)
    restarted = auth.ServiceAuthStore(lambda: db, clock=lambda: now[0])
    assert await restarted.authenticate(issued.raw_token)
    assert await store.invalidate_restored_generations(authority=auth.LOCAL_OWNER) == 2
    with pytest.raises(auth.AuthInvalid):
        await restarted.authenticate(issued.raw_token)
    assert db.data["setups"][setup.grant_id]["revoked_at"] is not None
    renewed = await issue(env, recover=True)
    assert renewed.context.generation == 2
    assert await restarted.authenticate(renewed.raw_token)


@pytest.mark.asyncio
async def test_list_metadata_no_digest_or_plaintext(env):
    store, _, _ = env
    issued = await issue(env)
    rows = await store.list_tokens(authority=auth.LOCAL_OWNER)
    assert len(rows) == 1 and rows[0]["id"] == issued.context.token_id
    assert "token_digest" not in rows[0] and issued.raw_token not in repr(rows)


def test_migration_is_additive_private_and_uses_existing_registries():
    sql = (Path(__file__).parents[2] / "data/migrations/2026-09-05-01-service-auth.sql").read_text()
    assert "CREATE SCHEMA IF NOT EXISTS cortex_auth" in sql
    for table in ["public.cortex_projects(id)", "public.agents(id)", "public.cortex_actors(id)"]:
        assert f"REFERENCES {table}" in sql
    assert "CREATE TABLE public." not in sql
    assert "ALTER TABLE public." not in sql
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA cortex_auth" in sql
    assert "service_tokens_one_candidate" in sql
    assert "service_principal_identity" in sql
    assert "token_digest bytea" in sql
    assert "interval '720 hours'" in sql and "interval '504 hours'" in sql
