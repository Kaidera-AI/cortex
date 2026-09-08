"""Raw ASGI streaming receipts: cancellation and post-revocation delivery."""
import asyncio
from dataclasses import replace
import importlib.util
from pathlib import Path

from fastapi import FastAPI
import pytest
from starlette.responses import StreamingResponse

from service_auth import AuthInvalid, AuthStoreUnavailable


spec = importlib.util.spec_from_file_location("http_admission_fixtures", Path(__file__).with_name("test_service_auth_http.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
adapter_type = fixture.adapter_type


def streaming_app(generator):
    from service_auth_http import ServiceAuthStreamingResponse
    app = FastAPI()
    async def endpoint():
        return ServiceAuthStreamingResponse(generator(), media_type="text/event-stream")
    app.add_api_route("/events", endpoint)
    return app


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 1)


@pytest.mark.asyncio
async def test_finite_stream_checks_actual_chunks_and_never_acknowledges_rotation(adapter_type):
    closed = asyncio.Event()
    async def generator():
        try:
            yield b"data: one\n\n"
            yield b"data: two\n\n"
        finally:
            closed.set()
    store = fixture.Store()
    app = streaming_app(generator)
    exchange = await fixture.Exchange("/events").run(fixture.wrapped(adapter_type, app, store))
    assert exchange.status == 200 and exchange.body == b"data: one\n\ndata: two\n\n"
    assert len(store.calls) >= 3 and closed.is_set() and store.lifecycle_writes == []
    assert sum(m["type"] == "http.response.start" for m in exchange.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "outage", "expired", "scope", "identity"])
async def test_idle_stream_revalidates_and_cancels_blocked_generator(adapter_type, change):
    blocked, closed = asyncio.Event(), asyncio.Event()
    async def generator():
        try:
            yield b"data: initial\n\n"
            await blocked.wait()
            yield b"data: must-not-escape\n\n"
        finally:
            closed.set()
    store = fixture.Store()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    baseline = set(asyncio.all_tasks())
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, store, recheck_interval=0.01)))
    await wait_until(lambda: b"initial" in exchange.body)
    if change == "outage":
        store.failure = AuthStoreUnavailable("secret-driver-detail")
    elif change in {"revoked", "expired"}:
        store.failure = AuthInvalid("secret-" + change)
    elif change == "scope":
        store.current = replace(store.current, scopes=frozenset({"memory:read"}))
    else:
        store.current = replace(store.current, generation=2)
    await asyncio.wait_for(task, 0.5)
    assert closed.is_set() and b"must-not-escape" not in exchange.body and b"secret" not in exchange.body
    assert sum(m["type"] == "http.response.start" for m in exchange.messages) == 1
    assert exchange.messages[-1].get("more_body", False) is False
    await asyncio.sleep(0)
    assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]


@pytest.mark.asyncio
async def test_revocation_while_chunk_waits_stops_the_new_chunk_before_send(adapter_type):
    release, blocked, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def generator():
        try:
            yield b"data: initial\n\n"
            blocked.set()
            await release.wait()
            yield b"data: late-secret\n\n"
        finally:
            closed.set()
    store = fixture.Store()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, store)))
    await blocked.wait()
    store.failure = AuthInvalid("revoked")
    release.set()
    await asyncio.wait_for(task, 0.5)
    assert closed.is_set() and b"late-secret" not in exchange.body


@pytest.mark.asyncio
@pytest.mark.parametrize("asgi_version", ["2.3", "2.4"])
async def test_disconnect_closes_generator_and_stops_idle_monitor(adapter_type, asgi_version):
    closed = asyncio.Event()
    async def generator():
        try:
            yield b"data: initial\n\n"
            await asyncio.Event().wait()
        finally:
            closed.set()
    store = fixture.Store()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    exchange.scope["asgi"]["spec_version"] = asgi_version
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, store, recheck_interval=0.01)))
    await wait_until(lambda: b"initial" in exchange.body)
    exchange.disconnect()
    await asyncio.wait_for(task, 0.5)
    calls = len(store.calls)
    await asyncio.sleep(0.03)
    assert closed.is_set() and len(store.calls) == calls


@pytest.mark.asyncio
async def test_stream_store_deadline_cancels_hung_revalidation_and_generator(adapter_type):
    blocked, closed, auth_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class HangingStore(fixture.Store):
        hanging = False
        async def authenticate(self, token):
            if self.hanging:
                try:
                    await asyncio.Event().wait()
                finally:
                    auth_cancelled.set()
            return await super().authenticate(token)
    async def generator():
        try:
            yield b"data: initial\n\n"
            await blocked.wait()
        finally:
            closed.set()
    store = HangingStore()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, store,
                                      recheck_interval=0.01, auth_timeout=0.02)))
    await wait_until(lambda: b"initial" in exchange.body)
    store.hanging = True
    await asyncio.wait_for(task, 0.5)
    assert closed.is_set() and auth_cancelled.is_set()
    assert exchange.body == b"data: initial\n\n"


