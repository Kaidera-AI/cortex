"""Actual offline ASGI admission and unchanged canonical-handler effects."""
import ast
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import re
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
import pytest
from starlette.responses import JSONResponse

from service_auth import AuthContext, AuthInvalid, AuthStoreUnavailable, SCOPES


API = Path(__file__).resolve().parents[1]
TOKEN = "ctx1_" + "1" * 32 + "." + "a" * 43
OTHER_TOKEN = "ctx1_" + "2" * 32 + "." + "b" * 43
PROJECT_ID = UUID("9b0cfd5c-7208-4a4f-b6ca-c983f354db48")


def context(**changes):
    values = dict(principal_id=UUID(int=1), project_id=PROJECT_ID,
                  project_key="school-notes", agent="homework-bot",
                  installation_id="test-only", token_id=UUID(int=2),
                  scopes=frozenset(SCOPES), expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                  generation=1, agent_id=UUID(int=3), actor_id=UUID(int=4))
    values.update(changes)
    return AuthContext(**values)


class Store:
    def __init__(self):
        self.current = context()
        self.calls = []
        self.lifecycle_writes = []
        self.failure = None

    async def authenticate(self, token):
        self.calls.append(token)
        if self.failure:
            raise self.failure
        if token not in {TOKEN, OTHER_TOKEN}:
            raise AuthInvalid("synthetic-secret-diagnostic")
        if token == OTHER_TOKEN:
            return replace(self.current, principal_id=UUID(int=11), token_id=UUID(int=12),
                           project_id=UUID(int=13), project_key="other-school", agent="other-bot")
        return self.current

    async def observe_consumer_use(self, *args):
        self.lifecycle_writes.append(args)
        raise AssertionError("admission must not acknowledge consumer use")


class Exchange:
    """Raw ASGI messages, including body chunking and explicit disconnect."""
    def __init__(self, path="/search", method="GET", headers=None, body=b"", query=b"", chunks=None):
        self.scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                      "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
                      "root_path": "", "query_string": query,
                      "headers": headers if headers is not None else [(b"authorization", ("Bearer " + TOKEN).encode())],
                      "client": ("127.0.0.1", 10001), "server": ("fixture.invalid", 443), "state": {}}
        self.receives = asyncio.Queue()
        pieces = [body] if chunks is None else chunks
        for index, part in enumerate(pieces):
            self.receives.put_nowait({"type": "http.request", "body": part, "more_body": index < len(pieces) - 1})
        self.messages = []
        self.sent = asyncio.Event()

    async def receive(self):
        return await self.receives.get()

    async def send(self, message):
        self.messages.append(dict(message))
        self.sent.set()

    def disconnect(self):
        self.receives.put_nowait({"type": "http.disconnect"})

    @property
    def status(self):
        starts = [m for m in self.messages if m["type"] == "http.response.start"]
        assert len(starts) == 1
        return starts[0]["status"]

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")

    @property
    def response_headers(self):
        return dict(next(m for m in self.messages if m["type"] == "http.response.start")["headers"])

    async def run(self, adapter):
        await asyncio.wait_for(adapter(self.scope, self.receive, self.send), 2)
        return self


@pytest.fixture
def adapter_type():
    return importlib.import_module("service_auth_http").ServiceAuthMiddleware


def wrapped(adapter_type, app, store, **kwargs):
    kwargs.setdefault("transport_is_verified", lambda scope: True)
    return adapter_type(app, app.router, lambda: store, **kwargs)


def fixture_app():
    app = FastAPI()
    calls = []

    async def endpoint(request: Request, x_project: str = Header(alias="X-Project"),
                       x_agent: str = Header(alias="X-Agent-Name")):
        calls.append((request.url.path, x_project, x_agent))
        return {"project": x_project, "agent": x_agent, "body": (await request.body()).decode(),
                "raw_auth": request.headers.get("authorization"),
                "legacy": request.headers.get("x-cortex-admin-token"),
                "verified_principal": str(request.state.service_auth.principal_id),
                "jwt_claims": getattr(request.state, "jwt_claims", None)}

    for method, path in [("GET", "/search"), ("POST", "/search"), ("POST", "/log"),
                         ("POST", "/sessions/ingest"), ("GET", "/boot/{agent}"),
                         ("GET", "/projects/{project_key}"), ("GET", "/admin/migrations")]:
        app.add_api_route(path, endpoint, methods=[method])
    return app, calls


