"""UNIT_PORTS float32 return witnesses; no PG, authority or performance credit."""

from contextlib import asynccontextmanager
import json
import struct
import unittest
from uuid import UUID

import httpx

from cortex_core.embeddings.pg_search import EmbeddingIdentity, vector_literal
from cortex_core.embeddings.query_cache import CacheScope, QueryEmbeddingCache
from cortex_core.modules.providers.hosted import HostedProvider


IDENTITY = EmbeddingIdentity("openrouter", "fixture-model", "v1", 768, "exact")
FRACTIONAL = [0.123456789, 1.0] + [0.0] * 766
SCOPE = CacheScope(UUID(int=1), UUID(int=11), "fixture-generation")


def independent_float32(values):
    return tuple(struct.unpack("!f", struct.pack("!f", value))[0] for value in values)


class WarmConnection:
    """Returns an explicit vector-text boundary value; does not simulate SQL."""

    def __init__(self, values):
        self.text = json.dumps(values)

    async def execute(self, *args):
        return None

    async def fetchrow(self, *args):
        return {"vector": self.text, "fresh": True}


class CacheFloat32RoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def cold(self, values):
        published = []

        async def key(provider):
            self.assertEqual(provider, "openrouter")
            return "public-synthetic-test-key"

        async def response(request):
            return httpx.Response(200, json={"data": [{"embedding": values}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            provider = HostedProvider(client, key)
            cache = QueryEmbeddingCache(None, lambda *args: None, provider.embed)

            async def claim(*args):
                return (SCOPE.tenant_id, SCOPE.project_id, SCOPE.permission_generation,
                        IDENTITY.key, "fixture-query-digest"), None, 1

            async def publish(subject, key, owner, fence, vector):
                # Capture the actual existing writer's canonical representation.
                published.append(tuple(json.loads(vector_literal(vector, 768))))
                return True

            cache._lookup_claim = claim
            cache._publish = publish
            result = await cache.get("fixture-subject", IDENTITY, "fractional query")
        return result, published

    async def test_cold_provider_vector_is_returned_as_canonical_float32(self):
        result, published = await self.cold(FRACTIONAL)
        expected = independent_float32(FRACTIONAL)
        self.assertFalse(result.cache_hit)
        self.assertEqual(published, [expected])
        self.assertEqual(result.vector, expected,
                         "cold return differs from its canonical PG publication")

    async def test_warm_vector_text_is_returned_as_canonical_float32(self):
        # PG vector text may use a short decimal spelling; the cache must return
        # the exact canonical component, independently of that spelling.
        values = [0.12345679, 1.0] + [0.0] * 766
        connection = WarmConnection(values)

        async def forbidden_provider(*args):
            self.fail("warm result must not call the provider")

        cache = QueryEmbeddingCache(None, lambda *args: None, forbidden_provider)

        @asynccontextmanager
        async def request(*args, **kwargs):
            yield connection, SCOPE

        cache.db._request = request
        result = await cache.get("fixture-subject", IDENTITY, "fractional query")
        self.assertTrue(result.cache_hit)
        self.assertEqual(result.vector, independent_float32(values),
                         "warm decoding preserves decimal doubles instead of float32")

    async def test_exactly_representable_positive_retains_existing_values(self):
        values = [1.0] + [0.0] * 767
        result, published = await self.cold(values)
        self.assertEqual(result.vector, tuple(values))
        self.assertEqual(published, [tuple(values)])
        self.assertFalse(result.cache_hit)


if __name__ == "__main__":
    unittest.main()
