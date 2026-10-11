"""Frozen source-only Vera rework: event shape and explicit pull completion."""
import json
import unittest

from test_vision_pull_cache import Client, MODEL, worker


class PullProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def assert_refused_and_retried(self, lines):
        module = worker()
        client = Client(lines)
        for _ in range(2):
            observed = None
            try:
                await module._ensure_model(client, MODEL)
            except Exception as error:
                observed = error
            self.assertNotIn(MODEL, module._pulled, "unqualified pull was cached")
            self.assertIsInstance(observed, RuntimeError, "refusal must use the fixed public failure")
            self.assertEqual(str(observed), "Ollama model pull failed")
        self.assertEqual(client.pulls, 2)
        self.assertEqual(client.closed, 2)

    async def test_array_event_refuses_with_fixed_error_and_no_cache(self):
        await self.assert_refused_and_retried(["[]"])

    async def test_string_event_refuses_with_fixed_error_and_no_cache(self):
        await self.assert_refused_and_retried([json.dumps("plain stream message")])

    async def test_progress_only_eof_refuses_with_fixed_error_and_no_cache(self):
        await self.assert_refused_and_retried([json.dumps({"status": "pulling manifest"}),
                                             json.dumps({"status": "downloading", "completed": 12})])

    async def test_error_after_success_still_refuses_and_never_caches(self):
        await self.assert_refused_and_retried([json.dumps({"status": "success"}),
                                             json.dumps({"error": "synthetic-secret-after-success"})])
