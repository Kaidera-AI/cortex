"""RED-first controls found while verifying B01, kept separate from original tests."""
import unittest
from unittest.mock import patch
from vector_baseline import oracle, postgres


class FailureControls(unittest.TestCase):
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
