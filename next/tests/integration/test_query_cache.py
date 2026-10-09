"""Synthetic provider transport and real disposable-PG cache tests."""

import asyncio
import unittest

import httpx
import test_pg_search
from cortex_core.embeddings.pg_search import CapabilityUnavailable
from cortex_core.embeddings.query_cache import QueryEmbeddingCache, CacheScope
from cortex_core.modules.providers.hosted import HostedProvider

IDENTITY = test_pg_search.IDENTITY
VECTOR = test_pg_search.VECTOR


class CacheTests(test_pg_search.SearchTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.admin.execute(
            "ALTER TABLE public.test_grants ADD COLUMN permission_generation text NOT NULL DEFAULT 'g1'"
        )
        await self.admin.execute(
            test_pg_search.Path(__file__)
            .resolve()
            .parents[2]
            .joinpath("schema/retrieval/002-query-cache.sql")
            .read_text()
        )
        await self.admin.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON retrieval.query_embeddings TO search_runtime; GRANT USAGE ON ALL SEQUENCES IN SCHEMA retrieval TO search_runtime"
        )
        self.calls = 0

        async def authorize(conn, subject):
            row = await conn.fetchrow(
                "SELECT tenant_id,project_id,permission_generation FROM public.test_grants WHERE subject=$1",
                subject,
            )
            if row is None:
                raise PermissionError("revoked")
            return CacheScope(
                row["tenant_id"], row["project_id"], row["permission_generation"]
            )

        async def embed(text, identity):
            self.calls += 1
            await asyncio.sleep(0.03)
            return VECTOR.copy()

        self.authorize_cache = authorize
        self.embed = embed
        self.cache = QueryEmbeddingCache(self.pool, authorize, embed)

    async def test_cache_warm_reuse_and_process_restart(self):
        first = await self.cache.get("alice", IDENTITY, "same query")
        self.assertFalse(first.cache_hit)
        second_instance = QueryEmbeddingCache(
            self.pool, self.authorize_cache, self.embed
        )
        second = await second_instance.get("alice", IDENTITY, "same query")
        self.assertTrue(second.cache_hit)
        self.assertEqual(self.calls, 1)
        self.assertEqual(first.vector, second.vector)
        columns = await self.admin.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='retrieval' AND table_name='query_embeddings'"
        )
        self.assertNotIn("query", [r[0] for r in columns])

    async def test_cache_key_tenant_project_permission_identity(self):
        for subject in ["alice", "bob", "alice-other"]:
            await self.cache.get(subject, IDENTITY, "same query")
        await self.admin.execute(
            "UPDATE public.test_grants SET permission_generation='g2' WHERE subject='alice'"
        )
        await self.cache.get("alice", IDENTITY, "same query")
        changed = test_pg_search.EmbeddingIdentity("openrouter", "new", "v2", 768, "p2")
        await self.cache.get("alice", changed, "same query")
        await self.cache.get("alice", changed, "same query ")
        self.assertEqual(self.calls, 6)

    async def test_singleflight_across_instances(self):
        other = QueryEmbeddingCache(self.pool, self.authorize_cache, self.embed)
        results = await asyncio.gather(
            self.cache.get("alice", IDENTITY, "concurrent"),
            other.get("alice", IDENTITY, "concurrent"),
        )
        self.assertEqual(self.calls, 1)
        self.assertEqual([r.cache_hit for r in results].count(False), 1)

    async def test_ttl_and_failure_not_cached(self):
        cache = QueryEmbeddingCache(
            self.pool, self.authorize_cache, self.embed, ttl_seconds=0.02
        )
        await cache.get("alice", IDENTITY, "ttl")
        await asyncio.sleep(0.04)
        await cache.get("alice", IDENTITY, "ttl")
        self.assertEqual(self.calls, 2)

        async def failed(text, identity):
            raise CapabilityUnavailable("provider_unavailable")

        cache = QueryEmbeddingCache(self.pool, self.authorize_cache, failed)
        with self.assertRaises(CapabilityUnavailable):
            await cache.get("alice", IDENTITY, "failure")
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT count(*) FROM retrieval.query_embeddings WHERE embedding IS NULL"
            ),
            0,
        )

    async def test_cache_hit_rechecks_revocation(self):
        await self.cache.get("alice", IDENTITY, "revoked")
        await self.admin.execute("DELETE FROM public.test_grants WHERE subject='alice'")
        with self.assertRaises(PermissionError):
            await self.cache.get("alice", IDENTITY, "revoked")

    async def test_revoked_midflight_cannot_publish(self):
        async def revoke(text, identity):
            await self.admin.execute(
                "DELETE FROM public.test_grants WHERE subject='alice'"
            )
            return VECTOR

        cache = QueryEmbeddingCache(self.pool, self.authorize_cache, revoke)
        with self.assertRaises(PermissionError):
            await cache.get("alice", IDENTITY, "midflight")
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT count(*) FROM retrieval.query_embeddings WHERE embedding IS NOT NULL"
            ),
            0,
        )

    async def test_expired_lease_cannot_overwrite_successor(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow(text, identity):
            entered.set()
            await release.wait()
            return VECTOR

        old = QueryEmbeddingCache(
            self.pool, self.authorize_cache, slow, lease_seconds=0.03, wait_seconds=0.5
        )
        task = asyncio.create_task(old.get("alice", IDENTITY, "lease"))
        await entered.wait()
        await asyncio.sleep(0.05)
        newer = await self.cache.get("alice", IDENTITY, "lease")
        release.set()
        result = await task
        self.assertEqual(result.vector, newer.vector)
        self.assertTrue(result.cache_hit)
        self.assertEqual(self.calls, 1)

    async def test_cache_rls_without_scope(self):
        await self.cache.get("alice", IDENTITY, "private")
        async with self.pool.acquire() as conn:
            self.assertEqual(
                await conn.fetchval("SELECT count(*) FROM retrieval.query_embeddings"),
                0,
            )
        flags = await self.admin.fetchrow(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='retrieval.query_embeddings'::regclass"
        )
        self.assertTrue(flags[0] and flags[1])


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_hosted_endpoint_and_snapshot(self):
        requests = []

        async def responder(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [{"embedding": VECTOR}]})

        async def key(provider):
            self.assertEqual(provider, "openrouter")
            return "synthetic-test-key"

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(responder)
        ) as client:
            provider = HostedProvider(client, key)
            vector = await provider.embed("query", IDENTITY)
        self.assertEqual(vector, VECTOR)
        self.assertEqual(
            str(requests[0].url), "https://openrouter.ai/api/v1/embeddings"
        )
        import json

        self.assertEqual(
            json.loads(requests[0].content),
            {"model": IDENTITY.model, "input": "query", "dimensions": 768},
        )

    async def test_provider_refuses_missing_key_wrong_provider_and_invalid_output(self):
        async def no_key(provider):
            return None

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"data": [{"embedding": [0.0] * 768}]}
                )
            )
        ) as client:
            provider = HostedProvider(client, no_key)
            with self.assertRaises(CapabilityUnavailable):
                await provider.embed("query", IDENTITY)

            async def key(provider):
                return "synthetic-test-key"

            provider = HostedProvider(client, key)
            with self.assertRaises(CapabilityUnavailable):
                await provider.embed("query", IDENTITY)
            changed = test_pg_search.EmbeddingIdentity(
                "new-provider", "x", "v1", 768, "p"
            )
            with self.assertRaises(CapabilityUnavailable):
                await provider.embed("query", changed)
