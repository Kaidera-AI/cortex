"""R148: projects must reach native tables before their ledger says migrated."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import json
import uuid
from datetime import datetime

import pytest

from fixtures.state_import_native import INSTALLATION, state_cluster

PROJECTS = [uuid.UUID(f"14800000-0000-4000-8000-{n:012d}") for n in (10, 11, 12)]
SOURCE_INSTALLATION = uuid.UUID("14800000-0000-4000-8000-000000000002")
WHEN = "2026-10-01T02:03:04+00:00"
CONVERTER = "7bba356f2dfe602929d6a6dc53ac984edc170247"


def modules():
    for name in ("cortex_v2.state_import", "cortex_v2.legacy_projects"):
        assert importlib.util.find_spec(name) is not None, f"Missing native importer: {name}"
    return (importlib.import_module("cortex_v2.state_import"),
            importlib.import_module("cortex_v2.legacy_projects"))


async def seed(pair, *, defect=None):
    for index, identity in enumerate(PROJECTS):
        key = ("alpha", "archived", "empty")[index]
        roots = [{"path": f"/fixture/{key}/z", "kind": "secondary", "note": "original λ"},
                 {"path": f"/fixture/{key}", "kind": "primary"},
                 {"path": f"/fixture/{key}/a", "kind": "secondary"}]
        metadata = {"roots": roots, "aliases": [f"{key}-old"], "kept": {"original": "unchanged"}}
        if defect == "missing_roots" and index == 0:
            metadata.pop("roots")
        if defect == "mismatched_roots" and index == 0:
            metadata["roots"] = roots[:2]
        await pair["writer"].execute(
            "INSERT INTO public.cortex_projects VALUES($1,$2,$3,$4,$5,'repo',$6,$7,$8::jsonb,$9::timestamptz,$9::timestamptz)",
            identity, key, f"Original {key}", "alpha" if index else None,
            f"/fixture/{key}", "archived" if index == 1 else "active",
            None if index == 2 else "fixture-agent", json.dumps(metadata), datetime.fromisoformat(WHEN),
        )
        for root_index, root in enumerate(roots):
            path_id = uuid.UUID(f"14800000-0000-4000-8000-{100 + index * 10 + root_index:012d}")
            await pair["writer"].execute(
                "INSERT INTO public.cortex_project_paths VALUES($1,$2,$3,$4,$5::jsonb,$6::timestamptz)",
                path_id, key, root["path"], root["kind"],
                json.dumps({k: v for k, v in root.items() if k not in ("path", "kind")}), datetime.fromisoformat(WHEN),
            )


async def inputs(pair):
    engine, projects = modules()
    snapshot = await projects.read_project_snapshot(pair["source"], expected_database=pair["source_name"])
    policy = {"version": "legacy-projects.v1", "project_scope_ids": {str(p): str(p) for p in PROJECTS}}
    binding = engine.RunBinding(
        run_id=uuid.uuid4(), source_installation_id=SOURCE_INSTALLATION,
        source_database=pair["source_name"], source_snapshot_sha256=snapshot.fingerprint,
        source_catalog_sha256=snapshot.catalog_sha256,
        source_extensions_sha256=snapshot.extensions_sha256, converter_sha=CONVERTER,
        target_installation_id=INSTALLATION, target_database=pair["target_name"],
        target_schema_sha256=await engine.native_schema_digest(pair["target"]),
        mapping_version="legacy-projects.v1", policy_sha256=engine.policy_digest(policy),
    )
    return engine, snapshot, binding, policy


async def ledger(pair):
    return await pair["target"].fetch(
        "SELECT to_jsonb(r)::text AS original FROM cortex_core.import_rows r ORDER BY ordinal"
    )


def test_native_projects_aliases_archives_empty_roots_and_inverse(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            result = await engine.import_projects(pair["target"], snapshot, binding, policy, batch_size=1)
            assert result["counts"] == {"migrated": 12}
            assert result["functional_pass"] is True
            assert result["checkpoint"] == 3
            rows = await pair["target"].fetch("SELECT * FROM cortex_core.project_registry ORDER BY original_project_id")
            assert len(rows) == 3 and {r["project_scope_id"] for r in rows} == set(PROJECTS)
            for row in rows:
                key = {str(p): k for p, k in zip(PROJECTS, ("alpha", "archived", "empty"))}[row["original_project_id"]]
                assert json.loads(row["roots"]) == [
                    {"path": f"/fixture/{key}", "kind": "primary"},
                    {"path": f"/fixture/{key}/a", "kind": "secondary"},
                    {"path": f"/fixture/{key}/z", "kind": "secondary", "note": "original λ"},
                ]
                assert row["default_agent"] == (None if key == "empty" else "fixture-agent")
                assert row["created_at"].isoformat() == row["updated_at"].isoformat() == WHEN
            scopes = await pair["target"].fetch("SELECT scope_id,is_active FROM cortex_core.scopes")
            assert {r["scope_id"]: r["is_active"] for r in scopes} == {PROJECTS[0]: True, PROJECTS[1]: False, PROJECTS[2]: True}
            aliases = await pair["target"].fetch("SELECT alias,is_primary FROM cortex_core.scope_aliases")
            assert {r["alias"]: r["is_primary"] for r in aliases} == {k: True for k in ("alpha", "archived", "empty")} | {k: False for k in ("alpha-old", "archived-old", "empty-old")}
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_profiles") == 0
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_identities") == 0
            source = {}
            for table in ("cortex_projects", "cortex_project_paths"):
                for row in await pair["writer"].fetch(f"SELECT id::text AS key,to_jsonb(t)::text AS original FROM public.{table} t"):
                    source[f"public.{table}:{row['key']}"] = row["original"].encode()
            inverse = await engine.read_inverse(pair["target"], binding.run_id)
            assert inverse == source
            stored = await pair["target"].fetch("SELECT source_reference,original_bytes,source_sha256,outcome,target_references FROM cortex_core.import_rows")
            for row in stored:
                assert row["original_bytes"] == source[row["source_reference"]]
                assert row["source_sha256"] == hashlib.sha256(row["original_bytes"]).digest()
                assert row["outcome"] == "migrated" and json.loads(row["target_references"])
    asyncio.run(run())


def test_identical_replay_is_durable_noop(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            first = await engine.import_projects(pair["target"], snapshot, binding, policy)
            before = await ledger(pair)
            events = await pair["target"].fetch("SELECT to_jsonb(r)::text FROM cortex_core.import_runs r ORDER BY event_seq")
            second = await pair["connect"](pair["target_name"], "cortex_v2_migrator")
            try:
                again = await engine.import_projects(second, snapshot, binding, policy)
            finally:
                await second.close()
            assert again == first and await ledger(pair) == before
            assert await pair["target"].fetch("SELECT to_jsonb(r)::text FROM cortex_core.import_runs r ORDER BY event_seq") == events
    asyncio.run(run())


@pytest.mark.parametrize("field", ["source_installation_id", "source_database", "source_snapshot_sha256", "source_catalog_sha256", "source_extensions_sha256", "converter_sha", "target_installation_id", "target_database", "target_schema_sha256", "mapping_version", "policy_sha256"])
def test_changed_run_binding_refuses_without_effects(state_cluster, field):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            await engine.import_projects(pair["target"], snapshot, binding, policy)
            before = await ledger(pair)
            value = uuid.uuid4() if field.endswith("_id") else "changed-binding"
            with pytest.raises(engine.ImportRefused):
                await engine.import_projects(pair["target"], snapshot, dataclasses.replace(binding, **{field: value}), policy)
            assert await ledger(pair) == before
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_crash_checkpoint_and_restart(state_cluster, stage):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            def crash(point, checkpoint):
                if point == stage and checkpoint == 1:
                    raise RuntimeError("Injected owned fixture crash")
            with pytest.raises(RuntimeError, match="Injected"):
                await engine.import_projects(pair["target"], snapshot, binding, policy, batch_size=1, fault=crash)
            count = await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_registry")
            assert count == (0 if stage == "before_commit" else 1)
            assert len(await ledger(pair)) == (0 if stage == "before_commit" else 4)
            result = await engine.import_projects(pair["target"], snapshot, binding, policy, batch_size=1)
            assert result["counts"] == {"migrated": 12} and result["functional_pass"] is True
    asyncio.run(run())


def test_concurrent_importers_share_one_checkpoint(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            other = await pair["connect"](pair["target_name"], "cortex_v2_migrator")
            try:
                results = await asyncio.gather(*(engine.import_projects(conn, snapshot, binding, policy, batch_size=1) for conn in (pair["target"], other)))
            finally:
                await other.close()
            assert results[0] == results[1] and results[0]["counts"] == {"migrated": 12}
            assert len(await ledger(pair)) == 12
    asyncio.run(run())


@pytest.mark.parametrize("defect", ["missing_roots", "mismatched_roots"])
def test_bad_project_keeps_originals_without_native_migration(state_cluster, defect):
    async def run():
        async with state_cluster() as pair:
            await seed(pair, defect=defect)
            engine, snapshot, binding, policy = await inputs(pair)
            result = await engine.import_projects(pair["target"], snapshot, binding, policy)
            assert result["counts"] == {"migrated": 8, "quarantined": 4}
            assert result["functional_pass"] is False
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_registry WHERE project_scope_id=$1", PROJECTS[0]) == 0
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE outcome='quarantined' AND reason IS NOT NULL AND octet_length(original_bytes)>0") == 4
    asyncio.run(run())


def test_native_target_drift_cannot_be_hidden_by_original_ledger(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            await engine.import_projects(pair["target"], snapshot, binding, policy)
            before = await ledger(pair)
            await pair["target"].execute("UPDATE cortex_core.project_registry SET display_name='Changed fixture' WHERE project_scope_id=$1", PROJECTS[0])
            with pytest.raises(engine.ImportRefused):
                await engine.read_inverse(pair["target"], binding.run_id)
            with pytest.raises(engine.ImportRefused):
                await engine.import_projects(pair["target"], snapshot, binding, policy)
            assert await ledger(pair) == before
    asyncio.run(run())


def test_source_reader_refuses_live_name_and_is_readonly(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, projects = modules()
            with pytest.raises(engine.ImportRefused):
                await projects.read_project_snapshot(pair["source"], expected_database="platform_agent_memory")
            with pytest.raises(Exception):
                await pair["source"].execute("DELETE FROM public.cortex_projects")
            assert await pair["writer"].fetchval("SELECT count(*) FROM public.cortex_projects") == 3
            snapshot = await projects.read_project_snapshot(pair["source"], expected_database=pair["source_name"])
            assert len(snapshot.records) == 12
    asyncio.run(run())


def test_orphan_path_is_accounted_and_quarantined(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            await pair["writer"].execute("INSERT INTO public.cortex_project_paths VALUES($1,'absent','/fixture/absent','primary','{}'::jsonb,$2::timestamptz)", uuid.uuid4(), datetime.fromisoformat(WHEN))
            engine, snapshot, binding, policy = await inputs(pair)
            result = await engine.import_projects(pair["target"], snapshot, binding, policy)
            assert result["counts"] == {"migrated": 12, "quarantined": 1}
            assert result["functional_pass"] is False and len(await ledger(pair)) == 13
            assert await pair["target"].fetchval("SELECT reason FROM cortex_core.import_rows WHERE outcome='quarantined'") == "orphan_project_path"
    asyncio.run(run())


def test_ambiguous_preexisting_target_is_not_overwritten(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            await pair["target"].execute("INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES($1,'project','Unrelated fixture')", PROJECTS[0])
            with pytest.raises(engine.ImportRefused):
                await engine.import_projects(pair["target"], snapshot, binding, policy)
            assert await pair["target"].fetchval("SELECT display_name FROM cortex_core.scopes WHERE scope_id=$1", PROJECTS[0]) == "Unrelated fixture"
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_registry") == 0
            assert len(await ledger(pair)) == 0
    asyncio.run(run())


@pytest.mark.parametrize("changed", ["source", "policy"])
def test_changed_actual_input_refuses_bound_run(state_cluster, changed):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            await engine.import_projects(pair["target"], snapshot, binding, policy)
            before = await ledger(pair)
            if changed == "source":
                await pair["writer"].execute("UPDATE public.cortex_projects SET display_name='New source fixture' WHERE id=$1", PROJECTS[0])
                _, projects = modules()
                snapshot = await projects.read_project_snapshot(pair["source"], expected_database=pair["source_name"])
            else:
                policy = {**policy, "approved_revision": 2}
            with pytest.raises(engine.ImportRefused):
                await engine.import_projects(pair["target"], snapshot, binding, policy)
            assert await ledger(pair) == before
    asyncio.run(run())


def test_wrong_target_role_cannot_import(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            app = await pair["connect"](pair["target_name"], "cortex_v2_app")
            try:
                with pytest.raises(engine.ImportRefused):
                    await engine.import_projects(app, snapshot, binding, policy)
            finally:
                await app.close()
            assert len(await ledger(pair)) == 0
    asyncio.run(run())


def test_blob_chunks_exact_hash_and_incomplete_refusal(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            await engine.import_projects(pair["target"], snapshot, binding, policy)
            original = b"\x00Original fixture\xff\xce\xbb"
            digest = hashlib.sha256(original).digest()
            chunks = (original[:7], original[7:])
            sql = "INSERT INTO cortex_core.import_blob_chunks(run_id,blob_sha256,chunk_index,chunk_bytes,chunk_sha256,total_bytes,chunk_count) VALUES($1,$2,$3,$4,$5,$6,2)"
            await pair["target"].execute(sql, binding.run_id, digest, 0, chunks[0], hashlib.sha256(chunks[0]).digest(), len(original))
            with pytest.raises(engine.ImportRefused):
                await engine.read_blob(pair["target"], binding.run_id, digest)
            with pytest.raises(Exception):
                await pair["target"].execute(sql, binding.run_id, digest, 1, chunks[1], b"x" * 32, len(original))
            await pair["target"].execute(sql, binding.run_id, digest, 1, chunks[1], hashlib.sha256(chunks[1]).digest(), len(original))
            assert await engine.read_blob(pair["target"], binding.run_id, digest) == original
    asyncio.run(run())


def test_storage_is_present_forced_rls_and_migrator_only(state_cluster):
    async def run():
        async with state_cluster() as pair:
            for table in ("import_runs", "import_rows", "import_blob_chunks"):
                row = await pair["target"].fetchrow("SELECT c.relrowsecurity,c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='cortex_core' AND c.relname=$1", table)
                assert row is not None, f"Missing durable storage: {table}"
                assert row["relrowsecurity"] and row["relforcerowsecurity"]
                assert await pair["target"].fetchval("SELECT has_table_privilege('cortex_v2_app',$1,'INSERT')", "cortex_core." + table) is False
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_committed_import_ledger_is_append_only(state_cluster, mutation):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            await engine.import_projects(pair["target"], snapshot, binding, policy)
            for table in ("import_runs", "import_rows", "import_blob_chunks"):
                sql = (f"UPDATE cortex_core.{table} SET " + ("event_seq=event_seq" if table == "import_runs" else "ordinal=ordinal" if table == "import_rows" else "chunk_index=chunk_index")) if mutation == "UPDATE" else f"{mutation} FROM cortex_core.{table}" if mutation == "DELETE" else f"TRUNCATE cortex_core.{table}"
                with pytest.raises(Exception):
                    await pair["target"].execute(sql)
    asyncio.run(run())