@pytest.mark.asyncio
async def test_asgi_24_failed_send_closes_without_sending_another_response(adapter_type):
    closed = asyncio.Event()
    async def generator():
        try:
            yield b"data: lost\n\n"
            await asyncio.Event().wait()
        finally:
            closed.set()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    exchange.scope["asgi"]["spec_version"] = "2.4"
    sent = []
    async def send(message):
        sent.append(dict(message))
        if message["type"] == "http.response.body":
            raise OSError("synthetic connection is closed")
    await asyncio.wait_for(fixture.wrapped(adapter_type, app, fixture.Store())(
        exchange.scope, exchange.receive, send), 0.5)
    assert closed.is_set()
    assert sum(m["type"] == "http.response.start" for m in sent) == 1
    assert sum(m["type"] == "http.response.body" for m in sent) == 1


@pytest.mark.asyncio
async def test_external_request_cancellation_cleans_up_without_swallowing_it(adapter_type):
    closed = asyncio.Event()
    async def generator():
        try:
            yield b"data: initial\n\n"
            await asyncio.Event().wait()
        finally:
            closed.set()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, fixture.Store(), recheck_interval=0.01)))
    await wait_until(lambda: b"initial" in exchange.body)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_revocation_cancels_send_blocked_before_chunk_commit(adapter_type):
    pending, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def generator():
        try:
            yield b"data: initial\n\n"
            yield b"data: pending-secret\n\n"
            await asyncio.Event().wait()
        finally:
            closed.set()
    app = streaming_app(generator)
    store = fixture.Store()
    exchange = fixture.Exchange("/events")
    async def send(message):
        if b"pending-secret" in message.get("body", b""):
            pending.set()
            await release.wait()
        await exchange.send(message)
    baseline = set(asyncio.all_tasks())
    task = asyncio.create_task(fixture.wrapped(adapter_type, app, store, recheck_interval=0.01)(
        exchange.scope, exchange.receive, send))
    await asyncio.wait_for(pending.wait(), 0.5)
    store.failure = AuthInvalid("secret-revocation")
    await asyncio.wait_for(task, 0.5)
    release.set()
    await asyncio.sleep(0)
    assert closed.is_set()
    assert exchange.body == b"data: initial\n\n"
    assert sum(m["type"] == "http.response.start" for m in exchange.messages) == 1
    assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]


def test_owned_response_rejects_arbitrary_async_iterator():
    from service_auth_http import ServiceAuthStreamingResponse
    class Unowned:
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration
        async def aclose(self):
            raise AssertionError("An arbitrary close method does not grant custody")
    with pytest.raises(TypeError):
        ServiceAuthStreamingResponse(Unowned(), media_type="text/event-stream")


@pytest.mark.asyncio
async def test_plain_starlette_iterator_is_not_claimed_owned_by_admission(adapter_type):
    cancelled = asyncio.Event()
    class Unowned:
        first = True
        close_calls = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.first:
                self.first = False
                return b"data: initial\n\n"
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def aclose(self):
            self.close_calls += 1
    iterator = Unowned()
    app = FastAPI()
    async def endpoint():
        return StreamingResponse(iterator, media_type="text/event-stream")
    app.add_api_route("/events", endpoint)
    exchange = fixture.Exchange("/events")
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, fixture.Store())))
    await wait_until(lambda: b"initial" in exchange.body)
    exchange.disconnect()
    await asyncio.wait_for(task, 0.5)
    assert cancelled.is_set() and iterator.close_calls == 0


@pytest.mark.asyncio
async def test_revoked_owned_generator_cleanup_is_awaited_without_repeat_cancellation(adapter_type):
    release_chunk, cleanup_started, release_cleanup, closed = (asyncio.Event() for _ in range(4))
    async def generator():
        try:
            yield b"data: initial\n\n"
            await release_chunk.wait()
            yield b"data: rejected\n\n"
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            closed.set()
    store = fixture.Store()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, store, recheck_interval=0.01)))
    await wait_until(lambda: b"initial" in exchange.body)
    store.failure = AuthInvalid("revoked")
    release_chunk.set()
    await asyncio.wait_for(cleanup_started.wait(), 0.5)
    await asyncio.sleep(0.03)  # idle monitor must not cancel cleanup a second time.
    exchange.disconnect()
    await asyncio.sleep(0.01)
    assert not task.done() and not closed.is_set()
    release_cleanup.set()
    await asyncio.wait_for(task, 0.5)
    assert closed.is_set() and b"rejected" not in exchange.body


