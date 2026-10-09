"""RED-first controls found while verifying B01, kept separate from original tests."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from vector_baseline import corpus, oracle, postgres


class FailureControls(unittest.TestCase):
    def test_fixture_ids_are_explicit_unique_before_writing(self):
        identity = {"provider": "synthetic", "model": "fixture", "dimension": 2,
                    "metric": "cosine", "generation": "fixture"}
        with tempfile.TemporaryDirectory() as d:
            for n, rows in enumerate(([{}], [{"id": "same"}, {"id": "same"}])):
                with self.subTest(rows=rows):
                    destination = Path(d) / str(n)
                    with self.assertRaises(ValueError):
                        corpus.write_corpus(destination, [[1, 0]] * len(rows), rows, identity)
                    self.assertFalse(destination.exists(), "invalid IDs must fail before corpus creation")

    def test_each_edge_mode_covers_all_five_cardinalities(self):
        with tempfile.TemporaryDirectory() as d:
            c = corpus.generate(Path(d) / "c", count=100000, dimension=2, model="fixture")
            q = corpus.queries(c, "heldout")
            for mode in ("dense", "hybrid", "sparse"):
                self.assertEqual({x["eligible_count"] for x in q if x["stratum"] == "edges" and x["mode"] == mode},
                                 {0, 1, 9, 10, 11})

    def test_float32_underflow_refused(self):
        identity = {"provider": "synthetic", "model": "fixture", "dimension": 2,
                    "metric": "cosine", "generation": "fixture"}
        with self.assertRaises(ValueError):
            corpus.vector_check([1e-100, 0], identity)

    def test_shared_engine_constraint(self):
        args = postgres.DisposablePostgres().run_args()
        self.assertIn("--memory=1g", args)
        self.assertIn("worker=mike", args)

    def test_search_error_and_bad_empty_are_failures(self):
        self.assertEqual(oracle.cells([{"cell": "tight", "status": "ERROR"}])["tight"]["verdict"], "FAIL")
        self.assertEqual(oracle.cells([{"cell": "empty", "status": "MEASURED", "safe": False,
                                        "tie_recall": None}])["empty"]["verdict"], "FAIL")

    def test_failed_start_removes_created_container_volume_and_secret(self):
        commands = []
        def fake(args, **kw):
            commands.append(args)
            if args[0] == "start":
                raise RuntimeError("fixture startup failure")
            if args[0] == "run":
                raise RuntimeError("fixture failed run after creating container")
            return "created" if args[0] == "create" else ""
        p = postgres.DisposablePostgres(runner=fake)
        with self.assertRaises(RuntimeError):
            with p:
                self.fail("startup should not reach body")
        self.assertTrue(any(c[:2] == ["rm", "-f"] for c in commands))
        self.assertTrue(any(c[:2] == ["volume", "rm"] for c in commands))
        self.assertTrue(any(c[:2] == ["secret", "rm"] for c in commands))
        self.assertTrue(p.cleanup_verified)


if __name__ == "__main__":
    unittest.main()
