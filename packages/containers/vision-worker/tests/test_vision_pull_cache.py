"""Frozen source-only pull-stream/cache controls: no model, engine or network."""
import asyncio
from contextlib import suppress
import importlib.util
import json
from pathlib import Path
import unittest

WORKER = Path(__file__).resolve().parents[1] / "worker.py"
MODEL = "synthetic-pull-cache-control"


def worker():
    spec = importlib.util.spec_from_file_location("vision_pull_cache_control", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    def __init__(self, lines, *, present=False):
        self.lines = lines
        self.present = present
        self.pulls = self.tags = self.closed = 0

    async def get(self, url):
        assert url.endswith("/api/tags")
        self.tags += 1
        client = self

        class Tags:
            def raise_for_status(self):
                pass

            def json(self):
                return {"models": [{"name": MODEL}] if client.present else []}

        return Tags()

    def stream(self, method, url, **kwargs):
        assert method == "POST" and url.endswith("/api/pull")
        assert kwargs["json"] == {"name": MODEL}
        self.pulls += 1
        client = self

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                client.closed += 1
                return False

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                for line in client.lines:
                    await asyncio.sleep(0)
                    yield line

        return Stream()


class PullCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_after_progress_never_caches_and_next_call_retries(self):
        module = worker()
        client = Client([json.dumps({"status": "pulling manifest"}),
                         json.dumps({"error": "manifest unavailable"})])
        for _ in range(2):
            with suppress(Exception):
                await module._ensure_model(client, MODEL)
            self.assertNotIn(MODEL, module._pulled, "failed pull was cached")
        self.assertEqual(client.pulls, 2)
        self.assertEqual(client.closed, 2)

    async def test_error_key_with_empty_or_null_payload_is_still_failure(self):
        for error in ("", None):
            with self.subTest(error=error):
                module = worker()
                client = Client([json.dumps({"error": error})])
                with suppress(Exception):
                    await module._ensure_model(client, MODEL)
                self.assertNotIn(MODEL, module._pulled)

    async def test_failure_exception_does_not_echo_error_payload(self):
        module = worker()
        client = Client([json.dumps({"error": "synthetic-secret-do-not-echo"})])
        with self.assertRaises(RuntimeError) as failure:
            await module._ensure_model(client, MODEL)
        self.assertNotIn("synthetic-secret-do-not-echo", str(failure.exception))
        self.assertNotIn(MODEL, module._pulled)

    async def test_successful_stream_caches_once_and_preserves_blank_lines(self):
        module = worker()
        client = Client(["", "  ", json.dumps({"status": "pulling manifest"}),
                         json.dumps({"status": "success"})])
        await module._ensure_model(client, MODEL)
        await module._ensure_model(client, MODEL)
        self.assertIn(MODEL, module._pulled)
        self.assertEqual(client.tags, 1)
        self.assertEqual(client.pulls, 1)
        self.assertEqual(client.closed, 1)

    async def test_already_present_model_caches_without_pull(self):
        module = worker()
        client = Client([], present=True)
        await module._ensure_model(client, MODEL)
        await module._ensure_model(client, MODEL)
        self.assertIn(MODEL, module._pulled)
        self.assertEqual(client.tags, 1)
        self.assertEqual(client.pulls, 0)

    async def test_malformed_stream_never_caches_and_retries(self):
        module = worker()
        client = Client(["not-json"])
        for _ in range(2):
            with suppress(Exception):
                await module._ensure_model(client, MODEL)
            self.assertNotIn(MODEL, module._pulled)
        self.assertEqual(client.pulls, 2)
