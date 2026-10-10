"""Existing callback-error contract must distinguish dependency and request timeout."""
import time
import unittest
from uuid import UUID
import httpx
from cortex_core.conductor.metrics import MetricIdentity, Metrics
from cortex_core.gateway.health_metrics import CoreSample, TelemetryGateway

class DependencyTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_health_timeout_is_dependency_unavailable(self):
        async def health():
            raise TimeoutError("synthetic-dependency-secret")
        async def sample():
            return CoreSample(1, 1)
        gateway = TelemetryGateway(Metrics(MetricIdentity(UUID(int=1), "dev", "test")), health, sample, timeout_seconds=2)
        started = time.monotonic()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 1)), base_url="http://fake.invalid") as client:
            response = await client.get("/health")
        elapsed = time.monotonic() - started
        print("dependency_timeout_elapsed=", elapsed)
        self.assertLess(elapsed, gateway.timeout_seconds)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic-dependency-secret", response.text)
        self.assertEqual(response.json()["reason"], "core_unavailable")

    async def test_immediate_sample_timeout_is_sample_unavailable(self):
        async def health():
            return {"core_available": True, "state": "healthy"}
        async def sample():
            raise TimeoutError("synthetic-dependency-secret")
        gateway = TelemetryGateway(Metrics(MetricIdentity(UUID(int=1), "dev", "test")), health, sample, timeout_seconds=2)
        started = time.monotonic()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 1)), base_url="http://fake.invalid") as client:
            response = await client.get("/metrics")
        elapsed = time.monotonic() - started
        print("dependency_timeout_elapsed=", elapsed)
        self.assertLess(elapsed, gateway.timeout_seconds)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic-dependency-secret", response.text)
        self.assertEqual(response.json()["error"]["reason"], "sample_unavailable")
