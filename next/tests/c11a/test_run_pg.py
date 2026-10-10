"""C11a disposable-runner admission and exact-owned cleanup controls."""

import importlib.util
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch


RUNNER = Path(os.environ.get("C11A_RUNNER_PATH", Path(__file__).with_name("run_pg.py")))
SPEC = importlib.util.spec_from_file_location("c11a_runner_under_test", RUNNER)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_memory_below_35_percent_refuses_before_podman(self):
        calls = []
        def command(*args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0,
                stdout="System-wide memory free percentage: 34%\n")
        with patch.object(runner, "command", side_effect=command):
            caught = None
            try:
                runner.preflight()
            except Exception as error:
                caught = error
        self.assertIsInstance(caught, RuntimeError)
        self.assertRegex(str(caught), "35%")
        self.assertEqual(calls, [("memory_pressure", "-Q")])

    def test_owned_stack_has_bounded_resources_and_cleanup_on_runner_failure(self):
        calls = []
        def command(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("podman", "port"):
                return subprocess.CompletedProcess(args, 0, stdout="127.0.0.1:49222\n")
            if args[:2] == ("podman", "ps"):
                return subprocess.CompletedProcess(args, 0, stdout="")
            return subprocess.CompletedProcess(args, 0, stdout="")
        with (patch.object(runner, "preflight", return_value=42),
              patch.object(runner, "uuid4", return_value=SimpleNamespace(hex="a" * 32)),
              patch.object(runner, "command", side_effect=command),
              patch.object(runner.subprocess, "run", side_effect=RuntimeError("synthetic test failure"))):
            with self.assertRaisesRegex(RuntimeError, "synthetic test failure"):
                runner.main()
        name = "kaidera-test-cortex-c11a-" + "a" * 10
        start = next(args for args in calls if args[:2] == ("podman", "run"))
        self.assertIn(("--cpus", "2"), list(zip(start, start[1:])))
        self.assertIn(("--memory", "1g"), list(zip(start, start[1:])))
        self.assertIn(("--pull=never", "--name"), list(zip(start, start[1:])))
        self.assertIn(("-p", "127.0.0.1::5432"), list(zip(start, start[1:])))
        self.assertIn(("podman", "rm", "-f", "-v", name), calls)
        self.assertIn(("podman", "ps", "-a", "--filter", "label=cortex.test=cox-c11a-" + "a" * 10,
                       "--format", "{{.Names}}"), calls)


if __name__ == "__main__":
    unittest.main()
