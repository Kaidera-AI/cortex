import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cortex_core.release.build import engine, policy


class Engine(unittest.TestCase):
    def test_actual_adapter_full_argv_and_closed_stdio(self):
        seen = {}

        class Process:
            pid = 123

            def wait(self, timeout):
                seen["timeout"] = timeout
                return 0

        def popen(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return Process()

        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            row = engine.run_operation(
                ["/usr/bin/podman", "build", "ctx"],
                p,
                {"PYTHONHASHSEED": "0"},
                30,
                p / "out",
                p / "err",
                popen=popen,
            )
            self.assertEqual(row["exit"], 0)
            self.assertTrue(seen["start_new_session"])
            self.assertTrue(seen["stdout"].closed)
            self.assertTrue(seen["stderr"].closed)

    def test_timeout_kills_only_exact_owned_process_group(self):
        calls = []

        class Process:
            pid = 123
            waits = 0

            def wait(self, timeout):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("engine", timeout)
                return -15

        with (
            tempfile.TemporaryDirectory() as t,
            patch.object(engine.os, "killpg", side_effect=lambda *a: calls.append(a)),
        ):
            p = Path(t)
            with self.assertRaises(subprocess.TimeoutExpired):
                engine.run_operation(
                    ["engine"],
                    p,
                    {},
                    1,
                    p / "out",
                    p / "err",
                    popen=lambda *a, **k: Process(),
                )
        self.assertEqual(calls, [(123, 15)])

    def test_fresh_roots_reject_shared_and_nested_storage(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            for rows in [[p / "a", p / "a"], [p / "a", p / "a/child"], [p]]:
                with self.assertRaises(ValueError):
                    engine.require_fresh_paths(rows)

    def test_private_environment_cannot_enter_build_or_evidence(self):
        self.assertEqual(
            policy.public_environment({"PYTHONHASHSEED": "0"}, ["PYTHONHASHSEED"]),
            {"PYTHONHASHSEED": "0"},
        )
        with self.assertRaises(ValueError):
            policy.public_environment({"API_KEY": "secret"}, ["API_KEY"])


class Cancellation(unittest.TestCase):
    def test_interruption_still_cancels_owned_child(self):
        calls = []

        class Process:
            pid = 456
            waits = 0

            def wait(self, timeout):
                self.waits += 1
                if self.waits == 1:
                    raise KeyboardInterrupt()
                return -15

        with (
            tempfile.TemporaryDirectory() as t,
            patch.object(engine.os, "killpg", side_effect=lambda *a: calls.append(a)),
        ):
            p = Path(t)
            with self.assertRaises(KeyboardInterrupt):
                engine.run_operation(
                    ["engine"],
                    p,
                    {},
                    1,
                    p / "out",
                    p / "err",
                    popen=lambda *a, **k: Process(),
                )
        self.assertEqual(calls, [(456, 15)])

    def test_real_child_stdout_stderr_and_exit_are_captured(self):
        import sys

        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            row = engine.run_operation(
                [
                    sys.executable,
                    "-c",
                    'import sys; print("out"); print("err", file=sys.stderr); sys.exit(7)',
                ],
                p,
                {},
                5,
                p / "out",
                p / "err",
            )
            self.assertEqual(row["exit"], 7)
            self.assertEqual((p / "out").read_bytes(), b"out\n")
            self.assertEqual((p / "err").read_bytes(), b"err\n")


class Deadlines(unittest.TestCase):
    def test_phase_budget_can_exceed_old_generic_120_second_cap(self):
        deadline = engine.Deadline(600, clock=lambda: 10)
        self.assertEqual(deadline.phase_timeout(240), 240)

    def test_remaining_absolute_budget_bounds_each_phase(self):
        now = [10]
        deadline = engine.Deadline(600, clock=lambda: now[0])
        now[0] = 590
        self.assertEqual(deadline.phase_timeout(240), 20)
        now[0] = 611
        with self.assertRaises(TimeoutError):
            deadline.phase_timeout(240)

    def test_invalid_nonfinite_or_boolean_timeout_refuses(self):
        for value in [True, float("nan"), float("inf"), -1, 0]:
            with self.assertRaises(ValueError):
                engine.Deadline(value)


class OwnedRoots(unittest.TestCase):
    def test_created_roots_cleanup_and_external_root_refusal(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            owned = engine.OwnedRoots([p / "store", p / "runroot"])
            owned.create()
            (p / "store" / "generated").write_bytes(b"owned")
            result = owned.cleanup()
            self.assertTrue(result["complete"])
            self.assertEqual(result["remaining_owned_resources"], [])
            self.assertFalse((p / "store").exists())
            with self.assertRaises(ValueError):
                engine.OwnedRoots([p]).create()

    def test_replaced_root_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            owned = engine.OwnedRoots([p / "store"])
            owned.create()
            (p / "store").rename(p / "original")
            (p / "store").mkdir()
            (p / "store" / "foreign").write_bytes(b"keep")
            result = owned.cleanup()
            self.assertFalse(result["complete"])
            self.assertEqual((p / "store" / "foreign").read_bytes(), b"keep")
