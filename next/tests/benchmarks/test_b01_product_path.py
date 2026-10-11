"""Frozen B01 adapter controls; unit ports are not real-PG qualification."""
import asyncio
import copy
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from uuid import UUID

import httpx
import numpy as np
from cortex_core.embeddings.pg_search import EmbeddingIdentity, Freshness, Hit, SearchResult
from cortex_core.embeddings.query_cache import CachedEmbedding
from cortex_core.modules.providers.hosted import HostedProvider
from vector_baseline import corpus, load, oracle

NEXT = Path(__file__).resolve().parents[2]
IDENTITY = EmbeddingIdentity("openrouter", "fixture-model", "v1", 768, "preprocess-v1")
TA, PA = UUID(int=2), UUID(int=3)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class MissingSurface:
    """Explicit inert preimplementation seam: behavioral assertions fail."""
    async def evaluate(self, *args, **kwargs):
        return {"status": "NOT_RUN", "reason": "implementation_absent"}

    def client(self, *args, **kwargs):
        class Inert:
            async def open(self): pass
            async def request(self, query): return {"status": "NOT_RUN"}
            async def aclose(self): pass
        return Inert()

    def receipt(self, *args, **kwargs):
        return {"status": "NOT_RUN", "gtm_qualified": False,
                "whole_product_stack_complete": False, "engine_decision": "UNDECIDED",
                "load": {"records": [], "warmup_records": []}}


def surface():
    try:
        return importlib.import_module("vector_baseline.product_path")
    except ModuleNotFoundError as error:
        if error.name != "vector_baseline.product_path": raise
        return MissingSurface()


class UnitCache:
    """A unit port, with the REAL hosted adapter behind its cache miss."""
    def __init__(self, provider):
        self.provider, self.calls, self.values = provider, 0, {}

    async def get(self, subject, identity, text):
        self.calls += 1
        key = (subject, identity.key, text)
        if key in self.values: return CachedEmbedding(self.values[key], True)
        vector = tuple(await self.provider.embed(text, identity))
        self.values[key] = vector
        return CachedEmbedding(vector, False)


class UnitSearch:
    def __init__(self):
        self.calls, self.bad_hit, self.wrong_identity, self.failure = [], None, False, None

    async def search(self, subject, identity, vector, *, limit=20):
        self.calls.append((subject, identity.key, tuple(vector), limit))
        if self.failure: raise self.failure
        hits = (Hit("p-r1", "memory", 1, 0.), Hit("p-r2", "memory", 1, 0.),
                Hit("p-r3", "memory", 1, 1.))[:limit]
        if subject == "empty": hits = ()
        if self.bad_hit: hits = (self.bad_hit,) + hits[1:]
        return SearchResult(hits, Freshness("wrong" if self.wrong_identity else identity.key, len(hits), 0))


class ProductPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="b01-query-input-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "corpus"
        vectors = np.zeros((3, 768), dtype="<f4")
        vectors[0, 0] = vectors[1, 0] = vectors[2, 1] = 1
        self.corpus = corpus.write_corpus(self.root, vectors,
            [{"id": "r1", "tenant": "ta", "project": "pa"},
             {"id": "r2", "tenant": "ta", "project": "pa"},
             {"id": "r3", "tenant": "ta", "project": "pa"}],
            {"provider": IDENTITY.provider, "model": IDENTITY.model, "dimension": 768,
             "metric": "cosine", "generation": "fixture-g1"})
        query = {"id": "q1", "text": "basis-a", "subject": "reader", "cell": "scope/dense",
                 "mode": "dense", "limit": 2, "tenant": "ta", "project": "pa",
                 "vector": vectors[0].tolist(), "sparse": None, "lo": None, "hi": None,
                 "kind": None, "time_lo": None, "time_hi": None,
                 "status": "READY", "generation": "fixture-g1"}
        empty = {**query, "id": "empty", "subject": "empty", "text": "empty-basis", "project": "empty"}
        self.spec = {"schema": "cortex.b01-product-input.v1", "identity": {
            "provider": IDENTITY.provider, "model": IDENTITY.model, "version": IDENTITY.version,
            "dimensions": IDENTITY.dimensions, "preprocessing": IDENTITY.preprocessing},
            "corpus_manifest_sha256": corpus.digest(self.root / "manifest.json"),
            "queries": [query, empty], "splits": {"tuning": [], "held_out": ["q1", "empty"]},
            "scope_map": [{"tenant": "ta", "project": "pa", "tenant_id": str(TA), "project_id": str(PA)},
                          {"tenant": "ta", "project": "empty", "tenant_id": str(TA), "project_id": str(UUID(int=4))}],
            "kind_map": {"0": "memory"},
            "record_map": {"r1": "p-r1", "r2": "p-r2", "r3": "p-r3"},
            "source_revisions": {"r1": 1, "r2": 1, "r3": 1},
            "source_sha256": {"src/cortex_core/embeddings/pg_search.py": corpus.digest(NEXT / "src/cortex_core/embeddings/pg_search.py")}}
        self.expected = hashlib.sha256(canonical(self.spec)).hexdigest()
        self.provider_calls = self.credential_calls = self.auth_calls = 0
        self.provider_vector = vectors[0].tolist()

        def respond(request):
            self.provider_calls += 1
            return httpx.Response(200, json={"data": [{"embedding": self.provider_vector}]})

        async def resolve(provider):
            self.credential_calls += 1
            return "public-synthetic-fixture-token"

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
        self.addAsyncCleanup(self.http.aclose)
        self.provider = HostedProvider(self.http, resolve)
        self.cache, self.search = UnitCache(self.provider), UnitSearch()

    async def authorize(self, subject):
        self.auth_calls += 1
        return SimpleNamespace(tenant_id=TA, project_id=PA if subject != "empty" else UUID(int=4),
                               permission_generation=1)

    async def evaluate(self, qid="q1", *, spec=None, expected=None):
        return await surface().evaluate(self.root, spec if spec is not None else self.spec,
            expected if expected is not None else self.expected, qid, self.cache, self.search,
            self.authorize, source_root=NEXT)

    async def test_actual_calls_cold_warm_and_independent_tiny_truth(self):
        truth = oracle.rank(self.corpus, self.spec["queries"][0], modes=("dense",))
        self.assertEqual(truth.ids("dense", limit=3), ["r1", "r2", "r3"])
        self.assertEqual([truth.score("dense", rid) for rid in ["r1", "r2", "r3"]], [1., 1., 0.])
        cold, warm = await self.evaluate(), await self.evaluate()
        self.assertEqual(cold["status"], "MEASURED")
        self.assertEqual(warm["status"], "MEASURED")
        self.assertFalse(cold["cache_hit"])
        self.assertTrue(warm["cache_hit"])
        self.assertEqual((self.provider_calls, self.credential_calls, self.cache.calls, len(self.search.calls)), (1, 1, 2, 2))
        self.assertEqual(cold["ids"], ["r1", "r2"])
        self.assertEqual(cold["measurement"]["strict_recall"], 1.)
        self.assertEqual(cold["measurement"]["tie_recall"], 1.)
        self.assertGreaterEqual(self.auth_calls, 2)

    async def test_changed_input_mapping_refused_before_any_port(self):
        changed = copy.deepcopy(self.spec)
        changed["scope_map"][0]["project_id"] = str(UUID(int=9))
        row = await self.evaluate(spec=changed)
        self.assertEqual(row["reason"], "input_binding_changed")
        self.assertEqual((self.auth_calls, self.cache.calls, self.provider_calls, len(self.search.calls)), (0, 0, 0, 0))

    async def test_changed_corpus_and_dependency_bytes_refused(self):
        with (self.root / "vectors.npy").open("ab") as stream: stream.write(b"changed")
        row = await self.evaluate()
        self.assertEqual(row["status"], "INPUT_REFUSED")
        self.assertEqual(self.cache.calls, 0)

    async def test_wrong_identity_refused_before_ports(self):
        changed = copy.deepcopy(self.spec)
        changed["identity"]["model"] = "different"
        row = await self.evaluate(spec=changed, expected=hashlib.sha256(canonical(changed)).hexdigest())
        self.assertEqual(row["reason"], "identity_mismatch")
        self.assertEqual((self.auth_calls, self.cache.calls), (0, 0))

    async def test_split_overlap_refused_before_ports(self):
        changed = copy.deepcopy(self.spec); changed["splits"]["tuning"] = ["q1"]
        row = await self.evaluate(spec=changed, expected=hashlib.sha256(canonical(changed)).hexdigest())
        self.assertEqual(row["reason"], "split_overlap")
        self.assertEqual((self.auth_calls, self.cache.calls), (0, 0))

    async def test_current_scope_is_compared_with_frozen_mapping(self):
        async def other(subject):
            return SimpleNamespace(tenant_id=TA, project_id=UUID(int=99), permission_generation=1)
        row = await surface().evaluate(self.root, self.spec, self.expected, "q1", self.cache,
            self.search, other, source_root=NEXT)
        self.assertEqual(row["reason"], "current_scope_mismatch")
        self.assertEqual(self.cache.calls, 0)

    async def test_unsupported_filter_sparse_hybrid_and_metric_have_no_credit(self):
        for field, value in [("kind", 0), ("mode", "sparse"), ("mode", "hybrid")]:
            changed = copy.deepcopy(self.spec); changed["queries"][0][field] = value
            row = await self.evaluate(spec=changed, expected=hashlib.sha256(canonical(changed)).hexdigest())
            self.assertEqual(row["status"], "NOT_RUN")
            self.assertNotIn("measurement", row)
        self.assertEqual((self.auth_calls, self.cache.calls, self.provider_calls), (0, 0, 0))

    async def test_unbound_provider_vector_has_no_recall_credit(self):
        self.provider_vector = [0., 1.] + [0.] * 766
        row = await self.evaluate()
        self.assertEqual(row["reason"], "provider_vector_unbound")
        self.assertNotIn("measurement", row)
        self.assertEqual(len(self.search.calls), 0)

    async def test_unknown_foreign_duplicate_kind_and_revision_hits_refuse(self):
        for hit in [Hit("unknown", "memory", 1, 0.), Hit("p-r1", "different", 1, 0.),
                    Hit("p-r1", "memory", 2, 0.), Hit("p-r2", "memory", 1, 0.)]:
            self.search.bad_hit = hit
            row = await self.evaluate()
            self.assertEqual(row["status"], "RESULT_REFUSED")
            self.assertNotIn("measurement", row)

    async def test_wrong_freshness_identity_has_no_credit(self):
        self.search.wrong_identity = True
        row = await self.evaluate()
        self.assertEqual(row["reason"], "result_identity_mismatch")
        self.assertNotIn("measurement", row)

    async def test_empty_and_less_than_k_are_not_fictional_recall(self):
        empty = await self.evaluate("empty")
        self.assertEqual(empty["status"], "MEASURED")
        self.assertTrue(empty["measurement"]["empty_exact"])
        self.assertIsNone(empty["measurement"]["tie_recall"])
        changed = copy.deepcopy(self.spec); changed["queries"][0]["limit"] = 10
        row = await self.evaluate(spec=changed, expected=hashlib.sha256(canonical(changed)).hexdigest())
        self.assertEqual(row["measurement"]["expected_count"], 3)
        self.assertEqual(row["measurement"]["tie_recall"], 1.)

    async def test_unavailable_results_are_charged_as_load_errors(self):
        from cortex_core.embeddings.pg_search import CoreUnavailable
        self.search.failure = CoreUnavailable("private-dependency-message")
        module = surface()
        run = await load.run([self.spec["queries"][0]], lambda n: module.client(
            self.root, self.spec, self.expected, self.cache, self.search, self.authorize,
            source_root=NEXT), load.RunConfig(duration_seconds=.2, warmup_seconds=0, deadline_seconds=.1))
        self.assertEqual(len(run["records"]), 8)
        self.assertTrue(all(row["status"] == "ERROR" for row in run["records"]))
        self.assertTrue(all(row["latency_seconds"] >= .1 for row in run["records"]))
        self.assertNotIn("private-dependency-message", str(run))

    async def test_fixed_offers_and_receipt_preserve_errors_and_no_gtm_credit(self):
        module = surface()
        run = await load.run([self.spec["queries"][0]], lambda n: module.client(
            self.root, self.spec, self.expected, self.cache, self.search, self.authorize,
            source_root=NEXT), load.RunConfig(duration_seconds=.2, warmup_seconds=.2))
        receipt = module.receipt(run, self.expected, verification_kind="UNIT_PORTS",
                                 cleanup={"complete": True})
        self.assertEqual(len(receipt["load"]["records"]), 8)
        self.assertEqual(len(receipt["load"]["warmup_records"]), 8)
        self.assertEqual(run["clients_closed"], 8)
        self.assertFalse(receipt["gtm_qualified"])
        self.assertFalse(receipt["whole_product_stack_complete"])
        self.assertEqual(receipt["engine_decision"], "UNDECIDED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
