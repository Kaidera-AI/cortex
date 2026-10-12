"""Owned actual-PG numeric witnesses; synthetic authority, no native API credit."""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import unittest

import httpx

from cortex_core.embeddings.query_cache import CacheScope, QueryEmbeddingCache
from cortex_core.modules.providers.hosted import HostedProvider
import test_pg_search as existing
from test_query_cache_float32_roundtrip import FRACTIONAL, independent_float32


class ObservedConnection:
    """Observe actual driver results/parameters without changing their semantics."""

    def __init__(self, connection, owner):
        self.connection, self.owner = connection, owner

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def fetchrow(self, text, *args):
        row = await self.connection.fetchrow(text, *args)
        if "FROM retrieval.query_embeddings" in text and row is not None:
            self.owner.lookup_texts.append(row["vector"])
        return row

    async def fetch(self, text, *args):
        rows = await self.connection.fetch(text, *args)
        if "FROM retrieval.search_vectors v" in text:
            self.owner.search_inputs.append(tuple(json.loads(args[3])))
        return rows


class TestNativeCacheFloat32RoundTrip(unittest.IsolatedAsyncioTestCase):
    """Borrow fixture setup only; do not inherit or recount its test bodies."""

    async def asyncSetUp(self):
        if os.environ.get("NEMO_CACHE_ROUNDTRIP_OWNED_FIXTURE") != "1":
            raise RuntimeError("explicit owned disposable-PG fixture required")
        await existing.SearchTests.asyncSetUp(self)
        self.addAsyncCleanup(existing.SearchTests.asyncTearDown, self)
        await self.admin.execute(
            "ALTER TABLE public.test_grants ADD COLUMN permission_generation text "
            "NOT NULL DEFAULT 'g1'"
        )
        await self.admin.execute(
            Path(__file__).resolve().parents[2]
            .joinpath("schema/retrieval/002-query-cache.sql").read_text()
        )
        await self.admin.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON retrieval.query_embeddings "
            "TO search_runtime; GRANT USAGE ON ALL SEQUENCES IN SCHEMA "
            "retrieval TO search_runtime"
        )
        self.calls = 0
        self.publication_inputs, self.lookup_texts, self.search_inputs = [], [], []

        async def authorize(conn, subject):
            row = await conn.fetchrow(
                "SELECT tenant_id,project_id,permission_generation "
                "FROM public.test_grants WHERE subject=$1", subject
            )
            if row is None:
                raise PermissionError("revoked")
            return CacheScope(row["tenant_id"], row["project_id"],
                              row["permission_generation"])

        async def key(provider):
            return "public-synthetic-test-key"

        async def response(request):
            self.calls += 1
            return httpx.Response(200, json={"data": [{"embedding": FRACTIONAL}]})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        self.addAsyncCleanup(self.client.aclose)
        provider = HostedProvider(self.client, key)
        self.cache = QueryEmbeddingCache(self.pool, authorize, provider.embed)
        original_publish = self.cache._publish

        async def publish(subject, cache_key, owner, fence, vector):
            self.publication_inputs.append(vector)
            return await original_publish(subject, cache_key, owner, fence, vector)

        self.cache._publish = publish
        for port in (self.cache.db, self.store):
            original_request = port._request

            def observe(original):
                @asynccontextmanager
                async def request(*args, **kwargs):
                    async with original(*args, **kwargs) as (conn, scope):
                        yield ObservedConnection(conn, self), scope
                return request

            port._request = observe(original_request)
        await self.store.note_revision("alice", "fractional-record", "memory", 1)
        await self.store.store_embedding("alice", "fractional-record", 1,
                                        existing.IDENTITY, FRACTIONAL)

    async def observe_result(self, result):
        stored_text = await self.admin.fetchval(
            "SELECT embedding::text FROM retrieval.query_embeddings "
            "WHERE tenant_id=$1 AND project_id=$2",
            existing.TENANT_A, existing.PROJECT_A
        )
        hits = await self.store.search("alice", existing.IDENTITY, result.vector)
        observations = {
            "verification_kind": "REAL_PG_SYNTHETIC_AUTHORITY",
            "returned_vector": result.vector,
            "cache_hit": result.cache_hit,
            "publication_inputs": self.publication_inputs,
            "publication_types": [type(v).__name__ for v in self.publication_inputs],
            "actual_pg_stored_text": stored_text,
            "actual_cache_lookup_texts": self.lookup_texts,
            "actual_search_sql_vector_inputs": self.search_inputs,
            "actual_search_hit_ids": [h.record_id for h in hits.hits],
            "provider_mock_calls": self.calls,
        }
        directory = Path(os.environ["NEMO_CACHE_ROUNDTRIP_OBSERVATIONS"])
        (directory / (self._testMethodName + ".json")).write_text(
            json.dumps(observations, indent=2) + "\n"
        )
        return stored_text, hits

    async def test_native_cold_return_matches_publication_and_search_float32(self):
        result = await self.cache.get("alice", existing.IDENTITY, "cold-fractional")
        stored_text, hits = await self.observe_result(result)
        expected = independent_float32(FRACTIONAL)
        self.assertEqual(result.vector, expected,
                         "cold return differs from actual PG float32 storage")
        self.assertFalse(result.cache_hit)
        self.assertEqual(self.calls, 1)
        self.assertEqual(independent_float32(json.loads(stored_text)), expected)
        self.assertEqual(self.publication_inputs, [expected])
        self.assertTrue(all(isinstance(v, tuple) for v in self.publication_inputs))
        self.assertEqual(self.search_inputs, [expected])
        self.assertEqual([h.record_id for h in hits.hits], ["fractional-record"])

    async def test_native_warm_return_matches_publication_and_search_float32(self):
        await self.cache.get("alice", existing.IDENTITY, "warm-fractional")
        before = self.calls
        result = await self.cache.get("alice", existing.IDENTITY, "warm-fractional")
        stored_text, hits = await self.observe_result(result)
        expected = independent_float32(FRACTIONAL)
        self.assertEqual(result.vector, expected,
                         "warm return differs from actual PG float32 storage")
        self.assertTrue(result.cache_hit)
        self.assertEqual(self.calls - before, 0)
        self.assertEqual(independent_float32(json.loads(stored_text)), expected)
        self.assertEqual(self.lookup_texts, [stored_text])
        self.assertEqual(self.publication_inputs, [expected])
        self.assertTrue(all(isinstance(v, tuple) for v in self.publication_inputs))
        self.assertEqual(self.search_inputs, [expected])
        self.assertEqual([h.record_id for h in hits.hits], ["fractional-record"])


if __name__ == "__main__":
    unittest.main()
