"""R151: every agent-family row needs usable native readback, never authority."""
import asyncio
import dataclasses
import hashlib
import importlib
import json
import uuid
from pathlib import Path

import pytest
from fixtures.state_import_agents_native import AUTHORITY, agent_cluster
from test_state_import_projects_native import PROJECTS, WHEN, seed, inputs as project_inputs

ROOT = Path(__file__).resolve().parents[1]
CUSTOMER = "15100000-0000-4000-8000-000000000001"
ACTOR = "15100000-0000-4000-8000-000000000002"
USER = "15100000-0000-4000-8000-000000000003"
TABLES = ("public.agents", "public.agent_profiles", "public.roles",
          "cortex.agent_profiles", "cortex.roles", "cortex.role_audit_events")
ROSTER = {"roles": {"pm_lead": "worker", "approved_agents": ["worker", "old"],
                    "support_agents": ["old", "worker"],
                    "role_assignments": {"lead": "worker"}},
          "responsibilities": {"worker": ["write", "review"], "old": "history"}}


def uid(n):
    return str(uuid.UUID(f"15100000-0000-4000-8000-{n:012d}"))


def modules():
    assert importlib.util.find_spec("cortex_v2.legacy_agents"), "Missing agent-family native reader"
    engine = importlib.import_module("cortex_v2.state_import")
    assert hasattr(engine, "import_agents"), "Missing agent-family entrypoint"
    agents = importlib.import_module("cortex_v2.legacy_agents")
    assert hasattr(agents, "read_native_roster"), "Missing finite native roster reader"
    return engine, agents


async def setup(pair):
    await seed(pair)
    await pair["writer"].execute("UPDATE public.cortex_projects SET metadata=metadata || jsonb_build_object('roster_policy',$1::jsonb) WHERE id=$2", json.dumps(ROSTER), PROJECTS[0])
    e, s, b, p = await project_inputs(pair)
    await e.import_projects(pair["target"], s, b, p)
    items = [
        (TABLES[0], dict(id=uid(10), name="worker", project="alpha", project_id=str(PROJECTS[0]), actor_id=ACTOR, role="lead", capabilities={"can_write": True, "responsibility": ["write", "review"]}, runtime_state={"old": False}, status="available")),
        (TABLES[0], dict(id=uid(11), name="old", project="alpha", project_id=str(PROJECTS[0]), actor_id=ACTOR, role="support", capabilities={"visibility": "history-only"}, runtime_state={}, status="retired")),
        (TABLES[0], dict(id=uid(12), name="archived-worker", project="archived", project_id=str(PROJECTS[1]), actor_id=ACTOR, role="lead", capabilities={}, runtime_state={}, status="available")),
        (TABLES[1], dict(id=uid(13), project="alpha", project_id=str(PROJECTS[0]), actor_id=ACTOR, agent_name="profile-only", profile_kind="identity", role="support", source_file="profiles/profile-only.md", profile_text="Original λ responsibility", metadata={"history": [{"role": "support"}, {"role": "lead"}]})),
        (TABLES[1], dict(id=uid(14), project="alpha", project_id=str(PROJECTS[0]), actor_id=ACTOR, agent_name="worker", profile_kind="role", role="lead", source_file="roles/lead.md", profile_text="Original lead history", metadata={"history": ["new", "old"]})),
        (TABLES[2], dict(project="alpha", name="lead", default_capabilities={"can_write": True}, description="Own write and review", is_builtin=False, source_file="roles/lead.md")),
        (TABLES[3], dict(id=uid(15), name="core-worker", role="support", lane="cortex", reports_to="worker", capabilities={"history": ["support", "lead"]}, status="active", customer_id=CUSTOMER, project_id=str(PROJECTS[0]))),
        (TABLES[4], dict(id=uid(16), customer_id=CUSTOMER, project_id=str(PROJECTS[0]), name="lead", display_name="Lead", description="Core responsibility", permissions={"write": True}, is_builtin=False, status="active")),
        (TABLES[5], dict(id=uid(17), customer_id=CUSTOMER, project_id=str(PROJECTS[0]), role_name="lead", action="create", actor_scope="agent", actor_agent="worker", actor_user_id=USER, metadata={"order": ["create", "update"]})),
        (TABLES[5], dict(id=uid(18), customer_id=CUSTOMER, project_id=str(PROJECTS[0]), role_name="lead", action="update", actor_scope="agent", actor_agent="worker", actor_user_id=USER, metadata={"before": False, "after": True}, created_at="2026-10-02T02:03:04+00:00")),
    ]
    for table, row in items:
        # Fill historical timestamps explicitly, never rely on wall-clock defaults.
        if table == TABLES[1]:
            row.setdefault("updated_at", WHEN)
        else:
            row.setdefault("created_at", WHEN)
            if table != TABLES[0]:
                row.setdefault("updated_at", WHEN)
        await pair["writer"].execute(f"INSERT INTO {table} SELECT * FROM jsonb_populate_record(NULL::{table},$1::jsonb)", json.dumps(row))
    return b.run_id


