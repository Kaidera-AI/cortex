"""Fake local collector/scraper; no listener, telemetry backend or credentials."""
import asyncio
import os
import socket
import time
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
from prometheus_client.parser import text_string_to_metric_families

from cortex_core.conductor.metrics import MetricIdentity, Metrics
from cortex_core.gateway.health_metrics import CoreSample, TelemetryGateway

SECRET = "synthetic-monitoring-secret"


class FakeScraper:
    def __init__(self, app, peer=("127.0.0.1", 1234)):
        self.app, self.peer = app, peer
        self.forwarded = []

    async def pull(self, path, *, headers=None, method="GET"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=self.peer),
            base_url="http://local-collector.invalid",
        ) as client:
            return await client.request(method, path, headers=headers)

    def forward(self, text):
        self.forwarded.append(text)  # Fake forwarding; never a network sender.


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.metrics = Metrics(MetricIdentity(UUID(int=1), "test", "test"))
        self.metrics.observe_api(0.031)
        self.metrics.observe_search(0.11)
        self.health = {"core_available": True, "state": "healthy", "fence": 77,
                       "password": SECRET, "provider_keys": SECRET}
        self.sample = CoreSample(12345, 7)
        self.health_calls = self.sample_calls = 0

        async def health():
            self.health_calls += 1
            return self.health

        async def sample():
            self.sample_calls += 1
            return self.sample

        self.read_health, self.read_sample = health, sample
        self.gateway = self.make()
        self.scraper = FakeScraper(self.gateway.app)

    def make(self, **options):
        return TelemetryGateway(self.metrics, self.read_health, self.read_sample, **options)

    def samples(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/plain; version=0.0.4"))
        return {s.name: s for f in text_string_to_metric_families(response.text) for s in f.samples}

    async def test_fake_scraper_gets_closed_metrics_and_can_forward_locally(self):
        response = await self.scraper.pull("/metrics")
        samples = self.samples(response)
        self.assertEqual(set(samples), {"cortex_api_latency_seconds", "cortex_search_latency_seconds",
                                       "cortex_db_bytes", "cortex_embed_backlog"})
        for sample in samples.values():
            self.assertEqual(sample.labels, {"deployment_id": str(UUID(int=1)), "environment": "test",
                                             "component": "cortex", "release": "test"})
        self.assertEqual(samples["cortex_db_bytes"].value, 12345)
        self.assertEqual(samples["cortex_embed_backlog"].value, 7)
        self.assertEqual(samples["cortex_api_latency_seconds"].value, 0.031)
        self.scraper.forward(response.text)
        self.assertEqual(self.scraper.forwarded, [response.text])
        self.assertEqual((self.health_calls, self.sample_calls), (1, 1))

    async def test_health_is_minimal_typed_sanitized_and_not_cached(self):
        response = await self.scraper.pull("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"component": "cortex", "status": "ok", "core_available": True,
                                           "conductor": "healthy", "reason": None})
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn("fence", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual((await self.scraper.pull("/metrics")).headers["cache-control"], "no-store")

    async def test_core_outage_reports_health_and_refuses_stale_metrics(self):
        self.metrics.update_core(999999, 123)
        self.health = {"core_available": False, "state": "core_unavailable"}
        health = await self.scraper.pull("/health")
        self.assertEqual(health.status_code, 503)
        self.assertEqual(health.json()["status"], "unavailable")
        self.assertIs(health.json()["core_available"], False)
        metrics = await self.scraper.pull("/metrics")
        self.assertEqual(metrics.status_code, 503)
        self.assertEqual(metrics.json()["error"]["reason"], "core_unavailable")
        self.assertNotIn("999999", metrics.text)
        self.assertEqual(self.sample_calls, 0)

    async def test_conductor_down_is_degraded_while_core_metrics_remain_available(self):
        self.health = {"core_available": True, "state": "supervisor_down"}
        response = await self.scraper.pull("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "degraded")
        self.assertEqual(self.samples(await self.scraper.pull("/metrics"))["cortex_db_bytes"].value, 12345)

    async def test_external_or_missing_peer_and_spoofed_headers_are_refused_first(self):
        for peer in (("198.51.100.7", 1234), None, ("collector.invalid", 1234)):
            with self.subTest(peer=peer):
                scraper = FakeScraper(self.gateway.app, peer)
                for path in ("/health", "/metrics"):
                    response = await scraper.pull(path, headers={"X-Forwarded-For": "127.0.0.1", "Host": "localhost"})
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["error"]["reason"], "loopback_required")
        self.assertEqual((self.health_calls, self.sample_calls), (0, 0))

    async def test_binding_is_literal_loopback_with_proxy_trust_disabled(self):
        for host in ("127.0.0.1", "127.4.3.2", "::1"):
            gateway = self.make(host=host, port=43210)
            runner = Mock(return_value="fake-started")
            self.assertEqual(gateway.serve(runner), "fake-started")
            runner.assert_called_once_with(gateway.app, host=host, port=43210,
                                           proxy_headers=False, forwarded_allow_ips="")
        for host in ("0.0.0.0", "::", "198.51.100.7", "localhost", "127.0.0.1.invalid", None):
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.make(host=host)
        for port in (-1, 65536, True, "43210"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.make(port=port)

    async def test_dependency_exceptions_are_scrubbed_before_json(self):
        async def failed():
            raise RuntimeError(SECRET)
        for port in ("health", "sample"):
            gateway = TelemetryGateway(self.metrics, failed if port == "health" else self.read_health,
                                       failed if port == "sample" else self.read_sample)
            scraper = FakeScraper(gateway.app)
            response = await scraper.pull("/health" if port == "health" else "/metrics")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(SECRET, response.text)
            self.assertNotIn("RuntimeError", response.text)

    async def test_binding_rechecks_reconfigured_host_and_port_before_runner(self):
        for field, value in (("host", "0.0.0.0"), ("host", "collector.invalid"), ("port", True)):
            with self.subTest(field=field, value=value):
                gateway = self.make()
                setattr(gateway, field, value)
                runner = Mock()
                with self.assertRaises(ValueError):
                    gateway.serve(runner)
                runner.assert_not_called()

    async def test_noop_or_invalid_core_sample_cannot_serve_old_success(self):
        self.metrics.update_core(999999, 123)
        for value in (None, {"db_bytes": 1, "embed_backlog": 1}, CoreSample(True, 1),
                      CoreSample(-1, 1), CoreSample(1, float("nan"))):
            with self.subTest(value=value):
                self.sample = value
                response = await self.scraper.pull("/metrics")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["reason"], "sample_unavailable")
                self.assertNotIn("999999", response.text)

    async def test_every_scrape_updates_actual_supplied_core_values(self):
        first = self.samples(await self.scraper.pull("/metrics"))
        self.sample = CoreSample(45678, 0)
        second = self.samples(await self.scraper.pull("/metrics"))
        self.assertEqual(first["cortex_db_bytes"].value, 12345)
        self.assertEqual(second["cortex_db_bytes"].value, 45678)
        self.assertEqual(second["cortex_embed_backlog"].value, 0)
        self.assertEqual(self.sample_calls, 2)

    async def test_malformed_health_receipts_cannot_claim_healthy(self):
        for value in (None, {}, {"core_available": "true", "state": "healthy"},
                      {"core_available": True, "state": "core_unavailable"},
                      {"core_available": False, "state": "healthy"},
                      {"core_available": True, "state": SECRET}):
            with self.subTest(value=value):
                self.health = value
                response = await self.scraper.pull("/health")
                self.assertEqual(response.status_code, 503)
                self.assertIs(response.json()["core_available"], False)
                self.assertNotIn(SECRET, response.text)

    async def test_one_whole_budget_covers_health_and_sampling(self):
        cancelled = asyncio.Event()
        async def health():
            await asyncio.sleep(0.015)
            return self.health
        async def sample():
            try:
                await asyncio.sleep(0.015)
                return self.sample
            finally:
                cancelled.set()
        gateway = TelemetryGateway(self.metrics, health, sample, timeout_seconds=0.02)
        start = time.monotonic()
        response = await FakeScraper(gateway.app).pull("/metrics")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["reason"], "timeout")
        self.assertLess(time.monotonic() - start, 0.08)
        self.assertTrue(cancelled.is_set())

    async def test_late_callback_cannot_return_success_after_deadline(self):
        async def sample():
            time.sleep(0.04)  # Intentional event-loop stall; timeout must still refuse success.
            return self.sample
        gateway = TelemetryGateway(self.metrics, self.read_health, sample, timeout_seconds=0.02)
        response = await FakeScraper(gateway.app).pull("/metrics")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["reason"], "timeout")

    async def test_stalled_sampler_is_cancelled_within_request_budget(self):
        closed = asyncio.Event()
        async def sample():
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        gateway = TelemetryGateway(self.metrics, self.read_health, sample, timeout_seconds=0.02)
        try:
            response = await asyncio.wait_for(FakeScraper(gateway.app).pull("/metrics"), 0.08)
        except TimeoutError:
            self.fail("The gateway failed to bound the stalled sampler")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["reason"], "timeout")
        self.assertTrue(closed.is_set())

    async def test_cancellation_propagates_and_fake_scraper_closes(self):
        started, closed = asyncio.Event(), asyncio.Event()
        async def sample():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        gateway = TelemetryGateway(self.metrics, self.read_health, sample)
        task = asyncio.create_task(FakeScraper(gateway.app).pull("/metrics"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())

    async def test_late_metric_serialization_cannot_return_success(self):
        export = self.metrics.export
        def slow():
            time.sleep(0.04)
            return export()
        gateway = self.make(timeout_seconds=0.02)
        with patch.object(self.metrics, "export", side_effect=slow):
            response = await FakeScraper(gateway.app).pull("/metrics")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["reason"], "timeout")

    async def test_no_monitoring_credential_or_outbound_connection_is_used(self):
        with patch.dict(os.environ, {"KOS_MONITORING_TOKEN": SECRET}), patch.object(
            socket.socket, "connect", side_effect=AssertionError("unexpected outbound connection")
        ):
            health = await self.scraper.pull("/health")
            metrics = await self.scraper.pull("/metrics")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(metrics.status_code, 200)
            self.assertNotIn(SECRET, health.text + metrics.text)

    async def test_post_cannot_invoke_read_ports(self):
        for path in ("/health", "/metrics"):
            self.assertEqual((await self.scraper.pull(path, method="POST")).status_code, 405)
        self.assertEqual((self.health_calls, self.sample_calls), (0, 0))

    async def test_unobserved_latencies_are_omitted_and_ports_are_required(self):
        metrics = Metrics(MetricIdentity(UUID(int=1), "test", "test"))
        gateway = TelemetryGateway(metrics, self.read_health, self.read_sample)
        samples = self.samples(await FakeScraper(gateway.app).pull("/metrics"))
        self.assertEqual(set(samples), {"cortex_db_bytes", "cortex_embed_backlog"})
        for health, sample in ((None, self.read_sample), (self.read_health, None)):
            with self.assertRaises(ValueError):
                TelemetryGateway(metrics, health, sample)
        for timeout in (0, -1, True, float("nan"), 3):
            with self.assertRaises(ValueError):
                self.make(timeout_seconds=timeout)
