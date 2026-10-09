"""Synthetic behavior checks, never a production recall/performance receipt."""

import math
import os
import unittest
from pathlib import Path
from uuid import UUID

import asyncpg
from cortex_core.embeddings.pg_search import (
    CapabilityUnavailable,
    CoreUnavailable,
    EmbeddingIdentity,
    PostgresSearch,
    Scope,
    StaleEmbedding,
)

TENANT_A = UUID("00000000-0000-0000-0000-000000000001")
TENANT_B = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_A = UUID("00000000-0000-0000-0000-000000000011")
PROJECT_B = UUID("00000000-0000-0000-0000-000000000012")
PROJECT_C = UUID("00000000-0000-0000-0000-000000000013")
IDENTITY = EmbeddingIdentity("openrouter", "current-model", "v1", 768, "preprocess-v1")
VECTOR = [1.0] + [0.0] * 767


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin = await asyncpg.connect(os.environ["SEARCH_TEST_DSN"])
        await self.admin.execute(
            "DROP SCHEMA IF EXISTS retrieval CASCADE; DROP TABLE IF EXISTS public.test_grants"
        )
        await self.admin.execute(
            Path(__file__)
            .resolve()
            .parents[2]
            .joinpath("schema/retrieval/001-pg-search.sql")
            .read_text()
        )
        if not await self.admin.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname='search_runtime'"
        ):
            await self.admin.execute(
                "CREATE ROLE search_runtime LOGIN NOSUPERUSER NOBYPASSRLS"
            )
        await self.admin.execute(
            "GRANT USAGE ON SCHEMA retrieval TO search_runtime; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA retrieval TO search_runtime"
        )
        await self.admin.execute(
            "CREATE TABLE public.test_grants(subject text primary key, tenant_id uuid, project_id uuid); GRANT SELECT ON public.test_grants TO search_runtime"
        )
        await self.admin.executemany(
            "INSERT INTO public.test_grants VALUES($1,$2,$3)",
            [
                ("alice", TENANT_A, PROJECT_A),
                ("bob", TENANT_B, PROJECT_B),
                ("alice-other", TENANT_A, PROJECT_C),
            ],
        )
        self.pool = await asyncpg.create_pool(
            os.environ["SEARCH_TEST_DSN"].replace("postgres@", "search_runtime@"),
            min_size=1,
            max_size=3,
        )

        async def authorize(conn, subject):
            row = await conn.fetchrow(
                "SELECT tenant_id, project_id FROM public.test_grants WHERE subject=$1",
                subject,
            )
            if row is None:
                raise PermissionError("revoked")
            return Scope(row["tenant_id"], row["project_id"])

        self.store = PostgresSearch(self.pool, authorize)
        for subject in ["alice", "bob", "alice-other"]:
            await self.store.configure(subject, IDENTITY, "ready")
        if not getattr(self.__class__, "printed_version", False):
            print(
                "PG",
                await self.admin.fetchval("SHOW server_version"),
                "pgvector",
                await self.admin.fetchval(
                    "SELECT extversion FROM pg_extension WHERE extname='vector'"
                ),
            )
            self.__class__.printed_version = True

    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.close()

    async def record(self, subject="alice", rid="record-a", revision=1, vector=VECTOR):
        await self.store.note_revision(subject, rid, "memory", revision)
        await self.store.store_embedding(subject, rid, revision, IDENTITY, vector)

    async def test_tenant_and_project_filters(self):
        await self.record()
        await self.record("bob", "secret-b")
        await self.record("alice-other", "secret-c")
        result = await self.store.search("alice", IDENTITY, VECTOR, limit=10)
        self.assertEqual([x.record_id for x in result.hits], ["record-a"])
        self.assertEqual(result.freshness.pending_records, 0)
        self.assertEqual(result.freshness.status, "current")

    async def test_forced_rls_without_scope_and_wrong_tenant_write(self):
        await self.record()
        async with self.pool.acquire() as conn:
            self.assertEqual(
                await conn.fetchval("SELECT count(*) FROM retrieval.search_vectors"), 0
            )
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('cortex.tenant_id',$1,true), set_config('cortex.project_id',$2,true)",
                    str(TENANT_A),
                    str(PROJECT_A),
                )
                with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(
                        "INSERT INTO retrieval.search_sources VALUES($1,$2,'bad','memory',1)",
                        TENANT_B,
                        PROJECT_B,
                    )
        flags = await self.admin.fetch(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='retrieval' AND relkind='r'"
        )
        self.assertTrue(flags and all(r[0] and r[1] for r in flags))

    async def test_current_authorization_revocation(self):
        await self.record()
        await self.store.search("alice", IDENTITY, VECTOR)
        await self.admin.execute("DELETE FROM public.test_grants WHERE subject='alice'")
        with self.assertRaises(PermissionError):
            await self.store.search("alice", IDENTITY, VECTOR)

    async def test_unavailable_is_distinct_from_empty(self):
        result = await self.store.search("alice", IDENTITY, VECTOR)
        self.assertEqual(result.hits, ())
        for state in ["disabled", "rebuilding"]:
            await self.store.configure("alice", IDENTITY, state)
            with self.assertRaises(CapabilityUnavailable) as caught:
                await self.store.search("alice", IDENTITY, VECTOR)
            self.assertEqual(caught.exception.code, "capability_unavailable")
            self.assertEqual(caught.exception.reason, state)
        with self.assertRaises(CapabilityUnavailable):
            await self.store.search(
                "alice",
                EmbeddingIdentity("openrouter", "wrong", "v1", 768, "preprocess-v1"),
                VECTOR,
            )

    async def test_freshness_and_stale_response_rejection(self):
        await self.record()
        await self.store.note_revision("alice", "record-a", "memory", 2)
        result = await self.store.search("alice", IDENTITY, VECTOR)
        self.assertEqual(result.hits, ())
        self.assertEqual(result.freshness.pending_records, 1)
        self.assertEqual(result.freshness.status, "lagging")
        with self.assertRaises(StaleEmbedding):
            await self.store.store_embedding("alice", "record-a", 1, IDENTITY, VECTOR)
        await self.store.store_embedding("alice", "record-a", 2, IDENTITY, VECTOR)
        self.assertEqual(
            (
                await self.store.search("alice", IDENTITY, VECTOR)
            ).freshness.pending_records,
            0,
        )
        await self.store.note_revision("alice", "record-a", "memory", 1)
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT source_revision FROM retrieval.search_sources WHERE record_id='record-a'"
            ),
            2,
        )

    async def test_generation_change_excludes_old_vectors(self):
        await self.record()
        changed = EmbeddingIdentity(
            "openrouter", "new-model", "v2", 768, "preprocess-v2"
        )
        await self.store.configure("alice", changed, "ready")
        self.assertEqual(
            (
                await self.store.search("alice", changed, VECTOR)
            ).freshness.pending_records,
            1,
        )
        with self.assertRaises(CapabilityUnavailable):
            await self.store.store_embedding("alice", "record-a", 1, IDENTITY, VECTOR)
        with self.assertRaises(CapabilityUnavailable):
            await self.store.search("alice", IDENTITY, VECTOR)
        await self.store.store_embedding("alice", "record-a", 1, changed, VECTOR)
        self.assertEqual(
            len((await self.store.search("alice", changed, VECTOR)).hits), 1
        )

    async def test_hnsw_plan_and_distance(self):
        await self.record()
        # A one-row relation legitimately chooses a cheaper scoped B-tree.
        # Exercise index eligibility on a corpus while preserving the assertion.
        await self.admin.execute(
            """INSERT INTO retrieval.search_sources
            SELECT $1,$2,'plan-'||i,'memory',1 FROM generate_series(1,2000) i""",
            TENANT_A,
            PROJECT_A,
        )
        await self.admin.execute(
            """INSERT INTO retrieval.search_vectors
            SELECT $1,$2,'plan-'||i,1,$3,$4::vector FROM generate_series(1,2000) i""",
            TENANT_A,
            PROJECT_A,
            IDENTITY.key,
            str(VECTOR),
        )
        await self.admin.execute("ANALYZE retrieval.search_vectors")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('cortex.tenant_id',$1,true),set_config('cortex.project_id',$2,true)",
                    str(TENANT_A),
                    str(PROJECT_A),
                )
                await conn.execute("SET LOCAL enable_seqscan=off")
                plan = await conn.fetchval(
                    "EXPLAIN (FORMAT JSON) SELECT record_id FROM retrieval.search_vectors ORDER BY embedding <=> $1::vector LIMIT 10",
                    str(VECTOR),
                )
                self.assertIn("search_vectors_hnsw", plan)
        result = await self.store.search("alice", IDENTITY, VECTOR)
        self.assertAlmostEqual(result.hits[0].distance, 0.0)

    async def test_input_bounds_zero_and_nonfinite_vectors(self):
        for v in [[], [0.0] * 768, [math.nan] + [0.0] * 767, [math.inf] + [0.0] * 767]:
            with self.assertRaises(ValueError):
                await self.store.search("alice", IDENTITY, v)
        for limit in [0, 101]:
            with self.assertRaises(ValueError):
                await self.store.search("alice", IDENTITY, VECTOR, limit=limit)
        with self.assertRaises(ValueError):
            await self.store.configure(
                "alice", EmbeddingIdentity("openrouter", "x", "v1", 3, "p"), "ready"
            )

    async def test_core_outage_never_empty(self):
        await self.pool.close()
        with self.assertRaises(CoreUnavailable) as caught:
            await self.store.search("alice", IDENTITY, VECTOR)
        self.assertEqual(caught.exception.code, "core_unavailable")

    async def test_superuser_runtime_is_rejected(self):
        pool = await asyncpg.create_pool(
            os.environ["SEARCH_TEST_DSN"], min_size=1, max_size=1
        )
        try:
            unsafe = PostgresSearch(pool, self.store.authorize)
            with self.assertRaises(PermissionError):
                await unsafe.search("alice", IDENTITY, VECTOR)
        finally:
            await pool.close()
