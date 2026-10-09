"""Real PG; explicitly synthetic C03 Core/fact/job and C04 registry ports."""
import asyncio
import hashlib
import json
import os
import unittest
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx
from cortex_core.embeddings.pg_search import CoreUnavailable
from cortex_core.modules.graph.pg_graph import (
    Edge, Extraction, GraphScope, GraphUnavailable, Node, PostgresGraph, Source,
)
from cortex_core.modules.graph.routes import graph_routes

ROOT = Path(__file__).resolve().parents[2]
TA, TB, PA, PB, PC = [UUID(int=x) for x in (1, 2, 11, 12, 13)]
SCOPES = {"alice": GraphScope(TA, PA, "alpha", "/approved/alpha"),
          "bob": GraphScope(TB, PB, "beta", "/approved/beta"),
          "alice-other": GraphScope(TA, PC, "gamma", "/approved/gamma")}
IDENTITY = "existing-domain-extractor:v1"


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin = await asyncpg.connect(os.environ["SEARCH_TEST_DSN"])
        await self.admin.execute("DROP SCHEMA IF EXISTS retrieval CASCADE; DROP SCHEMA IF EXISTS core CASCADE; DROP SCHEMA IF EXISTS coordination CASCADE; DROP TABLE IF EXISTS public.graph_test_grants")
        await self.admin.execute("""
          CREATE SCHEMA core; CREATE SCHEMA coordination;
          CREATE TABLE core.projects(tenant_id uuid,id uuid,name text,PRIMARY KEY(tenant_id,id));
          CREATE TABLE core.payloads(tenant_id uuid,project_id uuid,id uuid,body bytea,sha256 text,
            PRIMARY KEY(tenant_id,project_id,id), CHECK(sha256=encode(sha256(body),'hex')));
          CREATE TABLE core.records(tenant_id uuid,project_id uuid,id uuid,kind text,current_revision bigint,tombstone boolean,
            PRIMARY KEY(tenant_id,project_id,id),FOREIGN KEY(tenant_id,project_id) REFERENCES core.projects(tenant_id,id));
          CREATE TABLE core.record_revisions(tenant_id uuid,project_id uuid,record_id uuid,revision bigint,
            payload_ref uuid,tombstone boolean,PRIMARY KEY(tenant_id,project_id,record_id,revision),
            FOREIGN KEY(tenant_id,project_id,record_id) REFERENCES core.records(tenant_id,project_id,id),
            FOREIGN KEY(tenant_id,project_id,payload_ref) REFERENCES core.payloads(tenant_id,project_id,id));
          CREATE TABLE core.extraction_facts(tenant_id uuid,project_id uuid,id uuid,record_id uuid,source_revision bigint,
            extractor_identity text,payload_ref uuid,created_at timestamptz DEFAULT now(),PRIMARY KEY(tenant_id,project_id,id),
            FOREIGN KEY(tenant_id,project_id,record_id,source_revision) REFERENCES core.record_revisions(tenant_id,project_id,record_id,revision),
            FOREIGN KEY(tenant_id,project_id,payload_ref) REFERENCES core.payloads(tenant_id,project_id,id));
          CREATE TABLE coordination.jobs(tenant_id uuid,project_id uuid,id uuid,kind text,payload_ref uuid,
            idempotency_key text,state text DEFAULT 'pending',cancel_requested boolean DEFAULT false,
            created_at timestamptz DEFAULT now(), PRIMARY KEY(tenant_id,project_id,id),
            FOREIGN KEY(tenant_id,project_id,payload_ref) REFERENCES core.payloads(tenant_id,project_id,id));
          CREATE TABLE public.graph_test_grants(subject text PRIMARY KEY,tenant_id uuid,project_id uuid,
            project_key text,repo text,control boolean,writer boolean);
        """)
        await self.admin.execute((ROOT / "schema/retrieval/003-pg-graph.sql").read_text())
        if not await self.admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname='graph_runtime'"):
            await self.admin.execute("CREATE ROLE graph_runtime LOGIN NOSUPERUSER NOBYPASSRLS")
        await self.admin.execute("GRANT USAGE ON SCHEMA core,coordination,retrieval TO graph_runtime; GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA core,coordination,retrieval TO graph_runtime; GRANT SELECT ON public.graph_test_grants TO graph_runtime")
        for schema, table in [("core", t) for t in ("payloads", "records", "record_revisions", "extraction_facts")] + [("coordination", "jobs")]:
            await self.admin.execute(f"""ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY;
              ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY;
              CREATE POLICY scope ON {schema}.{table} USING(
                tenant_id=nullif(current_setting('cortex.tenant_id',true),'')::uuid AND
                project_id=nullif(current_setting('cortex.project_id',true),'')::uuid)""")
        for subject, scope in SCOPES.items():
            await self.admin.execute("INSERT INTO core.projects VALUES($1,$2,$3)", scope.tenant_id, scope.project_id, scope.project_key)
            await self.admin.execute("INSERT INTO public.graph_test_grants VALUES($1,$2,$3,$4,$5,true,true)", subject, scope.tenant_id, scope.project_id, scope.project_key, scope.repo)
        self.pool = await asyncpg.create_pool(os.environ["SEARCH_TEST_DSN"].replace("postgres@", "graph_runtime@"), min_size=1, max_size=3)
        self.auth_calls = []

        def authorization(operation):
            async def authorize(conn, subject):
                self.auth_calls.append((operation, subject))
                row = await conn.fetchrow("SELECT * FROM public.graph_test_grants WHERE subject=$1", subject)
                if row is None or (operation != "read" and not row[operation]):
                    raise PermissionError("revoked")
                return GraphScope(row["tenant_id"], row["project_id"], row["project_key"], row["repo"])
            return authorize

        async def load(conn, scope, references):
            rows = await conn.fetch("""SELECT r.*,p.body FROM core.records r JOIN core.record_revisions v
                ON (v.tenant_id,v.project_id,v.record_id,v.revision)=(r.tenant_id,r.project_id,r.id,r.current_revision)
                JOIN core.payloads p ON (p.tenant_id,p.project_id,p.id)=(v.tenant_id,v.project_id,v.payload_ref)
                WHERE NOT r.tombstone AND r.id=ANY($1::uuid[]) ORDER BY r.id""", [r.record_id for r in references])
            return [Source(r["id"], r["current_revision"], r["kind"], **json.loads(bytes(r["body"]))) for r in rows]

        async def extractor(source, options):
            return Extraction(IDENTITY, (Node(source.label, "concept", source.description),
                         Node("service", "service", "current service"), Node("file.py", "file", "current file")),
                         (Edge(source.label, "file.py", "uses", "current relationship"),))

        async def sink(conn, scope, source, facts):
            payload = json.dumps(asdict(facts)).encode()
            pid = uuid4()
            await conn.execute("INSERT INTO core.payloads VALUES($1,$2,$3,$4,$5)", scope.tenant_id, scope.project_id, pid, payload, hashlib.sha256(payload).hexdigest())
            fid = uuid4()
            await conn.execute("INSERT INTO core.extraction_facts VALUES($1,$2,$3,$4,$5,$6,$7)", scope.tenant_id, scope.project_id, fid, source.record_id, source.revision, facts.identity, pid)
            return fid

        async def enqueue(conn, scope, request):
            jid, pid = uuid4(), uuid4()
            payload = json.dumps(request).encode()
            await conn.execute("INSERT INTO core.payloads VALUES($1,$2,$3,$4,$5)", scope.tenant_id, scope.project_id, pid, payload, hashlib.sha256(payload).hexdigest())
            await conn.execute("INSERT INTO coordination.jobs(tenant_id,project_id,id,kind,payload_ref,idempotency_key) VALUES($1,$2,$3,'graph.build',$4,$5)", scope.tenant_id, scope.project_id, jid, pid, str(jid))
            return jid

        async def job(conn, scope, jid):
            row = await conn.fetchrow("SELECT id,state FROM coordination.jobs WHERE tenant_id=$1 AND project_id=$2 AND id=$3", scope.tenant_id, scope.project_id, jid)
            return None if row is None else {"id": str(row["id"]), "project": scope.project_key, "status": "queued" if row["state"] == "pending" else row["state"]}

        self.options = dict(authorize_read=authorization("read"), authorize_control=authorization("control"),
            authorize_writer=authorization("writer"), extractor_identity=IDENTITY, source_reader=load,
            extractor=extractor, fact_sink=sink, enqueue_job=enqueue, read_job=job)
        self.graph = PostgresGraph(self.pool, **self.options)
        for subject in SCOPES:
            await self.graph.configure(subject, "ready")

    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.close()

    async def record(self, subject="alice", rid=None, revision=1, kind="decision", label="alpha", tombstone=False):
        scope = SCOPES[subject]
        rid, pid = rid or uuid4(), uuid4()
        payload = json.dumps({"label": label, "description": "Core description", "content": "Core source"}).encode()
        async with self.admin.transaction():
            await self.admin.execute("INSERT INTO core.payloads VALUES($1,$2,$3,$4,$5)", scope.tenant_id, scope.project_id, pid, payload, hashlib.sha256(payload).hexdigest())
            await self.admin.execute("INSERT INTO core.records VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(tenant_id,project_id,id) DO UPDATE SET current_revision=EXCLUDED.current_revision,tombstone=EXCLUDED.tombstone", scope.tenant_id, scope.project_id, rid, kind, revision, tombstone)
            await self.admin.execute("INSERT INTO core.record_revisions VALUES($1,$2,$3,$4,$5,$6)", scope.tenant_id, scope.project_id, rid, revision, pid, tombstone)
        return rid

    async def project(self, subject="alice", **kw):
        await self.record(subject, **kw)
        return await self.graph.extract(subject, dry_run=False)

    async def test_current_caller_envelopes_and_provenance(self):
        await self.project()
        memory = await self.graph.memory("alice")
        self.assertEqual({n["name"] for n in memory["nodes"]}, {"alpha", "service", "file.py"})
        self.assertEqual(len(memory["edges"]), 1)
        self.assertEqual(memory["sources"][0]["source_table"], "decisions")
        self.assertEqual(memory["sources"][0]["label"], "alpha")
        self.assertEqual(len(memory["source_edges"]), 3)
        stats = await self.graph.stats("alice")
        self.assertEqual((stats["entity_count"], stats["relationship_count"]), (3, 1))
        self.assertEqual(stats["source_counts"]["decisions"], 1)
        self.assertEqual(stats["freshness"]["state"], "current")
        result = await self.graph.search("alice", "alpha", expand=True)
        self.assertEqual(result["project"], "alpha")
        self.assertEqual(len(result["relationships"]), 1)
        self.assertTrue(result["high_level"])
        self.assertTrue(result["low_level"])
        self.assertEqual((await self.graph.search("alice", "' OR 1=1 --"))["high_level"], [])

    async def test_revision_and_tombstone_never_serve_stale_graph(self):
        rid = await self.record()
        await self.graph.extract("alice", dry_run=False)
        await self.record(rid=rid, revision=2, label="new")
        self.assertEqual((await self.graph.memory("alice"))["nodes"], [])
        self.assertEqual((await self.graph.stats("alice"))["freshness"]["pending_records"], 1)
        await self.graph.extract("alice", dry_run=False)
        await self.record(rid=rid, revision=3, tombstone=True)
        self.assertEqual((await self.graph.memory("alice"))["nodes"], [])

    async def test_tenant_project_rls_and_fresh_grants(self):
        await self.project()
        await self.project("bob", label="secret-b")
        await self.project("alice-other", label="secret-c")
        self.assertEqual((await self.graph.search("alice", "secret"))["high_level"], [])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM retrieval.graph_nodes"), 0)
        flags = await self.admin.fetch("SELECT relrowsecurity,relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='retrieval' AND relkind='r'")
        self.assertTrue(flags and all(r[0] and r[1] for r in flags))
        await self.admin.execute("DELETE FROM public.graph_test_grants WHERE subject='alice'")
        with self.assertRaises(PermissionError):
            await self.graph.memory("alice")

    async def test_unknown_core_revision_refused_by_fk(self):
        await self.project()
        fid = await self.admin.fetchval("SELECT id FROM core.extraction_facts LIMIT 1")
        state = await self.admin.fetchrow("SELECT active_generation FROM retrieval.graph_state WHERE tenant_id=$1 AND project_id=$2", TA, PA)
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.admin.execute("INSERT INTO retrieval.graph_applied(tenant_id,project_id,generation,record_id,source_revision,source_kind,source_label,source_description,fact_id) VALUES($1,$2,$3,$4,1,'decision','bad','',$5)", TA, PA, state[0], uuid4(), fid)

    async def test_empty_and_missing_ports_are_distinct_from_unavailable(self):
        self.assertEqual((await self.graph.memory("alice"))["nodes"], [])
        await self.graph.configure("alice", "disabled")
        with self.assertRaises(GraphUnavailable):
            await self.graph.memory("alice")
        await self.graph.configure("alice", "ready")
        missing = PostgresGraph(self.pool, **{**self.options, "fact_sink": None})
        await self.record()
        with self.assertRaises(GraphUnavailable):
            await missing.extract("alice", dry_run=False)

    async def test_dry_run_does_not_extract_or_persist_facts(self):
        await self.record()
        result = await self.graph.extract("alice")
        self.assertEqual((result["status"], result["selected"], result["processed"]), ("dry-run", 1, 0))
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), 0)

    async def stalled_extract(self, change):
        rid = await self.record()
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = self.options["extractor"]
        async def extractor(source, options):
            entered.set()
            await proceed.wait()
            return await original(source, options)
        graph = PostgresGraph(self.pool, **{**self.options, "extractor": extractor})
        task = asyncio.create_task(graph.extract("alice", dry_run=False))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await change(rid)
            proceed.set()
            return await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_stale_source_completion_cannot_publish(self):
        async def change(rid):
            await self.record(rid=rid, revision=2)
        result = await self.stalled_extract(change)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["errors"][0]["error"], "stale_source")
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), 0)

    async def test_revoked_writer_during_extraction_cannot_publish(self):
        async def change(rid):
            await self.admin.execute("UPDATE public.graph_test_grants SET writer=false WHERE subject='alice'")
        with self.assertRaises(PermissionError):
            await self.stalled_extract(change)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), 0)

    async def test_changed_generation_completion_cannot_publish(self):
        async def change(rid):
            await self.graph.configure("alice", "ready")
        result = await self.stalled_extract(change)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["errors"][0]["error"], "stale_generation")
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), 0)

    async def test_fact_and_projection_share_rollback(self):
        await self.record()
        original = self.options["fact_sink"]
        async def broken(conn, scope, source, facts):
            await original(conn, scope, source, facts)
            raise RuntimeError("synthetic dependency secret")
        graph = PostgresGraph(self.pool, **{**self.options, "fact_sink": broken})
        result = await graph.extract("alice", dry_run=False)
        self.assertEqual(result["processed"], 0)
        self.assertNotIn("synthetic dependency secret", json.dumps(result))
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), 0)
        self.assertEqual((await self.graph.memory("alice"))["nodes"], [])

    async def test_scoped_prune_preserves_current_graph_other_projects_and_core(self):
        await self.project()
        await self.project("bob")
        await self.graph.configure("alice", "ready")
        await self.graph.extract("alice", dry_run=False)
        facts = await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts")
        generations = await self.admin.fetchval("SELECT count(*) FROM retrieval.graph_generations")
        preview = await self.graph.prune("alice")
        self.assertEqual(len(preview["candidates"]), 1)
        self.assertEqual(preview["pruned"], [])
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM retrieval.graph_generations"), generations)
        await self.graph.prune("alice", dry_run=False)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM retrieval.graph_generations"), generations-1)
        self.assertEqual((await self.graph.stats("alice"))["entity_count"], 3)
        self.assertEqual((await self.graph.stats("bob"))["entity_count"], 3)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), facts)
        await self.admin.execute("UPDATE public.graph_test_grants SET control=false WHERE subject='alice'")
        with self.assertRaises(PermissionError):
            await self.graph.prune("alice", dry_run=False)

    async def test_durable_build_intent_survives_adapter_restart_and_is_scoped(self):
        queued = await self.graph.build("alice", {"repo": "alpha", "full": True})
        self.assertEqual(queued["status"], "queued")
        restarted = PostgresGraph(self.pool, **self.options)
        status = await restarted.job("alice", queued["job_id"])
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "queued")
        self.assertIsNone(await restarted.job("bob", queued["job_id"]))
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM coordination.jobs"), 1)
        with self.assertRaises(PermissionError):
            await self.graph.build("alice", {"repo": "/unapproved", "full": True})

    async def test_build_missing_executor_is_unavailable_not_fake_success(self):
        with self.assertRaises(GraphUnavailable):
            await self.graph.build("alice", {"repo": "alpha", "sync": True, "embed": False})

    async def test_pending_selection_advances_past_already_applied_records(self):
        await self.record(rid=UUID(int=100))
        await self.record(rid=UUID(int=101), label="second")
        first = await self.graph.extract("alice", dry_run=False, limit=1)
        second = await self.graph.extract("alice", dry_run=False, limit=1)
        self.assertEqual((first["processed"], second["processed"]), (1, 1))
        self.assertEqual((await self.graph.stats("alice"))["freshness"]["pending_records"], 0)

    async def test_whole_read_budget_bounds_stalled_core_hydration(self):
        await self.record()
        async def stalled(*args):
            await asyncio.Event().wait()
        graph = PostgresGraph(self.pool, **{**self.options, "source_reader": stalled})
        start = asyncio.get_running_loop().time()
        try:
            with self.assertRaises(GraphUnavailable):
                await asyncio.wait_for(graph.extract("alice"), 2.7)
        except TimeoutError:
            self.fail("Core hydration outlived the whole graph read budget")
        self.assertLess(asyncio.get_running_loop().time()-start, 2.5)

    async def test_serialization_retry_rechecks_writer_and_rolls_back_facts(self):
        await self.record()
        original = self.options["fact_sink"]
        calls = 0
        async def racing(conn, scope, source, facts):
            nonlocal calls
            calls += 1
            fid = await original(conn, scope, source, facts)
            if calls == 1:
                raise asyncpg.SerializationError("synthetic concurrent update")
            return fid
        graph = PostgresGraph(self.pool, **{**self.options, "fact_sink": racing})
        self.assertEqual((await graph.extract("alice", dry_run=False))["processed"], 1)
        self.assertEqual(calls, 2)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), 1)
        self.assertGreaterEqual(self.auth_calls.count(("writer", "alice")), 2)

    async def test_extraction_cancellation_releases_all_connections(self):
        await self.record()
        entered = asyncio.Event()
        async def stalled(source, options):
            entered.set()
            await asyncio.Event().wait()
        graph = PostgresGraph(self.pool, **{**self.options, "extractor": stalled})
        task = asyncio.create_task(graph.extract("alice", dry_run=False))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async def use_connection():
            async with self.pool.acquire(timeout=0.2) as conn:
                return await conn.fetchval("SELECT 1")
        self.assertEqual(await asyncio.gather(*(use_connection() for _ in range(3))), [1, 1, 1])
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), 0)

    async def test_sync_build_accepts_only_complete_count_bound_legacy_receipt(self):
        await self.record(kind="code")
        async def execute(subject, scope, options):
            await self.graph.extract(subject, dry_run=False)
            stats = await self.graph.stats(subject)
            return {"status": "ok", "build_type": "full", "summary": "fixture Core records projected",
                    "files_parsed": 1, "errors": [], "total_nodes": stats["entity_count"], "total_edges": stats["relationship_count"]}
        graph = PostgresGraph(self.pool, **{**self.options, "build_executor": execute})
        result = await graph.build("alice", {"repo": "alpha", "full": True, "sync": True, "embed": False})
        self.assertEqual((result["status"], result["total_nodes"]), ("ok", 3))
        async def partial(*args):
            return {**result, "errors": ["synthetic parse failure"]}
        broken = PostgresGraph(self.pool, **{**self.options, "build_executor": partial})
        with self.assertRaises(GraphUnavailable):
            await broken.build("alice", {"repo": "alpha", "full": True, "sync": True, "embed": False})

    async def test_large_neighborhood_and_provenance_are_clipped_explicitly(self):
        for rid in (UUID(int=201), UUID(int=202)):
            await self.record(rid=rid)
        async def large(source, options):
            prefix = str(source.record_id)
            names = [f"{prefix}:{i:04d}" for i in range(800)]
            return Extraction(IDENTITY, tuple(Node(n, "concept") for n in names),
                              tuple(Edge(names[0], n, "uses") for n in names[1:]))
        graph = PostgresGraph(self.pool, **{**self.options, "extractor": large})
        await graph.extract("alice", dry_run=False)
        memory = await graph.memory("alice", limit=1000)
        self.assertEqual(len(memory["nodes"]), 1000)
        self.assertTrue(memory["truncated"])
        names = {n["name"] for n in memory["nodes"]}
        self.assertTrue(all(e["source"] in names and e["target"] in names for e in memory["edges"]))
        result = await graph.search("alice", "0000", expand=True, depth=3, limit=1000)
        self.assertLessEqual(len(result["high_level"])+len(result["low_level"]), 1000)
        self.assertTrue(result["truncated"])

    async def test_projection_requires_persisted_canonical_fact_receipt(self):
        await self.record()
        async def noop(*args):
            return None
        graph = PostgresGraph(self.pool, **{**self.options, "fact_sink": noop})
        result = await graph.extract("alice", dry_run=False)
        self.assertEqual(result["processed"], 0)
        self.assertEqual((await self.graph.memory("alice"))["nodes"], [])

    async def test_rebuild_uses_canonical_facts_without_extractor_or_new_fact(self):
        await self.project()
        count = await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts")
        await self.graph.configure("alice", "ready")
        graph = PostgresGraph(self.pool, **{**self.options, "extractor": None, "fact_sink": None})
        result = await graph.rebuild("alice")
        self.assertEqual(result["processed"], 1)
        self.assertEqual((await graph.stats("alice"))["entity_count"], 3)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), count)

    async def test_wrong_extractor_identity_is_unavailable(self):
        await self.project()
        graph = PostgresGraph(self.pool, **{**self.options, "extractor_identity": "other:v2"})
        with self.assertRaises(GraphUnavailable):
            await graph.memory("alice")

    async def test_canonical_payload_must_match_projected_facts(self):
        await self.record()
        async def wrong(conn, scope, source, facts):
            pid, fid = uuid4(), uuid4()
            payload = json.dumps({"identity": IDENTITY, "nodes": [], "edges": []}).encode()
            await conn.execute("INSERT INTO core.payloads VALUES($1,$2,$3,$4,$5)", scope.tenant_id, scope.project_id, pid, payload, hashlib.sha256(payload).hexdigest())
            await conn.execute("INSERT INTO core.extraction_facts(tenant_id,project_id,id,record_id,source_revision,extractor_identity,payload_ref) VALUES($1,$2,$3,$4,$5,$6,$7)", scope.tenant_id, scope.project_id, fid, source.record_id, source.revision, facts.identity, pid)
            return fid
        graph = PostgresGraph(self.pool, **{**self.options, "fact_sink": wrong})
        result = await graph.extract("alice", dry_run=False)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM core.extraction_facts"), 0)
        self.assertEqual((await graph.memory("alice"))["nodes"], [])

    async def test_bounds_and_invalid_modes_are_refused(self):
        for kwargs in [{"depth": 4}, {"limit": 0}, {"limit": 1001}]:
            with self.assertRaises(ValueError):
                await self.graph.search("alice", "alpha", **kwargs)
        with self.assertRaises(ValueError):
            await self.graph.extract("alice", limit=1001)
        with self.assertRaises(ValueError):
            await self.graph.build("alice", {"repo": "alpha", "full": True, "import_existing": True})

    async def test_core_outage_is_explicit(self):
        await self.pool.close()
        with self.assertRaises(CoreUnavailable):
            await self.graph.stats("alice")

    async def test_http_routes_seal_principal_and_validate_selectors(self):
        await self.project()
        async def principal(request):
            if request.headers.get("Authorization") != "Bearer synthetic-alice":
                raise PermissionError("unauthenticated")
            return "alice"
        app = graph_routes(self.graph, principal)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            headers = {"Authorization": "Bearer synthetic-alice", "X-Project": "alpha"}
            for path in ["/cortex-graph/stats", "/cortex-graph/memory", "/cortex-graph-search?q=alpha&expand=true", "/graph/stats"]:
                self.assertEqual((await client.get(path, headers=headers)).status_code, 200)
            self.assertEqual((await client.get("/cortex-graph/memory", headers={"X-Project": "alpha"})).status_code, 403)
            self.assertEqual((await client.get("/cortex-graph/memory", headers={**headers, "X-Project": "beta"})).status_code, 403)
            self.assertEqual((await client.post("/cortex-graph-extract", headers=headers, json={"dry_run": True})).json()["status"], "dry-run")
            self.assertEqual((await client.post("/graph/prune", headers=headers, json={"dry_run": True})).status_code, 200)
            queued = await client.post("/graph/build", headers=headers, json={"repo": "alpha", "full": True})
            self.assertEqual(queued.status_code, 202)
            self.assertEqual((await client.get(queued.json()["status_url"], headers=headers)).json()["status"], "queued")
            self.assertEqual((await client.get("/cortex-graph-search?q=alpha&depth=4", headers=headers)).status_code, 400)
            stats = (await client.get("/graph/stats", headers=headers)).json()
            self.assertEqual((stats["total_nodes"], stats["total_edges"]), (3, 1))
            self.assertTrue(stats["repos"][0]["path"].startswith("pg://"))