@pytest.mark.asyncio
async def test_any_original_scope_shrink_closes_even_if_route_permission_remains(adapter_type):
    closed = asyncio.Event()
    async def generator():
        try:
            yield b"data: initial\n\n"
            await asyncio.Event().wait()
        finally:
            closed.set()
    store = fixture.Store()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, store, recheck_interval=0.01)))
    await wait_until(lambda: b"initial" in exchange.body)
    store.current = replace(store.current, scopes=store.current.scopes - {"memory:read"})
    assert "coordination:read" in store.current.scopes
    await asyncio.wait_for(task, 0.5)
    assert closed.is_set() and exchange.body == b"data: initial\n\n"


@pytest.mark.asyncio
async def test_auth_denial_terminal_send_has_bounded_cancelled_cleanup(adapter_type):
    closed, terminal_pending, terminal_cancelled = (asyncio.Event() for _ in range(3))
    async def generator():
        try:
            yield b"data: initial\n\n"
            await asyncio.Event().wait()
        finally:
            closed.set()
    # This test targets ASGI terminal-send ownership, not generator qualification.
    app = FastAPI()
    async def endpoint():
        return StreamingResponse(generator(), media_type="text/event-stream")
    app.add_api_route("/events", endpoint)
    store = fixture.Store()
    exchange = fixture.Exchange("/events")
    async def send(message):
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            terminal_pending.set()
            try:
                await asyncio.Event().wait()
            finally:
                terminal_cancelled.set()
        await exchange.send(message)
    adapter = fixture.wrapped(adapter_type, app, store, recheck_interval=0.01, close_timeout=0.02)
    baseline = set(asyncio.all_tasks())
    task = asyncio.create_task(adapter(exchange.scope, exchange.receive, send))
    await wait_until(lambda: b"initial" in exchange.body)
    store.failure = AuthInvalid("revoked")
    await asyncio.wait_for(task, 0.5)
    assert closed.is_set() and terminal_pending.is_set() and terminal_cancelled.is_set()
    assert exchange.body == b"data: initial\n\n"
    assert sum(m["type"] == "http.response.start" for m in exchange.messages) == 1
    assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]


@pytest.mark.asyncio
@pytest.mark.parametrize("asgi_version", ["2.3", "2.4"])
@pytest.mark.parametrize("trigger", ["revocation", "disconnect", "external"])
async def test_idle_generator_awaited_cleanup_survives_cancellation_and_is_joined(adapter_type, asgi_version, trigger):
    cleanup_started, release_cleanup, closed = (asyncio.Event() for _ in range(3))
    async def generator():
        try:
            yield b"data: initial\n\n"
            await asyncio.Event().wait()
            yield b"data: forbidden\n\n"
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            closed.set()
    store = fixture.Store()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    exchange.scope["asgi"]["spec_version"] = asgi_version
    baseline = set(asyncio.all_tasks())
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, store, recheck_interval=0.01)))
    try:
        await wait_until(lambda: b"initial" in exchange.body)
        if trigger == "revocation":
            store.failure = AuthInvalid("revoked")
        elif trigger == "disconnect":
            exchange.disconnect()
        else:
            task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 0.5)
        await asyncio.sleep(0.02)
        assert not task.done() and not closed.is_set()
        if trigger == "external":
            # Native repeated Task.cancel is distinct from AnyIO level cancel.
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
        else:
            exchange.disconnect()
        await asyncio.sleep(0.02)
        assert not task.done() and not closed.is_set()
        release_cleanup.set()
        if trigger == "external":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
        else:
            await asyncio.wait_for(task, 0.5)
        assert closed.is_set() and b"forbidden" not in exchange.body
        assert sum(m["type"] == "http.response.start" for m in exchange.messages) == 1
        assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
    finally:
        # A RED assertion must not leave its controlled producer parked forever.
        release_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("asgi_version", ["2.3", "2.4"])
async def test_manual_send_cancellation_cleanup_survives_repeated_wrapper_cancel(adapter_type, asgi_version):
    release_chunk, cleanup_started, release_cleanup, closed = (asyncio.Event() for _ in range(4))
    async def generator():
        try:
            yield b"data: initial\n\n"
            await release_chunk.wait()
            yield b"data: forbidden\n\n"
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            closed.set()
    store = fixture.Store()
    app = streaming_app(generator)
    exchange = fixture.Exchange("/events")
    exchange.scope["asgi"]["spec_version"] = asgi_version
    baseline = set(asyncio.all_tasks())
    task = asyncio.create_task(exchange.run(fixture.wrapped(adapter_type, app, store)))
    try:
        await wait_until(lambda: b"initial" in exchange.body)
        store.failure = AuthInvalid("revoked")
        release_chunk.set()
        await asyncio.wait_for(cleanup_started.wait(), 0.5)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not closed.is_set()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert closed.is_set() and b"forbidden" not in exchange.body
        assert sum(m["type"] == "http.response.start" for m in exchange.messages) == 1
        assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
    finally:
        release_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
