import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from vector_baseline import corpus, oracle, postgres


IDENTITY = {"provider": "synthetic", "model": "b01-fixture-v1", "dimension": 2,
            "metric": "cosine", "generation": "fixture-1"}


def fixture(root):
    rows = []
    for i, sid in enumerate("abcdef"):
        rows.append({"id": sid, "tenant": "t1" if i != 4 else "t2", "project": "p1",
                     "kind": 1, "time": i, "ordinal": i, "deleted": i == 5,
                     "sparse": [[1, 1.0]] if i == 0 else [[2, 1.0]] if i == 1
                     else [[1, 2.0]] if i == 2 else []})
    return corpus.write_corpus(root, [[1, 0], [1, 0], [0, 1], [-1, 0], [1, 0], [1, 0]],
                               rows, IDENTITY)


def query(**changes):
    q = {"id": "q1", "status": "READY", "split": "heldout", "stratum": "scope",
         "mode": "dense", "tenant": "t1", "project": "p1", "lo": None, "hi": None,
         "kind": None, "time_lo": None, "time_hi": None,
         "vector": [1.0, 0.0], "sparse": [[1, 1.0]], "generation": "fixture-1"}
    q.update(changes)
    return q


class CorpusTests(unittest.TestCase):
    def test_default_is_exactly_five_million(self):
        self.assertEqual(inspect.signature(corpus.generate).parameters["count"].default, 5_000_000)

    def test_byte_reproducibility_and_chunk_independence(self):
        with tempfile.TemporaryDirectory() as d:
            a = corpus.generate(Path(d) / "a", count=1000, dimension=8, model="fixture", chunk_size=97)
            b = corpus.generate(Path(d) / "b", count=1000, dimension=8, model="fixture", chunk_size=211)
            self.assertEqual(a.manifest["files"], b.manifest["files"])
            self.assertEqual(a.manifest["distribution"], {"clustered": 700, "diffuse": 200, "ties": 100})
            self.assertEqual(len(set(a.records["id"])), 1000)
            self.assertEqual(int(np.count_nonzero(a.records["tenant"] == b"t00")), 500)
            self.assertTrue(np.isfinite(a.vectors).all())
            self.assertTrue(np.allclose(np.linalg.norm(a.vectors, axis=1), 1, atol=1e-6))

    def test_query_mix_disjoint_and_coverage_honest(self):
        with tempfile.TemporaryDirectory() as d:
            c = corpus.generate(Path(d) / "c", count=100_000, dimension=4, model="fixture")
            tuning = corpus.queries(c, "tuning")
            held = corpus.queries(c, "heldout")
            self.assertEqual(len(tuning), 1200)
            self.assertEqual(len(held), 1200)
            self.assertFalse({q["id"] for q in tuning} & {q["id"] for q in held})
            self.assertNotEqual(tuning[0]["vector"], held[0]["vector"])
            for stratum in corpus.STRATA:
                row = [q for q in held if q["stratum"] == stratum]
                self.assertEqual([sum(q["mode"] == mode for q in row)
                                  for mode in ("dense", "hybrid", "sparse")], [60, 120, 20])
            # Small fixtures cannot silently manufacture very-tight nonedge coverage.
            self.assertTrue(any(q["status"] == "NOT_RUN" for q in held))
            for q in held:
                if q["status"] != "READY":
                    continue
                truth = oracle.rank(c, q, block_size=4096, modes=())
                self.assertEqual(truth.eligible_count, q["eligible_count"])

    def test_validation_rejects_poison_and_hash_drift(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c"
            c = fixture(p)
            with p.joinpath("vectors.npy").open("r+b") as f:
                f.seek(-4, 2)
                f.write(b"xxxx")
            with self.assertRaises(ValueError):
                corpus.load(p)
            for vectors in ([[float("nan"), 0]], [[0, 0]], [[1, 2, 3]]):
                with self.assertRaises(ValueError):
                    corpus.write_corpus(Path(d) / "bad", vectors, [{"id": "x"}], IDENTITY)

    def test_sanitized_slot_is_strict_and_preserves_geometry(self):
        row = {"id": "a" * 64, "tenant": "b" * 64, "project": "c" * 64,
               "kind": 1, "time": 0, "ordinal": 0, "deleted": False,
               "model": IDENTITY["model"], "vector": [0.25, 0.75], "sparse": None}
        valid = corpus.validate_sanitized_row(row, IDENTITY)
        self.assertEqual(valid["vector"], row["vector"])
        for key in ("text", "name", "path", "email", "token", "unknown"):
            with self.assertRaises(ValueError):
                corpus.validate_sanitized_row(dict(row, **{key: "private"}), IDENTITY)
        for change in ({"model": "other"}, {"id": "raw-name"}, {"vector": [float("inf"), 0]},
                       {"sparse": [[1, 1], [1, 2]]}):
            with self.assertRaises(ValueError):
                corpus.validate_sanitized_row(dict(row, **change), IDENTITY)


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = fixture(Path(self.tmp.name) / "c")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_scan_scopes_deletes_and_all_metrics(self):
        t = oracle.rank(self.c, query(), block_size=1)
        self.assertEqual(t.ids("dense"), ["a", "b", "c", "d"])
        self.assertEqual(t.eligible_count, 4)
        self.assertEqual(t.ids("sparse"), ["c", "a"])
        self.assertEqual(t.ids("hybrid"), ["a", "c", "b", "d"])
        for metric in ("dot", "euclidean"):
            with tempfile.TemporaryDirectory() as d:
                c = corpus.write_corpus(Path(d) / "c", [[1, 0], [2, 0]],
                    [{"id": "a", "tenant": "t1", "project": "p1"},
                     {"id": "b", "tenant": "t1", "project": "p1"}], dict(IDENTITY, metric=metric))
                self.assertEqual(oracle.rank(c, query()).ids("dense"),
                                 ["b", "a"] if metric == "dot" else ["a", "b"])

    def test_filters_generation_and_query_validation(self):
        self.assertEqual(oracle.rank(self.c, query(lo=1, hi=2)).ids("dense"), ["b", "c"])
        self.assertEqual(oracle.rank(self.c, query(kind=2)).eligible_count, 0)
        self.assertEqual(oracle.rank(self.c, query(time_lo=2, time_hi=3)).ids("dense"), ["c", "d"])
        for bad in ({"generation": "old"}, {"tenant": ""}, {"vector": [0, 0]},
                    {"vector": [float("nan"), 1]}, {"sparse": [[1, -1]]}):
            with self.assertRaises(ValueError):
                oracle.rank(self.c, query(**bad))

    def test_exact_branch_ties_precede_prefetch_and_full_fusion_separate(self):
        t = oracle.rank(self.c, query(), prefetch=1)
        self.assertEqual(t.ids("hybrid", full=False), ["a", "c"])
        self.assertEqual(t.ids("hybrid", full=True), ["a", "c", "b", "d"])
        self.assertEqual(t.ids("dense", limit=1), ["a"])
        self.assertAlmostEqual(t.score("hybrid", "a"), 1 / 2 + 1 / 3)
        r = oracle.measure(t, ["a", "c"], "hybrid")
        self.assertFalse(r["safe"])
        self.assertEqual(r["strict_recall"], 0.5)

    def test_more_than_200_branch_ties_are_canonical_before_fusion(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [{"id": f"i{i:04}", "tenant": "t1", "project": "p1",
                     "sparse": [[1, 1]]} for i in reversed(range(205))]
            c = corpus.write_corpus(Path(d) / "c", [[1, 0]] * 205, rows, IDENTITY)
            t = oracle.rank(c, query(), prefetch=200)
            self.assertEqual(t.ids("hybrid", full=False, limit=200), [f"i{i:04}" for i in range(200)])
            self.assertEqual(t.ids("hybrid", full=True, limit=205), [f"i{i:04}" for i in range(205)])

    def test_tie_recall_empty_short_duplicate_forbidden(self):
        t = oracle.rank(self.c, query())
        r = oracle.measure(t, ["b"], "dense", top_k=1)
        self.assertEqual(r["strict_recall"], 0)
        self.assertEqual(r["tie_recall"], 1)
        self.assertTrue(r["safe"])
        for hits in (["a", "a", "c", "d"], ["a", "b", "c", "e"], ["a", "b", "c", "f"], ["a"]):
            self.assertFalse(oracle.measure(t, hits, "dense")["safe"])
        empty = oracle.rank(self.c, query(kind=2))
        self.assertIsNone(oracle.measure(empty, [], "dense")["tie_recall"])
        self.assertTrue(oracle.measure(empty, [], "dense")["safe"])
        self.assertFalse(oracle.measure(empty, ["a"], "dense")["safe"])

    def test_cell_mean_no_pooling_and_missing_no_qdrant_trigger(self):
        def row(cell, recall, **kw):
            return dict(cell=cell, status="MEASURED", safe=True, tie_recall=recall,
                        strict_recall=recall, **kw)
        cells = oracle.cells([row("broad", 1.0), row("tight", 0.9)])
        self.assertEqual(cells["broad"]["verdict"], "PASS")
        self.assertEqual(cells["tight"]["verdict"], "FAIL")
        self.assertEqual(cells["tight"]["failed_queries"], [0])
        mixed = oracle.cells([row("tight", 1), {"cell": "tight", "status": "NOT_RUN"}])
        self.assertEqual(mixed["tight"]["verdict"], "NOT_RUN")
        self.assertEqual(oracle.engine_decision(cells, dataset="synthetic", product_path=False), "UNDECIDED")
        self.assertEqual(oracle.engine_decision(cells, dataset="marlow-4oct", product_path=True,
                                             complete=True), "REOPEN_QDRANT")
        self.assertEqual(oracle.engine_decision(mixed, dataset="marlow-4oct", product_path=True,
                                             complete=True), "UNDECIDED")


class PostgresContractTests(unittest.TestCase):
    def test_query_uses_parameters_scope_and_dense_operator(self):
        sql, params = postgres.dense_query(query(), "cosine", limit=200)
        self.assertIn("<=>", sql)
        self.assertIn("tenant = %s", sql)
        self.assertIn("project = %s", sql)
        self.assertIn("NOT deleted", sql)
        self.assertIn("WITH TIES", sql)
        self.assertIn("ORDER BY distance, id", sql)
        evil = "x' OR true --"
        sql, params = postgres.dense_query(query(tenant=evil, lo=1), "dot")
        self.assertNotIn(evil, sql)
        self.assertIn(evil, params)
        self.assertIn("<#>", sql)

    def test_runtime_refuses_real_slot_and_non_digest_image(self):
        with self.assertRaises(ValueError):
            postgres.DisposablePostgres(image="pgvector:latest")
        with self.assertRaises(ValueError):
            postgres.require_synthetic({"dataset": "marlow-4oct"})

    def test_cleanup_attempts_all_owned_resources_after_failure(self):
        commands = []
        def fake(args, **kw):
            commands.append(args)
            if args[:2] == ["rm", "-f"]:
                raise RuntimeError("fixture container removal error")
            return ""
        p = postgres.DisposablePostgres(runner=fake)
        p.owned = {"container", "volume", "secret"}
        with self.assertRaises(RuntimeError):
            p.close()
        self.assertTrue(any(c[:2] == ["volume", "rm"] for c in commands))
        self.assertTrue(any(c[:2] == ["secret", "rm"] for c in commands))

    def test_runtime_security_and_owned_names(self):
        p = postgres.DisposablePostgres()
        args = p.run_args()
        self.assertIn("999:999", args)
        self.assertIn("--cap-drop=ALL", args)
        self.assertIn("--security-opt=no-new-privileges", args)
        self.assertIn("--memory=2g", args)
        self.assertIn("--cpus=2", args)
        self.assertIn("127.0.0.1::5432", args)
        self.assertRegex(p.name, r"^kaidera-dev-vector-baseline-[0-9]+$")
        self.assertNotIn("/Volumes", " ".join(args))


@unittest.skipUnless(os.environ.get("B01_PODMAN") == "1", "explicit disposable Podman fixture")
class PostgresIntegrationTests(unittest.TestCase):
    def test_actual_hnsw_filters_ties_and_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            c = fixture(Path(d) / "c")
            stack = postgres.DisposablePostgres()
            with stack:
                postgres.install(stack.connection, c)
                result = postgres.evaluate(stack.connection, c, query())
                self.assertEqual(result["ids"], ["a", "b", "c", "d"])
                self.assertTrue(result["measurement"]["safe"])
                self.assertEqual(result["measurement"]["tie_recall"], 1)
                self.assertIn("Index", result["forced_hnsw_plan"])
                self.assertTrue(stack.binding["postgres_version"])
                self.assertTrue(stack.binding["pgvector_version"])
            self.assertEqual(stack.owned, set())
            self.assertTrue(stack.cleanup_verified)


if __name__ == "__main__":
    unittest.main()
