"""C11a RED-first route, write, capability and Core-down assertions."""

import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.api.c11a import C05Committed, ConsumerGateway, GatewayError, capability_state, load_routes


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.core = True
        self.permission = True
        self.module = {"state": "ready", "model_id": "fixture-model", "generation": "fixture-1",
                       "freshness": {"complete": True, "applied_cursor": "committed-feed:1"}}
        async def core_probe():
            return self.core
        async def principal(_request):
            return {"principal_id": "fixture-principal", "project_id": "fixture-project"}
        async def recheck(_principal, _capability):
            self.calls.append("recheck")
            return self.permission
        async def source(_name, principal):
            self.calls.append("source:" + principal["project_id"])
            return self.module
        async def health():
            return {"component": "cortex", "status": "ok" if self.core else "unavailable"}
        async def search(_principal, _request, _key):
            self.calls.append("search")
            return {"hits": [], "complete": True}
        async def memory(_principal, _request, key):
            self.calls.append("memory")
            return C05Committed(key, {"record_id": "fixture", "revision": 1,
                                      "event_id": "fixture-event"}, True)
        self.handlers = {"C01-R022": search, "C01-R102": memory}
        self.arguments = dict(core_probe=core_probe, principal_resolver=principal,
                              permission_recheck=recheck, capability_source=source,
                              health=health, handlers=self.handlers)

    def request(self, method, path, *, headers=None, json_body=None):
        class Response:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self.body = body
            def json(self):
                return json.loads(self.body)
        async def run():
            gateway = ConsumerGateway(**self.arguments)
            body = json.dumps(json_body).encode() if json_body is not None else b""
            scope = {"type": "http", "method": method, "path": path, "query_string": b"",
                     "client": ("127.0.0.1", 12000),
                     "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]}
            messages = []
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            async def send(message):
                messages.append(message)
            await gateway.app(scope, receive, send)
            start = next(x for x in messages if x["type"] == "http.response.start")
            data = b"".join(x.get("body", b"") for x in messages if x["type"] == "http.response.body")
            return Response(start["status"], data)
        return asyncio.run(run())

    def test_released_route_inventory_covers_observed_and_openkai_without_sql_bypass(self):
        routes = load_routes()
        self.assertEqual(len(routes), 88)
        self.assertEqual(next(row for row in routes if row['id'] == 'C11-D1')['path'],
                         '/records/{id}')
        self.assertEqual(sum(row["observed"] for row in routes), 82)
        self.assertEqual(sum(row["openkai_production"] for row in routes), 9)
        self.assertEqual({row["effect"] for row in routes if row["id"] in {"C01-R125", "C01-R126"}},
                         {"retired_sql"})
        self.assertEqual(next(row for row in routes if row["id"] == "C01-R022")["effect"], "read")
        self.assertEqual(next(row for row in routes if row["id"] == "C01-R102")["effect"], "write")

    def test_manifest_route_binding_refuses_method_path_drift(self):
        from cortex_core.api import c11a
        for route_id, field, changed in (("C01-R001", "path", "/silent-drift"),
                                         ("C01-R102", "effect", "read"),
                                         ("C01-R022", "method", "PUT")):
            with self.subTest(route_id=route_id, field=field):
                with tempfile.TemporaryDirectory(prefix="cox-c11a-manifest-") as folder:
                    root = Path(folder)
                    shutil.copy2(c11a.CONTRACTS / "route-matrix.md", root / "route-matrix.md")
                    value = json.loads((c11a.CONTRACTS / "c11a-consumer-routes.json").read_text())
                    next(row for row in value["routes"] if row["id"] == route_id)[field] = changed
                    (root / "c11a-consumer-routes.json").write_text(json.dumps(value))
                    with patch.object(c11a, "CONTRACTS", root), self.assertRaises(GatewayError):
                        load_routes()

    def test_core_down_fails_closed_even_for_unbound_or_unknown_nonhealth(self):
        self.core = False
        health = self.request("GET", "/health")
        self.assertEqual(health.status_code, 503)
        self.assertEqual(health.json()["status"], "unavailable")
        for method, path in (("POST", "/search"), ("POST", "/memory"),
                             ("GET", "/metrics"), ("GET", "/unknown")):
            with self.subTest(path=path):
                response = self.request(method, path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["code"], "core_unavailable")

    def test_unlanded_addons_are_unavailable_even_if_source_claims_ready(self):
        async def run():
            async def source(_, _principal):
                return self.module
            async def allowed(*_):
                return True
            return [await capability_state(name, source, allowed, {})
                    for name in ("qdrant", "c10", "valkey", "documents", "duckdb", "local_model")]
        values = asyncio.run(run())
        self.assertTrue(all(x["state"] == "unavailable" and not x["complete"]
                            and x["code"] == "capability_unavailable"
                            and x["reason"] == "not_landed" for x in values))

    def test_pg_ready_requires_model_generation_freshness_and_recheck(self):
        self.module = {"state": "ready", "generation": "fixture-1",
                       "freshness": {"complete": True, "applied_cursor": "committed-feed:1"}}
        response = self.request("POST", "/search")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "capability_unavailable")
        self.assertNotIn("search", self.calls)
        self.module["model_id"] = ""
        self.assertEqual(self.request("POST", "/search").status_code, 503)
        self.module["model_id"] = "fixture-model"
        self.module["state"] = "rebuilding"
        self.assertEqual(self.request("POST", "/search").status_code, 503)
        self.module["state"] = "ready"
        self.module.pop("generation")
        self.assertEqual(self.request("POST", "/search").status_code, 503)
        self.module["generation"] = "fixture-1"
        self.module["freshness"]["complete"] = False
        self.assertEqual(self.request("POST", "/search").status_code, 503)
        self.module["freshness"]["complete"] = True
        self.permission = False
        response = self.request("POST", "/search")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("search", self.calls)
        self.permission = True
        response = self.request("POST", "/search")
        self.assertEqual(response.status_code, 200)
        self.assertIn("source:fixture-project", self.calls)
        self.assertIn("recheck", self.calls)
        self.assertIn("search", self.calls)

    def test_write_waits_for_committed_c05_receipt_and_idempotency_key(self):
        self.assertEqual(self.request("POST", "/memory").status_code, 400)
        response = self.request("POST", "/memory", headers={"Idempotency-Key": "fixture-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["receipt"]["revision"], 1)
        async def uncommitted(_principal, _request, key):
            return C05Committed(key, {"record_id": "fixture", "revision": 1}, False)
        self.handlers["C01-R102"] = uncommitted
        response = self.request("POST", "/memory", headers={"Idempotency-Key": "fixture-key"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "core_unavailable")
        async def wrong_key(_principal, _request, _key):
            return C05Committed("other-key", {"record_id": "fixture", "revision": 1}, True)
        self.handlers["C01-R102"] = wrong_key
        self.assertEqual(self.request("POST", "/memory", headers={"Idempotency-Key": "fixture-key"}).status_code, 503)
        async def rolled_back(_principal, _request, _key):
            raise RuntimeError("synthetic C05 rollback")
        self.handlers["C01-R102"] = rolled_back
        self.assertEqual(self.request("POST", "/memory", headers={"Idempotency-Key": "fixture-key"}).status_code, 503)

    def test_unbound_consumer_route_is_typed_unavailable_not_empty_success(self):
        response = self.request("GET", "/boot/example")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "capability_unavailable")
        response = self.request("POST", "/admin/sql/exec", headers={"Idempotency-Key": "fixture-key"})
        self.assertEqual(response.status_code, 410)


if __name__ == "__main__":
    unittest.main()
