"""The real-data slot accepts one vector, while corpus checks admit batches."""
import unittest
from vector_baseline import corpus


class SanitizedRowShapeTests(unittest.TestCase):
    def row(self, vector):
        return {"id": "a" * 64, "tenant": "b" * 64, "project": "c" * 64,
                "kind": 1, "time": 0, "ordinal": 0, "deleted": False,
                "model": "fixture", "vector": vector, "sparse": None}

    def identity(self, metric):
        return {"provider": "synthetic", "model": "fixture", "dimension": 2,
                "metric": metric, "generation": "fixture"}

    def test_nested_matrices_are_rejected_at_single_row_boundary(self):
        for metric in ("cosine", "dot", "euclidean"):
            for vector in ([[1, 0]], [[1, 0], [0, 1]], [[[1, 0]]]):
                with self.subTest(metric=metric, vector=vector):
                    with self.assertRaises(ValueError):
                        corpus.validate_sanitized_row(self.row(vector), self.identity(metric))

    def test_one_vector_preserves_geometry_and_corpus_batches_remain_valid(self):
        for metric in ("cosine", "dot", "euclidean"):
            with self.subTest(metric=metric):
                vector = [0.25, 0.75]
                result = corpus.validate_sanitized_row(self.row(vector), self.identity(metric))
                self.assertIs(result["vector"], vector)
                corpus.vector_check([[1, 0], [0, 1]], self.identity(metric))
                if metric != "cosine":
                    self.assertEqual(corpus.validate_sanitized_row(self.row([0, 0]),
                                     self.identity(metric))["vector"], [0, 0])