@pytest.mark.asyncio
async def test_bearer_only_dispatch_derives_identity_and_removes_raw_credentials(adapter_type):
    app, calls = fixture_app()
    store = Store()
    exchange = await Exchange().run(wrapped(adapter_type, app, store))
    assert exchange.status == 200
    result = json.loads(exchange.body)
    assert result["project"] == "school-notes" and result["agent"] == "homework-bot"
    assert result["verified_principal"] == str(store.current.principal_id)
    assert result["raw_auth"] is result["legacy"] is result["jwt_claims"] is None
    assert calls == [("/search", "school-notes", "homework-bot")]
    assert store.lifecycle_writes == []
    assert exchange.scope["state"] == {} and b"x-project" not in dict(exchange.scope["headers"])


def canonical_diary_app():
    tree = ast.parse((API / "main.py").read_text())
    names = {"write_diary", "DiaryWrite", "require_project_scope", "validate_project_key", "_VALID_PROJECT_KEY_RE"}
    selected = []
    for node in tree.body:
        if getattr(node, "name", None) in names or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node.decorator_list = []
            selected.append(node)
    assert len(selected) == len(names)
    effects = []
    acquired = []

    class Conn:
        async def fetchval(self, sql, *args):
            assert "INSERT INTO agent_diaries" in sql
            effects.append(args)
            return 42

    @asynccontextmanager
    async def acquire(project):
        acquired.append(project)
        yield Conn()

    namespace = {"Header": Header, "HTTPException": HTTPException, "BaseModel": BaseModel,
                 "Optional": Optional, "re": re, "json": json, "acquire_scoped": acquire}
    exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                 str(API / "main.py") + ":unchanged-diary", "exec"), namespace)
    app = FastAPI()
    app.add_api_route("/diary/{agent}", namespace["write_diary"], methods=["POST"])
    return app, acquired, effects


@pytest.mark.asyncio
async def test_real_canonical_diary_handler_writes_exactly_one_authorized_row(adapter_type):
    app, acquired, effects = canonical_diary_app()
    body = b'{"summary":"actual canonical handler"}'
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), (b"content-type", b"application/json")]
    exchange = await Exchange("/diary/homework-bot", "POST", headers, body).run(wrapped(adapter_type, app, Store()))
    assert exchange.status == 200 and json.loads(exchange.body) == {"id": "42", "outcome": "completed"}
    assert acquired == ["school-notes"]
    assert len(effects) == 1 and effects[0][:3] == ("school-notes", "homework-bot", "actual canonical handler")