async def prepare(pair):
    e, a = modules()
    s = await a.read_agent_snapshot(pair["source"], expected_database=pair["source_name"])
    policy = {"version": "legacy-agents.v1", "project_scope_ids": {str(p): str(p) for p in PROJECTS},
              "project_customer_ids": {str(PROJECTS[0]): CUSTOMER}, "record_mappings": {},
              "roster_profiles": {str(PROJECTS[0]): {"profile_id": uid(200), "agent_name": "roster:alpha"}}}
    for i, r in enumerate(s.records):
        value = e.load_json(r.original_bytes); table = r.source_reference.split(":", 1)[0]
        pid = value.get("project_id") or str(PROJECTS[0])
        name = value.get("agent_name") or value.get("name") or "history:lead"
        m = dict(source_project_id=pid, agent_name=name, actor_id=uid(300+i), principal_id=uid(400+i), authority="dormant")
        if value.get("actor_id"):
            m["source_actor_id"] = value["actor_id"]
        if value.get("actor_user_id"):
            m["source_author_user_id"] = value["actor_user_id"]
        if value.get("customer_id"):
            m["customer_id"] = value["customer_id"]
        if table in (TABLES[0], TABLES[3]):
            m["identity_id"] = value["id"]
        else:
            m["profile_id"] = value.get("id") or uid(100+i)
            if table == TABLES[1] and name == "profile-only":
                m["identity_id"] = uid(150)
        policy["record_mappings"][r.source_reference] = m
    _, _, old, _ = await project_inputs(pair)
    b = dataclasses.replace(old, run_id=uuid.uuid4(), source_snapshot_sha256=s.fingerprint,
                            source_catalog_sha256=s.catalog_sha256, source_extensions_sha256=s.extensions_sha256,
                            mapping_version="legacy-agents.v1", policy_sha256=e.policy_digest(policy))
    return e, a, s, b, policy


async def effects(pair, run):
    return tuple(await pair["target"].fetchval(q, run) for q in (
        "SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1",
        "SELECT count(*) FROM cortex_core.import_runs WHERE run_id=$1",
        "SELECT count(*) FROM cortex_core.project_identities WHERE $1::uuid IS NOT NULL",
        "SELECT count(*) FROM cortex_core.project_profiles WHERE $1::uuid IS NOT NULL"))


