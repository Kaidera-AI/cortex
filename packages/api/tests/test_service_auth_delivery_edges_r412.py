"""Additional frozen wire-framing and multipart observation controls."""
import asyncio
from fastapi import FastAPI
import pytest
from service_auth_http import ServiceAuthMiddleware
from test_service_auth_http import Exchange, TOKEN
from test_service_auth_delivery_r408 import ObservedStore


@pytest.mark.parametrize("multipart", (False, True))
@pytest.mark.parametrize("event", (True, False))
def test_split_crlf_does_not_invent_a_blank_sse_line(multipart, event):
    async def scenario():
        trace = []
        store = ObservedStore(trace)
        route = "/artifacts/parse-document" if multipart else "/events"
        router = FastAPI()
        async def placeholder(): return {}
        router.add_api_route(route, placeholder, methods=["POST"] if multipart else ["GET"])
        headers = [(b"authorization", ("Bearer " + TOKEN).encode())]
        if multipart: headers.append((b"content-type", b"multipart/form-data; boundary=PUBLIC"))
        wire = Exchange(route, "POST" if multipart else "GET", headers=headers)
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body", "body": b"data: PUBLIC\r", "more_body": True})
            assert store.observations == []
            await send({"type": "http.response.body", "body": b"\n", "more_body": True})
            assert store.observations == [], "split CRLF terminates one line, not an event"
            await send({"type": "http.response.body", "body": b"\r\n" if event else b"", "more_body": False})
        adapter = ServiceAuthMiddleware(app, router.router, lambda: store, transport_is_verified=lambda scope: True)
        await adapter(wire.scope, wire.receive, wire.send)
        assert len(store.observations) == int(event) and store.lifecycle_writes == []
    asyncio.run(scenario())


@pytest.mark.parametrize("multipart", (False, True))
@pytest.mark.parametrize("failure", ("body-send", "abort", "error-status"))
def test_multipart_and_normal_failure_never_records_delivery(multipart, failure):
    async def scenario():
        store = ObservedStore([])
        route = "/artifacts/parse-document" if multipart else "/search"
        router = FastAPI()
        async def placeholder(): return {}
        router.add_api_route(route, placeholder, methods=["POST"] if multipart else ["GET"])
        headers = [(b"authorization", ("Bearer " + TOKEN).encode())]
        if multipart: headers.append((b"content-type", b"multipart/form-data; boundary=PUBLIC"))
        wire = Exchange(route, "POST" if multipart else "GET", headers=headers)
        async def send(message):
            if failure == "body-send" and message["type"] == "http.response.body": raise OSError("PUBLIC send failed")
            await wire.send(message)
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 500 if failure == "error-status" else 200,
                        "headers": [(b"content-type", b"application/json")]})
            if failure == "abort": raise RuntimeError("PUBLIC abort")
            await send({"type": "http.response.body", "body": b"{}", "more_body": False})
        adapter = ServiceAuthMiddleware(app, router.router, lambda: store, transport_is_verified=lambda scope: True)
        try: await adapter(wire.scope, wire.receive, send)
        except (RuntimeError, OSError): assert failure in {"abort", "body-send"}
        assert store.observations == [] and store.lifecycle_writes == []
    asyncio.run(scenario())
