"""Mike PR29 regression probes retained, plus transaction retry contracts."""

import asyncio
import math
import unittest

import asyncpg
import test_pg_search as legacy
from cortex_core.embeddings.pg_search import (
    CapabilityUnavailable,
    EmbeddingIdentity,
    PostgresSearch,
    StaleEmbedding,
)

IDENTITY, VECTOR = legacy.IDENTITY, legacy.VECTOR


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = legacy.SearchTests.asyncSetUp
    asyncTearDown = legacy.SearchTests.asyncTearDown
    record = legacy.SearchTests.record

    async def test_float32_underflow_must_not_become_current_embedding(self):
        await self.store.note_revision("alice", "tiny", "memory", 1)
        try:
            await self.store.store_embedding(
                "alice", "tiny", 1, IDENTITY, [1e-100] + [0.0] * 767
            )
        except ValueError:
            return
        v = await self.admin.fetchval(
            "SELECT embedding::real[] FROM retrieval.search_vectors WHERE record_id='tiny'"
        )
        r = await self.store.search("alice", IDENTITY, VECTOR)
        print(
            "UNDERFLOW",
            "stored_all_zero",
            not any(v),
            "freshness",
            r.freshness.status,
            "distances",
            [h.distance for h in r.hits],
            flush=True,
        )
        self.fail(
            "finite Python input underflowed to zero in stored float32 without validation refusal"
        )

    async def test_float32_overflow_and_cosine_norm_refused(self):
        await self.store.note_revision("alice", "invalid", "memory", 1)
        for value in [1e100, 1e20, 1e-30]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    await self.store.store_embedding(
                        "alice", "invalid", 1, IDENTITY, [value] + [0.0] * 767
                    )
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT count(*) FROM retrieval.search_vectors WHERE record_id='invalid'"
            ),
            0,
        )

    async def test_valid_float32_distance_stays_finite(self):
        vector = [0.1, 0.2] + [0.0] * 766
        await self.record(vector=vector)
        result = await self.store.search("alice", IDENTITY, vector)
        self.assertTrue(math.isfinite(result.hits[0].distance))
        self.assertAlmostEqual(result.hits[0].distance, 0, places=5)

    async def race(self, operation, change, expected):
        checked, resume = asyncio.Event(), asyncio.Event()
        original = self.store.authorize
        calls = 0

        async def paused(conn, subject):
            nonlocal calls
            calls += 1
            scope = await original(conn, subject)
            if calls == 1:
                checked.set()
                await resume.wait()
            return scope

        reader = PostgresSearch(self.pool, paused)
        pending = asyncio.create_task(operation(reader))
        try:
            await asyncio.wait_for(checked.wait(), 2)
            await change()
        finally:
            resume.set()
        with self.assertRaises(expected):
            await asyncio.wait_for(pending, 3)
        self.assertGreaterEqual(calls, 2)

    async def test_generation_race_rechecks_and_returns_typed_refusal(self):
        changed = EmbeddingIdentity("openrouter", "changed", "v2", 768, "p2")
        await self.race(
            lambda reader: reader.search("alice", IDENTITY, VECTOR),
            lambda: self.store.configure("alice", changed, "ready"),
            CapabilityUnavailable,
        )

    async def test_source_revision_race_rechecks_and_refuses_stale_embedding(self):
        await self.store.note_revision("alice", "race", "memory", 1)
        await self.race(
            lambda reader: reader.store_embedding("alice", "race", 1, IDENTITY, VECTOR),
            lambda: self.store.note_revision("alice", "race", "memory", 2),
            StaleEmbedding,
        )

    async def test_revoked_grant_is_rechecked_on_retry(self):
        changed = EmbeddingIdentity("openrouter", "changed", "v2", 768, "p2")

        async def change():
            await self.store.configure("alice", changed, "ready")
            await self.admin.execute(
                "DELETE FROM public.test_grants WHERE subject='alice'"
            )

        await self.race(
            lambda reader: reader.search("alice", IDENTITY, VECTOR),
            change,
            PermissionError,
        )

    async def test_retry_exhaustion_is_bounded_and_typed(self):
        calls = 0

        async def always_conflict(conn, subject):
            nonlocal calls
            calls += 1
            raise asyncpg.SerializationError("synthetic 40001")

        reader = PostgresSearch(self.pool, always_conflict)
        with self.assertRaises(CapabilityUnavailable) as caught:
            await reader.search("alice", IDENTITY, VECTOR)
        self.assertEqual(caught.exception.reason, "concurrent_update")
        self.assertEqual(calls, 3)

    async def test_each_public_transaction_retries_from_authorization(self):
        operations = [
            lambda s: s.configure("alice", IDENTITY, "ready"),
            lambda s: s.note_revision("alice", "transient", "memory", 1),
            lambda s: s.store_embedding("alice", "transient", 1, IDENTITY, VECTOR),
            lambda s: s.search("alice", IDENTITY, VECTOR),
        ]
        for operation in operations:
            calls = 0

            async def once(conn, subject):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise asyncpg.SerializationError("synthetic transient")
                return await self.store.authorize(conn, subject)

            await operation(PostgresSearch(self.pool, once))
            self.assertEqual(calls, 2)

    async def test_revision_notice_advances_monotonically(self):
        await self.store.note_revision("alice", "revision", "memory", 1)
        await self.store.note_revision("alice", "revision", "memory", 2)
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT source_revision FROM retrieval.search_sources WHERE record_id='revision'"
            ),
            2,
        )