@pytest.mark.asyncio
@pytest.mark.parametrize("path,extra,token", [
    ("/diary/someone-else", [], TOKEN),
    ("/diary/homework-bot", [(b"x-project", b"another-project")], TOKEN),
    ("/diary/homework-bot", [(b"x-agent-name", b"somebody-else")], TOKEN),
    ("/diary/homework-bot", [], "invalid"),
])
async def test_rejection_prevents_real_handler_database_acquisition(adapter_type, path, extra, token):
    app, acquired, effects = canonical_diary_app()
    headers = [(b"authorization", ("Bearer " + token).encode()), (b"content-type", b"application/json"), *extra]
    exchange = await Exchange(path, "POST", headers, b'{"summary":"must not write"}').run(wrapped(adapter_type, app, Store()))
    assert exchange.status in {401, 403}
    assert acquired == effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,status", [
    ([], 401), ([(b"authorization", b"")], 401),
    ([(b"authorization", b"Bearer invalid")], 401),
    ([(b"authorization", b"Basic dXNlcjpwYXNz")], 401),
    ([(b"authorization", b"Bearer x, Bearer y")], 401),
    ([(b"authorization", ("Bearer " + TOKEN).encode()), (b"Authorization", ("Bearer " + TOKEN).encode())], 401),
    ([(b"authorization", ("Bearer " + TOKEN).encode()), (b"x-cortex-admin-token", b"")], 401),
    ([(b"x-cortex-admin-token", b"synthetic-secret-diagnostic")], 401),
    ([(b"authorization", ("Bearer " + TOKEN).encode()), (b"x-project", b"school-notes"), (b"x-project", b"school-notes")], 403),
    ([(b"authorization", ("Bearer " + TOKEN).encode()), (b"x-agent-name", b"homework-bot"), (b"x-agent-name", b"homework-bot")], 403),
])
async def test_credentials_and_duplicate_selectors_never_reach_downstream(adapter_type, headers, status):
    app, calls = fixture_app()
    exchange = await Exchange(headers=headers).run(wrapped(adapter_type, app, Store()))
    assert exchange.status == status and calls == []
    assert b"synthetic-secret-diagnostic" not in exchange.body and TOKEN.encode() not in exchange.body
    assert exchange.response_headers[b"cache-control"] == b"no-store"
    if status == 401:
        assert exchange.response_headers[b"www-authenticate"].startswith(b"Bearer")


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier", [None, lambda scope: False, lambda scope: "yes"])
async def test_no_transport_proof_fails_before_store_and_handler(adapter_type, verifier):
    app, calls = fixture_app()
    store = Store()
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), (b"x-forwarded-proto", b"https"), (b"forwarded", b"proto=https")]
    exchange = await Exchange(headers=headers).run(wrapped(adapter_type, app, store, transport_is_verified=verifier))
    assert exchange.status == 503 and not calls and not store.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", [(AuthInvalid("expired-secret"), 401), (AuthStoreUnavailable("sql-secret"), 503)])
async def test_failed_authentication_is_generic_and_never_falls_back(adapter_type, failure, status):
    app, calls = fixture_app()
    store = Store()
    store.failure = failure
    exchange = await Exchange().run(wrapped(adapter_type, app, store))
    assert exchange.status == status and calls == [] and b"secret" not in exchange.body


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("GET", "/projects"), ("POST", "/admin/sql/query"),
    ("POST", "/admin/sql/exec"), ("POST", "/admin/redis"), ("POST", "/handoffs/cross-project"),
    ("POST", "/project-local-sync"), ("GET", "/new-route"), ("GET", "/admin/new-route"),
    ("GET", "/docs"), ("GET", "/openapi.json"), ("HEAD", "/search"), ("OPTIONS", "/search"),
    ("GET", "/search/")])
async def test_explicit_holds_unknown_routes_and_framework_paths_stay_denied(adapter_type, method, path):
    app, calls = fixture_app()
    async def forbidden():
        calls.append("forbidden")
        return {}
    if path not in {"/docs", "/openapi.json", "/search/"} and method not in {"HEAD", "OPTIONS"}:
        app.add_api_route(path, forbidden, methods=[method])
    exchange = await Exchange(path, method).run(wrapped(adapter_type, app, Store()))
    assert exchange.status == 403 and calls == []


@pytest.mark.asyncio
async def test_scope_denial_happens_before_actual_route_dependency(adapter_type):
    app, calls = fixture_app()
    store = Store()
    store.current = replace(store.current, scopes=frozenset({"runtime:read"}))
    exchange = await Exchange().run(wrapped(adapter_type, app, store))
    assert exchange.status == 403 and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [b"project=other-project", b"project=school-notes&project=school-notes",
    b"project=school-notes&project_id=00000000-0000-0000-0000-000000000000", b"project=", b"project=_global",
    b"hall=all", b"hall=shared", b"access_token=secret", b"token=secret"])
async def test_query_selectors_and_credential_queries_are_checked(adapter_type, query):
    app, calls = fixture_app()
    exchange = await Exchange(query=query).run(wrapped(adapter_type, app, Store()))
    assert exchange.status in {401, 403} and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    (b'{"project":"other-project"}', 403), (b'{"project":null}', 403),
    (b'{"project":"school-notes","project":"school-notes"}', 400),
    (b'{"metadata":{"a":1,"a":2}}', 400), (b'{"x":NaN}', 400),
    (b'[]', 400), (b'not-json', 400), (b'{"x":"\xff"}', 400),
    (b'{"hall":"all"}', 403), (b'{"agent":"someone-else"}', 403),
])
async def test_json_validation_and_actor_checks_precede_handler(adapter_type, body, status):
    app, calls = fixture_app()
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), (b"content-type", b"application/json")]
    exchange = await Exchange("/sessions/ingest", "POST", headers, body).run(wrapped(adapter_type, app, Store()))
    assert exchange.status == status and calls == []


