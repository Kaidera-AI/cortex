"""Installed-entry dispatch and configured default endpoint, RED first."""

from pathlib import Path
import unittest

from cortex_core.cli.dispatch import dispatch


class DispatchTests(unittest.TestCase):
    def test_status_uses_explicit_configured_loopback_default(self):
        calls = []
        def status_main(argv):
            calls.append(argv)
            return 7
        result = dispatch(["status", "--json"],
                          {"CORTEX_API_URL": "http://127.0.0.1:9187"}, status_main)
        self.assertEqual(result, 7)
        self.assertEqual(calls, [["status", "--endpoint", "http://127.0.0.1:9187", "--json"]])

    def test_explicit_endpoint_wins_and_missing_default_refuses(self):
        calls = []
        def status_main(argv):
            calls.append(argv)
            return 0
        dispatch(["status", "--endpoint", "http://127.0.0.1:9012"],
                 {"CORTEX_API_URL": "http://127.0.0.1:9187"}, status_main)
        self.assertEqual(calls[-1], ["status", "--endpoint", "http://127.0.0.1:9012"])
        dispatch(["status"], {}, status_main)
        self.assertEqual(calls[-1], ["status"])

    def test_public_script_exists_and_dispatches_source_main(self):
        path = Path(__file__).resolve().parents[2] / "bin/cortex"
        body = path.read_text()
        self.assertIn("from cortex_core.cli.dispatch import main", body)
        self.assertTrue(body.startswith("#!/usr/bin/env python3"))


if __name__ == "__main__":
    unittest.main()
