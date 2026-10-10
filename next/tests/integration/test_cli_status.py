"""Frozen client controls: fake HTTP/ASGI transports only, never a listener."""
import asyncio
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import UUID

import httpx

from cortex_core.cli import status
from cortex_core.conductor.metrics import MetricIdentity, Metrics
from cortex_core.gateway.health_metrics import CoreSample, TelemetryGateway

ENDPOINT = "http://127.0.0.1:17891"
SECRET = "synthetic-status-private-value"


def receipt(state="ok"):
    return {"component": "cortex", "status": state, "core_available": state != "unavailable",
            "conductor": {"ok": "healthy", "degraded": "supervisor_down", "unavailable": "core_unavailable"}[state],
            "reason": "core_unavailable" if state == "unavailable" else None}


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0, block=0):
        self.chunks, self.delay, self.block = chunks, delay, block
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.block:
                time.sleep(self.block)
            yield chunk

    async def aclose(self):
        self.closed = True


class CliStatusTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, handler, *, endpoint=ENDPOINT, json_mode=True, argv=None):
        calls = []

        async def handle(request):
            calls.append(request)
            answer = handler(request)
            return await answer if hasattr(answer, "__await__") else answer

        out = StringIO()
        args = ["status", "--endpoint", endpoint] if argv is None else argv
        if json_mode:
            args = [*args, "--json"]
        code = await status.run(args, transport=httpx.MockTransport(handle), out=out)
        text = out.getvalue()
        return code, json.loads(text) if json_mode else text, calls

    def reply(self, state="ok"):
        return lambda _: httpx.Response(503 if state == "unavailable" else 200, json=receipt(state))

    async def test_healthy_json_is_exact_receipt_and_one_health_get(self):
        code, packet, calls = await self.invoke(self.reply())
        self.assertEqual(code, 0)
        self.assertEqual(packet, {"schema": "cortex.status.v1", "health": receipt(), "client_error": None})
        self.assertEqual([(r.method, str(r.url)) for r in calls], [("GET", ENDPOINT + "/health")])

    async def test_degraded_is_distinct_from_healthy_success(self):
        code, packet, _ = await self.invoke(self.reply("degraded"))
        self.assertEqual(code, 1)
        self.assertEqual(packet["health"], receipt("degraded"))

    async def test_core_unavailable_preserves_server_receipt(self):
        code, packet, _ = await self.invoke(self.reply("unavailable"))
        self.assertEqual(code, 2)
        self.assertEqual(packet["health"], receipt("unavailable"))
        self.assertIsNone(packet["client_error"])

    async def test_human_output_reports_core_and_conductor_without_readiness(self):
        code, text, _ = await self.invoke(self.reply("degraded"), json_mode=False)
        self.assertEqual(code, 1)
        self.assertIn("Core: available", text)
        self.assertIn("Conductor: supervisor_down", text)
        self.assertNotIn("ready", text.lower())
        self.assertNotIn("Cortex status: ok", text)

    async def test_actual_gateway_states_are_preserved_without_sampling(self):
        for state, expected in [("healthy", "ok"), ("supervisor_down", "degraded"), ("core_unavailable", "unavailable")]:
            calls = []

            async def health():
                calls.append("health")
                return {"state": state, "core_available": state != "core_unavailable", "password": SECRET}

            async def sample():
                calls.append("sample")
                return CoreSample(1, 0)

            gateway = TelemetryGateway(Metrics(MetricIdentity(UUID(int=1), "test", "test")), health, sample)
            out = StringIO()
            code = await status.run(["status", "--endpoint", ENDPOINT, "--json"],
                                    transport=httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 1234)), out=out)
            self.assertEqual(code, {"ok": 0, "degraded": 1, "unavailable": 2}[expected])
            self.assertEqual(json.loads(out.getvalue())["health"], receipt(expected))
            self.assertEqual(calls, ["health"])
            self.assertNotIn(SECRET, out.getvalue())

    async def test_ipv6_loopback_origin_is_supported(self):
        code, _, calls = await self.invoke(self.reply(), endpoint="http://[::1]:17891/")
        self.assertEqual(code, 0)
        self.assertEqual(str(calls[0].url), "http://[::1]:17891/health")

    async def test_external_and_dns_endpoints_refused_before_transport(self):
        for endpoint in ["http://198.51.100.8:17891", "http://0.0.0.0:17891", "http://localhost:17891", "http://[::]:17891"]:
            code, packet, calls = await self.invoke(self.reply(), endpoint=endpoint)
            self.assertEqual((code, calls), (64, []))
            self.assertEqual(packet["client_error"], "endpoint_refused")

    async def test_endpoint_paths_credentials_queries_and_bad_ports_are_refused(self):
        for endpoint in [ENDPOINT + "/health", ENDPOINT + "/../health", ENDPOINT + "/?x=1",
                         ENDPOINT + "#private", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                         "http://127.0.0.1", "file:///private", f"http://user:{SECRET}@127.0.0.1:17891",
                         "http://127.0.0.1:17891\\@198.51.100.8:80", "http://127.0.0.1:17891\n"]:
            code, packet, calls = await self.invoke(self.reply(), endpoint=endpoint)
            self.assertEqual((code, calls), (64, []))
            self.assertNotIn(SECRET, json.dumps(packet))

    async def test_missing_endpoint_and_unknown_command_refuse_without_default_guess(self):
        for args in [["status"], ["repair", "--endpoint", ENDPOINT], ["status", "--bad", SECRET]]:
            code, packet, calls = await self.invoke(self.reply(), argv=args)
            self.assertEqual((code, calls), (64, []))
            self.assertIsNone(packet["health"])
            self.assertNotIn(SECRET, json.dumps(packet))

    async def test_response_http_and_state_must_agree(self):
        bad = receipt()
        bad["conductor"] = "supervisor_down"
        for wire_code, body in [(503, receipt()), (200, receipt("unavailable")), (200, bad), (403, receipt()), (500, receipt())]:
            code, packet, _ = await self.invoke(lambda _, c=wire_code, b=body: httpx.Response(c, json=b))
            self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "invalid_response"))

    async def test_extra_fields_are_refused_without_echoing_private_payload(self):
        code, packet, _ = await self.invoke(lambda _: httpx.Response(200, json={**receipt(), "password": SECRET}))
        self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "invalid_response"))
        self.assertNotIn(SECRET, json.dumps(packet))

    async def test_strict_boolean_and_closed_reason_are_required(self):
        for field, value in [("core_available", 1), ("reason", SECRET), ("component", "other"), ("status", "ready"), ("conductor", "unknown")]:
            bad = receipt()
            bad[field] = value
            code, packet, _ = await self.invoke(lambda _, b=bad: httpx.Response(200, json=b))
            self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "invalid_response"))
            self.assertNotIn(SECRET, json.dumps(packet))

    async def test_invalid_json_duplicate_keys_and_wrong_content_type_are_refused(self):
        for content, kind in [(SECRET.encode(), "application/json"), (b'[]', "application/json"),
                              (json.dumps(receipt()).encode(), "text/plain"),
                              (b'{"component":"wrong",' + json.dumps(receipt()).encode()[1:], "application/json")]:
            code, packet, _ = await self.invoke(lambda _, c=content, k=kind: httpx.Response(200, content=c, headers={"content-type": k}))
            self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "invalid_response"))

    async def test_redirect_is_not_followed_even_to_another_valid_health_reply(self):
        def handler(request):
            if request.url.host == "127.0.0.1":
                return httpx.Response(302, headers={"location": f"http://198.51.100.8/{SECRET}"})
            return httpx.Response(200, json=receipt())

        code, packet, calls = await self.invoke(handler)
        self.assertEqual((code, len(calls)), (2, 1))
        self.assertNotIn(SECRET, json.dumps(packet))

    async def test_environment_proxy_cannot_replace_local_health_transport(self):
        seen = []

        class FakeNetwork(httpx.AsyncBaseTransport):
            def __init__(self, **options):
                self.proxy = options.get("proxy")

            async def handle_async_request(self, request):
                seen.append(self.proxy is not None)
                return httpx.Response(503 if self.proxy else 200, json=receipt("unavailable" if self.proxy else "ok"))

        env = {"HTTP_PROXY": "http://proxy.invalid:19999", "HTTPS_PROXY": "http://proxy.invalid:19999",
               "ALL_PROXY": "http://proxy.invalid:19999", "NO_PROXY": "",
               "http_proxy": "http://proxy.invalid:19999", "https_proxy": "http://proxy.invalid:19999",
               "all_proxy": "http://proxy.invalid:19999", "no_proxy": ""}
        out = StringIO()
        with patch.dict(os.environ, env), patch("httpx._client.AsyncHTTPTransport", FakeNetwork):
            code = await status.run(["status", "--endpoint", ENDPOINT, "--json"], out=out)
        self.assertEqual((code, seen), (0, [False]))
        self.assertEqual(json.loads(out.getvalue())["health"], receipt())

    async def test_connection_failure_is_scrubbed_and_never_retried(self):
        def handler(_):
            raise httpx.ConnectError(SECRET)

        code, packet, calls = await self.invoke(handler)
        self.assertEqual((code, len(calls)), (2, 1))
        self.assertEqual(packet, {"schema": "cortex.status.v1", "health": None, "client_error": "connection_unavailable"})

    async def test_oversized_stream_is_stopped_and_closed(self):
        body = Body([b" " * 8192, b" " * 8192, b" " + json.dumps(receipt()).encode()])
        code, packet, _ = await self.invoke(lambda _: httpx.Response(200, stream=body, headers={"content-type": "application/json"}))
        self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "invalid_response"))
        self.assertTrue(body.closed)

    async def test_false_content_length_does_not_bypass_actual_body_cap(self):
        body = Body([b" " * 16384, json.dumps(receipt()).encode()])
        code, packet, _ = await self.invoke(lambda _: httpx.Response(200, stream=body, headers={"content-type": "application/json", "content-length": "1"}))
        self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "invalid_response"))
        self.assertTrue(body.closed)

    async def test_whole_body_deadline_cancels_stalled_stream(self):
        body = Body([json.dumps(receipt()).encode()], delay=30)
        started = time.monotonic()
        with patch.object(status, "BUDGET_SECONDS", 0.25):
            code, packet, _ = await self.invoke(lambda _: httpx.Response(200, stream=body, headers={"content-type": "application/json"}))
        self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "timeout"))
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(body.closed)

    async def test_late_blocking_body_cannot_be_reported_healthy(self):
        body = Body([json.dumps(receipt()).encode()], block=0.35)
        with patch.object(status, "BUDGET_SECONDS", 0.25):
            code, packet, _ = await self.invoke(lambda _: httpx.Response(200, stream=body, headers={"content-type": "application/json"}))
        self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "timeout"))
        self.assertTrue(body.closed)

    async def test_caller_cancellation_propagates_and_closes_response(self):
        entered = asyncio.Event()

        class Stalled(Body):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""

        body = Stalled([])
        task = asyncio.create_task(self.invoke(lambda _: httpx.Response(200, stream=body, headers={"content-type": "application/json"})))
        try:
            try:
                await asyncio.wait_for(entered.wait(), 1)
            except TimeoutError:
                self.fail("client never entered the fake response stream")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(body.closed)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_interrupted_public_main_returns_130_without_traceback(self):
        source = Path(status.__file__).parents[2]
        env = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
        script = 'import httpx; from cortex_core.cli.status import main; defn = "def stop(r):\\n raise KeyboardInterrupt\\n"; exec(defn); raise SystemExit(main(["status", "--endpoint", "http://127.0.0.1:17891", "--json"], transport=httpx.MockTransport(stop)))'
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 130)
        self.assertNotIn("Traceback", result.stderr)

    async def test_server_timeout_reason_is_preserved(self):
        value = {**receipt("unavailable"), "reason": "timeout"}
        code, packet, _ = await self.invoke(lambda _: httpx.Response(503, json=value))
        self.assertEqual((code, packet["health"], packet["client_error"]), (2, value, None))

    async def test_request_headers_are_inside_whole_deadline(self):
        async def handler(_):
            await asyncio.sleep(30)
            return httpx.Response(200, json=receipt())

        started = time.monotonic()
        with patch.object(status, "BUDGET_SECONDS", 0.25):
            code, packet, calls = await self.invoke(handler)
        self.assertEqual((code, packet["client_error"], len(calls)), (2, "timeout", 1))
        self.assertLess(time.monotonic() - started, 2)

    async def test_parse_is_inside_deadline(self):
        original = json.loads

        def slow_loads(*args, **kwargs):
            time.sleep(0.35)
            return original(*args, **kwargs)

        with patch.object(status, "BUDGET_SECONDS", 0.25), patch.object(status.json, "loads", slow_loads):
            code, packet, _ = await self.invoke(self.reply())
        self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "timeout"))

    async def test_exact_limit_body_is_accepted(self):
        raw = json.dumps(receipt()).encode()
        body = Body([b" " * (16384 - len(raw)), raw])
        code, packet, _ = await self.invoke(lambda _: httpx.Response(200, stream=body, headers={"content-type": "application/json"}))
        self.assertEqual((code, packet["health"]), (0, receipt()))
        self.assertTrue(body.closed)

    async def test_compressed_or_non_utf8_response_is_refused(self):
        for raw, headers in [(json.dumps(receipt()).encode(), {"content-encoding": "gzip"}), (b"\xff", {})]:
            body = Body([raw])
            code, packet, _ = await self.invoke(lambda _: httpx.Response(200, stream=body, headers={"content-type": "application/json", **headers}))
            self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "invalid_response"))
            self.assertTrue(body.closed)

    async def test_malformed_nested_types_cannot_escape_as_traceback(self):
        for field in receipt():
            bad = {**receipt(), field: {"password": SECRET}}
            code, packet, _ = await self.invoke(lambda _: httpx.Response(200, json=bad))
            self.assertEqual((code, packet["health"], packet["client_error"]), (2, None, "invalid_response"))
            self.assertNotIn(SECRET, json.dumps(packet))


if __name__ == "__main__":
    unittest.main()