@pytest.mark.asyncio
async def test_chunked_json_is_replayed_byte_exactly_once(adapter_type):
    app, calls = fixture_app()
    body = b'{ "project" : "school-notes", "note" : "literal whitespace" }'
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), (b"content-type", b"application/json")]
    exchange = await Exchange("/search", "POST", headers, chunks=[body[:4], body[4:19], body[19:]]).run(wrapped(adapter_type, app, Store()))
    assert exchange.status == 200 and json.loads(exchange.body)["body"].encode() == body
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,status", [
    ([(b"content-type", b"text/plain")], 415),
    ([(b"content-type", b"multipart/form-data; boundary=abc")], 415),
    ([(b"content-type", b"application/json"), (b"content-encoding", b"gzip")], 415),
    ([(b"content-type", b"application/json"), (b"content-type", b"application/json")], 400),
    ([(b"content-type", b"application/json"), (b"content-length", b"2"), (b"transfer-encoding", b"chunked")], 400),
])
async def test_unsupported_envelopes_never_bypass_inspection(adapter_type, extra, status):
    app, calls = fixture_app()
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), *extra]
    exchange = await Exchange("/search", "POST", headers, b'{}').run(wrapped(adapter_type, app, Store()))
    assert exchange.status == status and calls == []


@pytest.mark.asyncio
async def test_oversize_and_disconnect_prevent_body_effects(adapter_type):
    app, calls = fixture_app()
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), (b"content-type", b"application/json")]
    exchange = await Exchange("/search", "POST", headers, chunks=[b'{"note":', b'"too long"}']).run(wrapped(adapter_type, app, Store(), max_body_bytes=10))
    assert exchange.status == 413 and calls == []
    disconnected = Exchange("/search", "POST", headers, chunks=[])
    disconnected.disconnect()
    await disconnected.run(wrapped(adapter_type, app, Store()))
    assert calls == []


@pytest.mark.asyncio
async def test_target_agent_is_not_asserted_actor_and_path_project_is_checked(adapter_type):
    app, calls = fixture_app()
    adapter = wrapped(adapter_type, app, Store())
    target = await Exchange("/boot/another-bot").run(adapter)
    assert target.status == 200
    denied = await Exchange("/projects/other-project").run(adapter)
    assert denied.status == 403 and len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_requests_keep_separate_identity_and_input_scopes(adapter_type):
    app, calls = fixture_app()
    adapter = wrapped(adapter_type, app, Store())
    one = Exchange()
    two = Exchange(headers=[(b"authorization", ("Bearer " + OTHER_TOKEN).encode())])
    await asyncio.gather(one.run(adapter), two.run(adapter))
    assert {(c[1], c[2]) for c in calls} == {("school-notes", "homework-bot"), ("other-school", "other-bot")}
    assert one.scope["state"] == two.scope["state"] == {}


@pytest.mark.asyncio
async def test_public_liveness_rejects_supplied_bad_or_legacy_credentials(adapter_type):
    app = FastAPI()
    calls = []
    async def live():
        calls.append("live")
        return {"alive": True}
    app.add_api_route("/health/live", live)
    store = Store()
    adapter = wrapped(adapter_type, app, store, transport_is_verified=None)
    public = await Exchange("/health/live", headers=[]).run(adapter)
    assert public.status == 200 and store.calls == []
    for headers in [[(b"authorization", b"Bearer invalid")], [(b"x-cortex-admin-token", b"")]]:
        failed = await Exchange("/health/live", headers=headers).run(wrapped(adapter_type, app, store))
        assert failed.status == 401
    assert calls == ["live"] and store.lifecycle_writes == []