def test_migration_widens_only_family_check(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            definition = await pair["target"].fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='cortex_core.import_rows'::regclass AND conname='import_rows_family_check'")
            assert "agents" in definition and "projects" in definition
            assert hashlib.sha256((ROOT / "migrations/0022_state_import.sql").read_bytes()).hexdigest() == "67b298e715502d7672c747e2b9e3d97f3a69b8140be7f8462803339faf3af90e"
    asyncio.run(run())


def test_all_classes_native_roster_originals_dormant_and_counted_once(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            project_run = await setup(pair)
            e, a, s, b, p = await prepare(pair)
            result = await e.import_agents(pair["target"], s, b, p)
            assert result["counts"] == {"migrated": 10} and result["functional_pass"] is True
            assert await e.read_inverse(pair["target"], b.run_id) == {r.source_reference:r.original_bytes for r in s.records}
            assert len(s.dependencies) == 12
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1", project_run) == 12
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_identities") == 5
            view = await a.read_native_roster(pair["target"], b.run_id)
            alpha = next(v for v in view if v["source_project_id"] == str(PROJECTS[0]))
            assert alpha["roster_policy"] == ROSTER
            assert [r["original_record"]["action"] for r in alpha["history"]] == ["create", "update"]
            assert {r["source_class"] for v in view for r in v["records"]} == set(TABLES)
            assert all(r["lineage"]["authority"] == "dormant" for v in view for r in v["records"])
            assert next(v for v in view if v["source_project_id"] == str(PROJECTS[2]))["records"] == []
            for _, table in AUTHORITY:
                assert await pair["target"].fetchval(f"SELECT count(*) FROM {table}") == 0
            for row in await pair["target"].fetch("SELECT * FROM cortex_core.import_rows WHERE run_id=$1", b.run_id):
                assert row["family"] == "agents" and json.loads(row["target_references"])
    asyncio.run(run())


def test_identical_replay_and_concurrent_default_repeatable_read(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair); e, a, s, b, p = await prepare(pair)
            other = await pair["connect"](pair["target_name"], "cortex_v2_migrator")
            try:
                for conn in (pair["target"], other):
                    await conn.execute("SET default_transaction_isolation='repeatable read'")
                results = await asyncio.gather(e.import_agents(pair["target"],s,b,p),e.import_agents(other,s,b,p))
                assert results[0] == results[1]
                before = await effects(pair,b.run_id)
                assert await e.import_agents(other,s,b,p) == results[0]
                assert await effects(pair,b.run_id) == before
            finally:
                await other.close()
    asyncio.run(run())


@pytest.mark.parametrize("field", ["source_installation_id", "source_database", "source_snapshot_sha256", "source_catalog_sha256", "source_extensions_sha256", "converter_sha", "target_installation_id", "target_database", "target_schema_sha256", "mapping_version", "policy_sha256"])
def test_changed_binding_refuses(agent_cluster, field):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair); e, a, s, b, p = await prepare(pair)
            await e.import_agents(pair["target"],s,b,p); before=await effects(pair,b.run_id)
            new = uuid.uuid4() if field.endswith("_id") else "changed"
            with pytest.raises(e.ImportRefused):
                await e.import_agents(pair["target"],s,dataclasses.replace(b,**{field:new}),p)
            assert await effects(pair,b.run_id) == before
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_atomic_crash_and_restart(agent_cluster, stage):
    async def run():
        async with agent_cluster() as pair:
            project_run=await setup(pair); e,a,s,b,p=await prepare(pair)
            def fault(point,checkpoint):
                if point==stage and checkpoint==1:
                    raise RuntimeError("Injected fixture crash")
            with pytest.raises(RuntimeError,match="Injected"):
                await e.import_agents(pair["target"],s,b,p,fault=fault)
            before=await effects(pair,b.run_id)
            assert (before==(0,0,0,0)) if stage=="before_commit" else (before[0]>0 and before[2]>0)
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1",project_run)==12
            assert (await e.import_agents(pair["target"],s,b,p))["counts"]=={"migrated":10}
    asyncio.run(run())


@pytest.mark.parametrize("defect", ["missing", "actor", "principal", "customer", "project", "authority", "owner", "history_role"])
def test_missing_conflicting_lineage_or_scope_is_quarantined(agent_cluster,defect):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair); e,a,s,b,p=await prepare(pair)
            ref=next(r for r in p["record_mappings"] if r.startswith("public.agents:")); m=p["record_mappings"][ref]
            if defect=="missing": del p["record_mappings"][ref]
            elif defect=="actor": m["source_actor_id"]=uid(999)
            elif defect=="principal": del m["principal_id"]
            elif defect=="customer": p["project_customer_ids"][str(PROJECTS[0])]=uid(999)
            elif defect=="project": m["source_project_id"]=str(PROJECTS[1])
            elif defect=="authority": m["authority"]="active"
            elif defect=="owner": m["agent_name"]="invented"
            else:
                await pair["writer"].execute("UPDATE cortex.role_audit_events SET role_name='missing-role'")
                s=await a.read_agent_snapshot(pair["source"],expected_database=pair["source_name"])
                b=dataclasses.replace(b,source_snapshot_sha256=s.fingerprint)
            b=dataclasses.replace(b,policy_sha256=e.policy_digest(p))
            result=await e.import_agents(pair["target"],s,b,p)
            assert result["functional_pass"] is False and result["counts"]["quarantined"]>0
            assert await e.read_inverse(pair["target"],b.run_id)=={r.source_reference:r.original_bytes for r in s.records}
            for _,table in AUTHORITY: assert await pair["target"].fetchval(f"SELECT count(*) FROM {table}")==0
    asyncio.run(run())


