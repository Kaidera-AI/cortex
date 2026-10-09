"""Collision snapshot gives an assertion failure, never a missing-key error."""
import copy
import unittest
from vector_baseline import postgres
from test_postgres_cleanup import ResourceEngine


class CollisionGuardTests(unittest.TestCase):
    def test_same_name_different_lifecycle_is_never_deleted(self):
        for kind in ("volume", "secret", "container"):
            with self.subTest(kind=kind):
                engine = ResourceEngine()
                stack = postgres.DisposablePostgres(runner=engine)
                engine.seed(kind, stack.name, {"kaidera.b01.lifecycle": "pre-existing"})
                before = copy.deepcopy(engine.resources)
                with self.assertRaises(RuntimeError):
                    stack.__enter__()
                self.assertEqual(engine.resources, before, "cleanup must preserve colliding external resources")
                self.assertEqual(set(engine.created), set(engine.removed))
                self.assertTrue(stack.cleanup_verified)