@pytest.mark.asyncio
async def test_business_ownership_denial_is_not_bypassed(adapter_type):
    app = FastAPI()
    async def reject():
        raise HTTPException(403, "Object is not available")
    app.add_api_route("/search", reject)
    exchange = await Exchange().run(wrapped(adapter_type, app, Store()))
    assert exchange.status == 403 and json.loads(exchange.body)["detail"] == "Object is not available"


@pytest.mark.asyncio
async def test_body_receive_deadline_cancels_stalled_sender_before_handler(adapter_type):
    app, calls = fixture_app()
    exchange = Exchange("/search", "POST", [(b"authorization", ("Bearer " + TOKEN).encode()),
                                             (b"content-type", b"application/json")], chunks=[])
    exchange.receives.put_nowait({"type": "http.request", "body": b'{', "more_body": True})
    await asyncio.wait_for(exchange.run(wrapped(adapter_type, app, Store(), body_timeout=0.02)), 0.5)
    assert exchange.status == 408 and calls == []


@pytest.mark.asyncio
async def test_authenticate_deadline_cancels_stalled_store_before_handler(adapter_type):
    app, calls = fixture_app()
    closed = asyncio.Event()
    class StalledStore(Store):
        async def authenticate(self, token):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
    exchange = await Exchange().run(wrapped(adapter_type, app, StalledStore(), auth_timeout=0.02))
    assert exchange.status == 503 and calls == [] and closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier", [None, lambda scope: False])
async def test_credential_bearing_liveness_also_requires_transport_proof(adapter_type, verifier):
    app = FastAPI()
    calls = []
    async def live():
        calls.append("live")
        return {}
    app.add_api_route("/health/live", live)
    store = Store()
    exchange = await Exchange("/health/live").run(wrapped(adapter_type, app, store, transport_is_verified=verifier))
    assert exchange.status == 503 and calls == store.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    [(b"Content-Type", b"application/json"), (b"content-type", b"application/json")],
    [(b"Content-Length", b"2"), (b"content-length", b"2")],
    [(b"Content-Length", b"2"), (b"Transfer-Encoding", b"chunked")],
    [(b"Content-Length", b"0"), (b"Content-Type", b"application/json")],
    [(b"Content-Length", b"99"), (b"Content-Type", b"application/json")],
    [(b"Content-Length", b"2,2"), (b"Content-Type", b"application/json")],
])
async def test_case_insensitive_framing_ambiguity_and_length_mismatch_fail(adapter_type, extra):
    app, calls = fixture_app()
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), *extra]
    exchange = await Exchange("/search", "POST", headers, b'{}').run(wrapped(adapter_type, app, Store()))
    assert exchange.status == 400 and calls == []


@pytest.mark.asyncio
async def test_partial_match_does_not_shadow_later_full_method_match(adapter_type):
    app, calls = fixture_app()  # GET /search precedes POST /search.
    exchange = await Exchange("/search", "POST").run(wrapped(adapter_type, app, Store()))
    assert exchange.status == 200 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow", ["route", "mount"])
async def test_first_full_denied_route_or_mount_is_not_skipped_for_allowed_looking_path(adapter_type, shadow):
    app = FastAPI()
    calls = []
    async def reached():
        calls.append("reached")
        return {}
    if shadow == "route":
        app.add_api_route("/{ignored:path}", reached)
    else:
        child = FastAPI()
        child.add_api_route("/search", reached)
        app.mount("/", child)
    app.add_api_route("/search", reached)
    exchange = await Exchange().run(wrapped(adapter_type, app, Store()))
    assert exchange.status == 403 and calls == []


@pytest.mark.asyncio
async def test_websocket_denied_and_lifespan_only_delegated(adapter_type):
    calls, messages = [], []
    async def downstream(scope, receive, send):
        calls.append(scope["type"])
    adapter = adapter_type(downstream, FastAPI().router, lambda: Store())
    async def receive():
        return {"type": "websocket.connect"}
    async def send(message):
        messages.append(message)
    await adapter({"type": "websocket"}, receive, send)
    assert messages == [{"type": "websocket.close", "code": 1008}] and calls == []
    await adapter({"type": "lifespan"}, receive, send)
    assert calls == ["lifespan"]
