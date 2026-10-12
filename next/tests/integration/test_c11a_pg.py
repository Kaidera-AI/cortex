"""C11a API readiness and permission recheck through a real disposable PG."""

import json
import unittest

import test_pg_search as legacy
from cortex_core.api.c11a import ConsumerGateway


class C11aPGTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = legacy.SearchTests.asyncSetUp
    asyncTearDown = legacy.SearchTests.asyncTearDown
    record = legacy.SearchTests.record

    async def request(self, gateway, path, method="POST"):
        messages = []
        scope = {"type": "http", "method": method, "path": path,
                 "query_string": b"", "headers": [(b"authorization", b"Bearer synthetic-alice")],
                 "client": ("127.0.0.1", 10001)}
        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}
        async def send(message):
            messages.append(message)
        await gateway.app(scope, receive, send)
        status = next(item["status"] for item in messages if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in messages
                        if item["type"] == "http.response.body")
        return status, json.loads(body)

    async def test_real_pg_search_ready_and_revoked_grant_fails_closed(self):
        await self.record("alice", "record-a")
        await self.record("bob", "secret-b")
        async def core_probe():
            return await self.admin.fetchval("SELECT 1") == 1
        async def principal(scope):
            if dict(scope["headers"]).get(b"authorization") != b"Bearer synthetic-alice":
                raise PermissionError()
            return {"principal_id": "alice", "project_id": str(legacy.PROJECT_A)}
        async def grant(context, _capability):
            return await self.admin.fetchval(
                "SELECT 1 FROM public.test_grants WHERE subject=$1 AND project_id=$2",
                context["principal_id"], legacy.PROJECT_A) == 1
        async def state(_capability, context):
            row = await self.admin.fetchrow(
                """SELECT identity,state FROM retrieval.search_state
                WHERE tenant_id=$1 AND project_id=$2""",
                legacy.TENANT_A, legacy.PROJECT_A)
            count = await self.admin.fetchval(
                "SELECT count(*) FROM retrieval.search_vectors WHERE tenant_id=$1 AND project_id=$2",
                legacy.TENANT_A, legacy.PROJECT_A)
            return {"state": row["state"] if row else "unavailable",
                    "model_id": row["identity"] if row else None,
                    "generation": "pg-fixture-1", "freshness":
                    {"complete": count == 1, "applied_cursor": f"pg-indexed:{count}"}}
        search_calls = []
        async def search(context, _scope, _key):
            search_calls.append(context["principal_id"])
            result = await self.store.search(context["principal_id"], legacy.IDENTITY,
                                             legacy.VECTOR, limit=10)
            return {"hits": [hit.record_id for hit in result.hits],
                    "complete": result.freshness.pending_records == 0}
        async def health():
            return {"component": "cortex", "status": "ok"}
        gateway = ConsumerGateway(core_probe=core_probe, principal_resolver=principal,
                                  permission_recheck=grant, capability_source=state,
                                  health=health, handlers={"C01-R022": search})
        status, body = await self.request(gateway, "/search")
        self.assertEqual(status, 200)
        self.assertEqual(body["hits"], ["record-a"])
        self.assertEqual(search_calls, ["alice"])
        await self.admin.execute("DELETE FROM public.test_grants WHERE subject='alice'")
        status, body = await self.request(gateway, "/search")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        self.assertNotIn("hits", body)
        self.assertEqual(search_calls, ["alice"])
