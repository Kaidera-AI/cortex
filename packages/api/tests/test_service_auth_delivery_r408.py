"""Successful-delivery evidence uses actual middleware, raw ASGI and no key/DB."""
import asyncio

from fastapi import FastAPI
import pytest

from service_auth import AuthInvalid
from service_auth_http import ServiceAuthMiddleware
from test_service_auth_http import Exchange, Store


class ObservedStore(Store):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace
        self.observations = []

    async def observe_consumer_use(self, context):
        self.observations.append(context)
        self.trace.append("observed")


async def exchange(mode, status=200):
    trace = []
    store = ObservedStore(trace)
    stream = mode.startswith("sse")
    route = "/events" if stream else "/search"
    router = FastAPI()
    async def placeholder(): return {}
    router.add_api_route(route, placeholder, methods=["GET"])
    wire = Exchange(route)

    async def send(message):
        if mode == "send-start-error" and message["type"] == "http.response.start":
            raise OSError("PUBLIC send failed")
        if mode in {"send-body-error", "sse-send-error"} and message["type"] == "http.response.body":
            raise OSError("PUBLIC send failed")
        await wire.send(message)
        trace.append("start" if message["type"] == "http.response.start" else
                     "body-more" if message.get("more_body") else "body-final")
        if mode == "revoked" and message["type"] == "http.response.start":
            store.failure = AuthInvalid()

    async def app(scope, receive, guarded_send):
        await guarded_send({"type": "http.response.start", "status": status,
            "headers": [(b"content-type", b"text/event-stream" if stream else b"application/json")]})
        if mode == "abort": raise RuntimeError("PUBLIC body aborted")
        if mode == "sse-first":
            await guarded_send({"type": "http.response.body", "body": b"data: PUBLIC event\n\n", "more_body": True})
            assert len(store.observations) == 1, "R408 delivered event needs use evidence before stream completion"
        elif mode == "sse-split":
            await guarded_send({"type": "http.response.body", "body": b"data: PUBLIC", "more_body": True})
            assert store.observations == [], "R408 partial event is not delivered evidence"
            await guarded_send({"type": "http.response.body", "body": b" event\n\n", "more_body": True})
            assert len(store.observations) == 1
        elif mode in {"sse-comment", "sse-send-error"}:
            await guarded_send({"type": "http.response.body", "body": b": PUBLIC keepalive\n\n" if mode == "sse-comment"
                                else b"data: PUBLIC event\n\n", "more_body": True})
        await guarded_send({"type": "http.response.body", "body": b"" if stream else b"{}", "more_body": False})

    adapter = ServiceAuthMiddleware(app, router.router, lambda: store, transport_is_verified=lambda scope: True)
    try:
        await asyncio.wait_for(adapter(wire.scope, wire.receive, send), 2)
    except RuntimeError:
        assert mode == "abort"
    return trace, store, wire


def test_nonstream_observation_is_after_the_successfully_sent_terminal_body():
    trace, store, wire = asyncio.run(exchange("normal"))
    assert wire.status == 200 and wire.body == b"{}"
    assert len(store.observations) == 1
    assert trace.index("observed") > trace.index("body-final")
    assert store.lifecycle_writes == []


@pytest.mark.parametrize("mode", ("sse-first", "sse-split"))
def test_first_complete_authorized_sse_event_is_observed_after_delivery(mode):
    trace, store, wire = asyncio.run(exchange(mode))
    assert wire.status == 200 and len(store.observations) == 1
    assert trace.index("observed") > trace.index("body-more")
    assert store.lifecycle_writes == []


@pytest.mark.parametrize("mode,status", (("send-start-error", 200), ("send-body-error", 200),
    ("abort", 200), ("revoked", 200), ("sse-send-error", 200), ("sse-comment", 200),
    ("normal", 401), ("normal", 500)))
def test_no_success_evidence_on_error_abort_revocation_or_comment_only(mode, status):
    _, store, _ = asyncio.run(exchange(mode, status))
    assert store.observations == [] and store.lifecycle_writes == []
