"""A lifecycle is fresh, shared by its resources, and never restarted."""
import unittest
from vector_baseline import postgres
from test_postgres_cleanup import ResourceEngine, create_resource


class LifecycleTests(unittest.TestCase):
    def test_one_stack_instance_cannot_reuse_its_creation_identity(self):
        engine = ResourceEngine(fault="volume", side_effect=False)
        stack = postgres.DisposablePostgres(runner=engine)
        with self.assertRaises(RuntimeError):
            stack.__enter__()
        before = len(engine.commands)
        caught = None
        try:
            stack.__enter__()
        except Exception as exc:
            caught = exc
        self.assertEqual(len(engine.commands), before, "used lifecycle must refuse before any engine operation")
        self.assertIsInstance(caught, ValueError)

    def test_every_create_has_same_fresh_identity_and_stacks_differ(self):
        lifecycles = []
        for _ in range(2):
            engine = ResourceEngine()
            stack = postgres.DisposablePostgres(runner=engine)
            with self.assertRaises(RuntimeError):
                stack.__enter__()
            identities = []
            for args, _ in engine.commands:
                if not create_resource(args):
                    continue
                labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
                self.assertIn("kaidera.b01.lifecycle", labels)
                self.assertEqual(labels.get("worker"), "mike")
                identities.append(labels["kaidera.b01.lifecycle"])
            self.assertEqual(len(identities), 3)
            self.assertEqual(len(set(identities)), 1)
            self.assertRegex(identities[0], r"^[0-9a-f]{32}$")
            lifecycles.append(identities[0])
        self.assertNotEqual(*lifecycles)