@pytest.mark.parametrize("collision", ["identity_id", "identity_name", "profile_id", "profile_owner", "duplicate_map"])
def test_unowned_native_collisions_refuse_before_effects(agent_cluster,collision):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair); e,a,s,b,p=await prepare(pair)
            maps=list(p["record_mappings"].items()); agent_ref,m=next((r,m) for r,m in maps if r.startswith("public.agents:")); profile_ref,pm=next((r,m) for r,m in maps if "profile_id" in m)
            if collision=="duplicate_map":
                next(v for k,v in maps if k!=agent_ref and "identity_id" in v)["identity_id"]=m["identity_id"]
                b=dataclasses.replace(b,policy_sha256=e.policy_digest(p))
            elif collision.startswith("identity"):
                await pair["target"].execute("INSERT INTO cortex_core.project_identities VALUES($1,$2,'unowned',$3,'agent','{}')",uuid.UUID(m["identity_id"] if collision=="identity_id" else uid(999)),PROJECTS[0],m["agent_name"] if collision=="identity_name" else "unrelated")
            else:
                await pair["target"].execute("INSERT INTO cortex_core.project_profiles VALUES($1,$2,$3,$4,'{}')",uuid.UUID(pm["profile_id"] if collision=="profile_id" else uid(999)),PROJECTS[0],profile_ref if collision=="profile_owner" else "unowned",pm["agent_name"])
            before=await effects(pair,b.run_id)
            with pytest.raises(e.ImportRefused): await e.import_agents(pair["target"],s,b,p)
            assert await effects(pair,b.run_id)==before
    asyncio.run(run())


@pytest.mark.parametrize("drift", ["identity_name", "profile_owner", "true_to_one", "false_to_zero", "precise_number", "project_roots", "source_context"])
def test_fresh_readback_and_replay_refuse_native_or_context_drift(agent_cluster,drift):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair)
            if drift=="precise_number": await pair["writer"].execute("UPDATE public.agents SET capabilities=capabilities || '{\"precise\":0.123456789012345678901234567890123456}'::jsonb WHERE id=$1",uuid.UUID(uid(10)))
            e,a,s,b,p=await prepare(pair); await e.import_agents(pair["target"],s,b,p)
            if drift=="source_context":
                await pair["writer"].execute("UPDATE public.cortex_projects SET metadata=metadata || '{\"changed\":true}'::jsonb WHERE id=$1",PROJECTS[0])
                changed=await a.read_agent_snapshot(pair["source"],expected_database=pair["source_name"])
                assert changed.fingerprint!=s.fingerprint
                with pytest.raises(e.ImportRefused): await e.import_agents(pair["target"],changed,b,p)
                return
            if drift=="identity_name": await pair["target"].execute("UPDATE cortex_core.project_identities SET identity_name='drift' WHERE identity_id=$1",uuid.UUID(uid(10)))
            elif drift=="profile_owner": await pair["target"].execute("UPDATE cortex_core.project_profiles SET agent_name='drift' WHERE profile_id=$1",uuid.UUID(uid(13)))
            elif drift=="project_roots": await pair["target"].execute("UPDATE cortex_core.project_registry SET roots='[]' WHERE project_scope_id=$1",PROJECTS[0])
            else:
                patch={"true_to_one":'{"capabilities":{"can_write":1,"responsibility":["write","review"]}}',"false_to_zero":'{"runtime_state":{"old":0}}',"precise_number":'{"capabilities":{"can_write":true,"responsibility":["write","review"],"precise":0.12345678901234568}}'}[drift]
                await pair["target"].execute("UPDATE cortex_core.project_identities SET original_record=original_record || $1::jsonb WHERE identity_id=$2",patch,uuid.UUID(uid(10)))
            for op in (e.read_inverse(pair["target"],b.run_id), e.import_agents(pair["target"],s,b,p), a.read_native_roster(pair["target"],b.run_id)):
                with pytest.raises(e.ImportRefused): await op
    asyncio.run(run())


def test_source_and_family_entrypoint_fences(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair); e,a,s,b,p=await prepare(pair)
            for name in ("cortex", "live_legacy", "legacy_restore_other"):
                with pytest.raises(e.ImportRefused): await a.read_agent_snapshot(pair["source"],expected_database=name)
            with pytest.raises(e.ImportRefused): await e.import_projects(pair["target"],s,b,p)
            assert await effects(pair,b.run_id)==(0,0,0,0)
            await pair["writer"].execute("DROP TABLE public.agents")
            with pytest.raises(e.ImportRefused): await a.read_agent_snapshot(pair["source"],expected_database=pair["source_name"])
    asyncio.run(run())
