"""Fail-closed Cortex HTTP admission boundary.

The integration supplies a server-owned transport verifier. JSON requests are
validated before dispatch; the five explicit multipart routes are authenticated
and authorized before their bounded bodies stream to FastAPI.
"""

import asyncio
from datetime import datetime, timezone
import json
import math
import re
from types import AsyncGeneratorType
from urllib.parse import parse_qsl

from anyio import CancelScope
from fastapi import HTTPException
from starlette.responses import StreamingResponse
from starlette.routing import Match, Route

from service_auth import AuthContext, AuthForbidden, AuthInvalid, AuthStoreUnavailable
from service_auth_policy import (
    ACTOR_BODY_FIELDS, ACTOR_PATH_FIELDS, PUBLIC_ROUTES,
    require_actor_selector, require_policy, require_project_selector,
)


class _DeliveredUse:
    """Recognize complete delivered events, preserving CRLF across sends."""
    def __init__(self):
        self.status = None
        self.sse = False
        self.line = bytearray()
        self.skip_lf = False
        self.event_size = 0
        self.has_data = False
        self.invalid = False

    def _end_line(self):
        if not self.line:
            delivered = self.has_data and not self.invalid
            self.event_size = 0
            self.has_data = self.invalid = False
            return delivered
        if not self.invalid:
            try:
                line = self.line.decode("utf-8")
            except UnicodeError:
                self.invalid = True
            else:
                if line == "data" or line.startswith("data:"):
                    self.has_data = True
        self.line.clear()
        return False

    def sent(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.sse = any(k.lower() == b"content-type" and v.lower().split(b";")[0].strip() == b"text/event-stream"
                           for k, v in message.get("headers", ()))
            return False
        if message["type"] != "http.response.body" or self.status is None or not 200 <= self.status < 300:
            return False
        if not self.sse:
            return not message.get("more_body", False)
        for char in message.get("body", b""):
            if self.skip_lf:
                self.skip_lf = False
                if char == 10:
                    continue
            if char in (10, 13):
                self.skip_lf = char == 13
                if self._end_line():
                    return True
            else:
                self.event_size += 1
                if self.event_size > 64 * 1024:
                    self.invalid = True
                # Keep a nonempty sentinel until the line ends while discarded.
                if not self.invalid or not self.line:
                    self.line.append(char)
        return False


_PROJECT_FIELDS = frozenset({
    "project", "project_key", "project_id", "source_project", "target_project",
    "source_project_key", "target_project_key", "parent_project_key",
})
_CREDENTIAL_FIELDS = frozenset({"token", "access_token", "authorization", "api_key", "bearer"})
_BEARER = re.compile(r"Bearer ([^\s,]+)", re.IGNORECASE)
_DETAILS = {400: "Invalid request", 401: "Valid bearer credential required",
            403: "Credential does not permit this operation", 408: "Request body timed out",
            413: "Request body is too large", 415: "Unsupported request representation",
            503: "Authentication is unavailable"}

# Request-body ceilings include bounded multipart framing overhead. The endpoint
# handlers retain their stricter per-file limits.
MULTIPART_LIMITS = {
    ("POST", "/artifacts/transcribe"): 201 * 1024 * 1024,
    ("POST", "/artifacts/describe-image"): 65 * 1024 * 1024,
    ("POST", "/artifacts/parse-document"): 26 * 1024 * 1024,
    ("POST", "/media/transcribe"): 201 * 1024 * 1024,
    ("POST", "/media/describe-image"): 65 * 1024 * 1024,
}
_MULTIPART_BOUNDARY = re.compile(rb"[0-9A-Za-z'()+_,./:=?-]{1,70}")


class _Denied(Exception):
    def __init__(self, status):
        self.status = status


class _Disconnected(Exception):
    pass


def _limit(value, ceiling):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= ceiling:
        raise ValueError("Admission limits must be positive, finite and within their ceiling")
    return value


def _headers(scope):
    values = {}
    for key, value in scope.get("headers", ()):
        key = key.lower()
        values.setdefault(key, []).append(value)
    if b"x-cortex-admin-token" in values or len(values.get(b"authorization", ())) > 1:
        raise _Denied(401)
    if any(len(values.get(key, ())) > 1 for key in (b"x-project", b"x-agent-name")):
        raise _Denied(403)
    framing = (b"content-type", b"content-length", b"transfer-encoding", b"content-encoding")
    if any(len(values.get(key, ())) > 1 for key in framing):
        raise _Denied(400)
    headers = {key: val[0] for key, val in values.items()}
    length = headers.get(b"content-length")
    if length is not None and (not re.fullmatch(rb"[0-9]+", length) or b"transfer-encoding" in headers):
        raise _Denied(400)
    if b"transfer-encoding" in headers and headers[b"transfer-encoding"].lower() != b"chunked":
        raise _Denied(400)
    if b"content-encoding" in headers and headers[b"content-encoding"].lower() != b"identity":
        raise _Denied(415)
    return headers


def _representation(headers):
    value = headers.get(b"content-type")
    if value is None:
        return "empty"
    parts = [part.strip() for part in value.split(b";")]
    media_type = parts[0].lower()
    if media_type == b"application/json":
        if any(part.lower() not in {b"charset=utf-8", b'charset="utf-8"'} for part in parts[1:]):
            raise _Denied(415)
        return "json"
    if media_type == b"multipart/form-data":
        if len(parts) != 2:
            raise _Denied(415)
        name, separator, boundary = parts[1].partition(b"=")
        if separator != b"=" or name.strip().lower() != b"boundary":
            raise _Denied(415)
        boundary = boundary.strip()
        if len(boundary) >= 2 and boundary[:1] == boundary[-1:] == b'"':
            boundary = boundary[1:-1]
        if not _MULTIPART_BOUNDARY.fullmatch(boundary):
            raise _Denied(415)
        return "multipart"
    raise _Denied(415)


def _query(scope):
    try:
        pairs = parse_qsl(scope.get("query_string", b"").decode("ascii"),
                          keep_blank_values=True, encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError):
        raise _Denied(400) from None
    result = {}
    for key, value in pairs:
        if key.lower() in _CREDENTIAL_FIELDS:
            raise _Denied(401)
        if key in _PROJECT_FIELDS | {"hall"} and key in result:
            raise _Denied(403)
        result[key] = value
    return result


def _route(router, scope):
    # Follow the actual router's first FULL match. Never skip a denied match or
    # look through a Mount for a later allowed-looking path.
    for route in router.routes:
        match, child = route.matches(scope)
        if match is Match.FULL:
            if not isinstance(route, Route):
                raise _Denied(403)
            return route.path, child.get("path_params", {})
    raise _Denied(403)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError("Nonfinite JSON constant")


def _selectors(method, template, context, headers, path, query, body):
    try:
        required = require_policy(method, template, context)
        cross_project_admin = (
            required == frozenset({"instance:admin"})
            and "instance:admin" in context.scopes
        )
        for mapping in (path, query, body):
            if not cross_project_admin:
                for key in _PROJECT_FIELDS & mapping.keys():
                    require_project_selector(mapping[key], context)
            if "hall" in mapping and mapping["hall"] not in ("project", "local"):
                raise _Denied(403)
        if b"x-project" in headers and not cross_project_admin:
            require_project_selector(headers[b"x-project"].decode("utf-8"), context)
        if b"x-agent-name" in headers:
            require_actor_selector(headers[b"x-agent-name"].decode("utf-8"), context)
        for key in ACTOR_PATH_FIELDS.get((method, template), ()):
            if key in path:
                require_actor_selector(path[key], context)
        for key in ACTOR_BODY_FIELDS.get((method, template), ()):
            if key in body:
                require_actor_selector(body[key], context)
    except (HTTPException, UnicodeError):
        raise _Denied(403) from None


async def _error(send, status):
    body = json.dumps({"detail": _DETAILS[status]}, separators=(",", ":")).encode()
    headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
               (b"content-length", str(len(body)).encode())]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class ServiceAuthStreamingResponse(StreamingResponse):
    """Explicit custody of one native async generator; required for strict SSE.

    Raw ASGI admission cannot discover/close arbitrary downstream iterators.
    Main must deliberately select this cooperating response before activation.
    """

    def __init__(self, content, **kwargs):
        if not isinstance(content, AsyncGeneratorType):
            raise TypeError("Strict streaming requires an owned native async generator")
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Active producers have been joined by stream_response. This also
            # settles custody if Starlette is cancelled before streaming starts.
            await self.body_iterator.aclose()

    async def stream_response(self, send):
        closing = False

        async def produce():
            nonlocal closing
            try:
                await super(ServiceAuthStreamingResponse, self).stream_response(send)
            finally:
                closing = True
                await self.body_iterator.aclose()

        # Native task custody isolates a generator's awaited finally from the
        # repeated level cancellation of Starlette's ASGI 2.3 AnyIO task scope.
        producer = asyncio.create_task(produce())
        try:
            await asyncio.shield(producer)
        except asyncio.CancelledError:
            # A cancelled __anext__ may already be in the generator's finally;
            # a manual send cancellation may instead have entered aclose with
            # cancelling()==0. Neither cleanup may receive another cancellation.
            if not producer.done() and not producer.cancelling() and not closing:
                producer.cancel()
            with CancelScope(shield=True):
                while not producer.done():
                    try:
                        await asyncio.shield(producer)
                    except asyncio.CancelledError:
                        # Explicit Task.cancel still reaches this wrapper even
                        # inside an AnyIO shield. Keep joining, never detach it.
                        pass
            if not producer.cancelled():
                producer.result()  # Preserve unexpected production/close errors.
            raise


class ServiceAuthMiddleware:
    """ASGI admission adapter with bounded JSON and multipart protocols."""

    def __init__(self, app, router, store_provider, *, transport_is_verified=None,
                 max_body_bytes=16 * 1024 * 1024, body_timeout=5,
                 multipart_body_timeout=300, auth_timeout=1,
                 recheck_interval=1, close_timeout=1):
        self.app = app
        self.router = router
        self.store_provider = store_provider
        self.transport_is_verified = transport_is_verified
        self.max_body_bytes = _limit(max_body_bytes, 16 * 1024 * 1024)
        self.body_timeout = _limit(body_timeout, 5)
        self.multipart_body_timeout = _limit(multipart_body_timeout, 300)
        self.auth_timeout = _limit(auth_timeout, 1)
        self.recheck_interval = _limit(recheck_interval, 1)
        self.close_timeout = _limit(close_timeout, 1)

    async def _authenticate(self, token):
        try:
            store = self.store_provider()
            async with asyncio.timeout(self.auth_timeout):
                context = await store.authenticate(token)
            if not isinstance(context, AuthContext):
                raise AuthStoreUnavailable()
            if context.expires_at <= datetime.now(timezone.utc):
                raise AuthInvalid()
            return context
        except AuthInvalid:
            raise _Denied(401) from None
        except AuthForbidden:
            raise _Denied(403) from None
        except Exception:
            # Do not expose provider/driver diagnostics or fall back to legacy.
            raise _Denied(503) from None

    async def _observe_consumer_use(self, context):
        if context is None:
            return
        try:
            store = self.store_provider()
            async with asyncio.timeout(self.auth_timeout):
                await store.observe_consumer_use(context)
        except Exception:
            # Authentication stays usable; owner acknowledgment remains closed
            # until a later verified successful response persists this evidence.
            return

    async def _body(self, receive, headers):
        chunks, size = [], 0
        try:
            async with asyncio.timeout(self.body_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        raise _Disconnected()
                    if message["type"] != "http.request":
                        raise _Denied(400)
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.max_body_bytes:
                        raise _Denied(413)
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            raise _Denied(408) from None
        if b"content-length" in headers:
            try:
                matches = int(headers[b"content-length"]) == size
            except ValueError:
                matches = False
            if not matches:
                raise _Denied(400)
        raw = b"".join(chunks)
        if not raw:
            return raw, {}
        try:
            parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs,
                                parse_constant=_nonfinite)
            if not isinstance(parsed, dict):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            raise _Denied(400) from None
        return raw, parsed

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        multipart_limit = None
        try:
            headers, query = _headers(scope), _query(scope)
            representation = _representation(headers)
            template, path = _route(self.router, scope)
            method = scope["method"]
            public = (method, template) in PUBLIC_ROUTES
            token, context = None, None
            if b"authorization" in headers or not public:
                try:
                    match = _BEARER.fullmatch(headers.get(b"authorization", b"").decode("ascii"))
                except UnicodeError:
                    match = None
                if not match:
                    raise _Denied(401)
                try:
                    verified = self.transport_is_verified is not None and self.transport_is_verified(scope) is True
                except Exception:
                    verified = False
                if not verified:
                    raise _Denied(503)
                token = match[1]
                context = await self._authenticate(token)
            multipart_limit = MULTIPART_LIMITS.get((method, template))
            if multipart_limit is not None:
                if representation != "multipart":
                    raise _Denied(415)
                declared = headers.get(b"content-length")
                if declared is not None and int(declared) > multipart_limit:
                    raise _Denied(413)
                raw, body = None, {}
                _selectors(method, template, context, headers, path, query, body)
            else:
                if representation == "multipart":
                    raise _Denied(415)
                raw, body = await self._body(receive, headers)
                _selectors(method, template, context, headers, path, query, body)
            admitted = dict(scope)
            admitted["state"] = dict(scope.get("state", {}))
            admitted["state"].pop("jwt_claims", None)
            admitted["headers"] = [(k.lower(), v) for k, v in scope.get("headers", ())
                                   if k.lower() not in {b"authorization", b"x-cortex-admin-token", b"x-project", b"x-agent-name"}]
            if context is not None:
                admitted["state"]["service_auth"] = context
                admitted["headers"].extend([(b"x-project", context.project_key.encode()),
                                            (b"x-agent-name", context.agent.encode())])
        except _Disconnected:
            return
        except _Denied as exc:
            await _error(send, exc.status)
            return

        async def recheck():
            current = await self._authenticate(token)
            # Exact original authority identity; shrinking scopes is caught by
            # the policy below. Never rebind an open stream to a new generation.
            identity = ("principal_id", "project_id", "project_key", "agent", "agent_id",
                        "actor_id", "installation_id", "token_id", "generation")
            if any(getattr(current, key) != getattr(context, key) for key in identity):
                raise _Denied(403)
            if not context.scopes <= current.scopes:
                raise _Denied(403)
            _selectors(method, template, current, headers, path, query, body)

        authority_check = recheck if context is not None else None
        if multipart_limit is not None:
            await self._dispatch_multipart(
                admitted, receive, send, headers, multipart_limit, authority_check, context
            )
        else:
            await self._dispatch(admitted, receive, send, raw, authority_check, context)

    async def _dispatch_multipart(
        self, scope, receive, send, headers, limit, recheck, context
    ):
        started = complete = disconnected = observed = False
        denial_status = None
        status_code = None
        delivery = _DeliveredUse()
        received = 0
        request_finished = False
        request_done = asyncio.Event()
        app_task = None
        deadline = asyncio.get_running_loop().time() + self.multipart_body_timeout
        declared = int(headers[b"content-length"]) if b"content-length" in headers else None

        async def limited_receive():
            nonlocal received, request_finished, disconnected, denial_status
            if request_finished:
                message = await receive()
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    denial_status = 408
                    raise _Denied(408)
                try:
                    async with asyncio.timeout(remaining):
                        message = await receive()
                except TimeoutError:
                    denial_status = 408
                    raise _Denied(408) from None
            if message["type"] == "http.disconnect":
                disconnected = True
                raise _Disconnected()
            if message["type"] != "http.request":
                denial_status = 400
                raise _Denied(400)
            received += len(message.get("body", b""))
            if received > limit:
                denial_status = 413
                raise _Denied(413)
            if not message.get("more_body", False):
                if declared is not None and declared != received:
                    denial_status = 400
                    raise _Denied(400)
                request_finished = True
                request_done.set()
            return message

        async def guarded_send(message):
            nonlocal started, complete, denial_status, status_code, observed
            if denial_status is not None or disconnected:
                return
            if message["type"] in {"http.response.start", "http.response.body"} and recheck is not None:
                try:
                    await recheck()
                except _Denied as exc:
                    denial_status = exc.status
                    raise
            await send(message)
            if message["type"] == "http.response.start":
                started = True
                status_code = message["status"]
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True
            if delivery.sent(message) and not observed and context is not None:
                if recheck is not None:
                    await recheck()
                await self._observe_consumer_use(context)
                observed = True

        async def watch_disconnect():
            nonlocal disconnected
            # Never compete with the downstream multipart parser for request
            # bytes. The watcher starts only after it consumed the final chunk.
            await request_done.wait()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    if app_task is not None and not app_task.done():
                        app_task.cancel()
                    return

        app_task = asyncio.create_task(self.app(scope, limited_receive, guarded_send))
        watcher = asyncio.create_task(watch_disconnect())
        try:
            await app_task
        except (_Denied, _Disconnected):
            pass
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or not disconnected:
                raise
        finally:
            if not watcher.done():
                watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if denial_status is not None and not started and not disconnected:
            await _error(send, denial_status)
        elif started and not complete and not disconnected:
            try:
                async with asyncio.timeout(self.close_timeout):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            except (OSError, TimeoutError):
                pass

    async def _dispatch(self, scope, receive, send, raw, recheck, context):
        started = complete = disconnected = failed = observed = False
        status_code = None
        delivery = _DeliveredUse()
        stream_ready = asyncio.Event()
        disconnect = asyncio.Event()
        replayed = False
        app_task = None
        stop_requested = False

        def stop_application():
            nonlocal stop_requested
            # A second monitor/disconnect cancellation must not interrupt the
            # response owner's already-running asynchronous generator cleanup.
            if not stop_requested and not app_task.cancelling():
                app_task.cancel()
            stop_requested = True

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def guarded_send(message):
            nonlocal started, complete, disconnected, failed, status_code, observed
            if disconnected or failed:
                raise asyncio.CancelledError()
            if message["type"] == "http.response.start":
                stream = any(k.lower() == b"content-type" and v.lower().split(b";")[0].strip() == b"text/event-stream"
                             for k, v in message.get("headers", ()))
                if stream and recheck:
                    stream_ready.set()
            if message["type"] == "http.response.body" and recheck is not None:
                try:
                    await recheck()
                except _Denied:
                    failed = True
                if failed:
                    stop_application()
                    raise asyncio.CancelledError()
            try:
                await send(message)
            except OSError:
                disconnected = True
                disconnect.set()
                stop_application()
                raise asyncio.CancelledError() from None
            if message["type"] == "http.response.start":
                started = True
                status_code = message["status"]
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True
            if delivery.sent(message) and not observed and context is not None:
                try:
                    await recheck()
                except _Denied:
                    failed = True
                    stop_application()
                    raise asyncio.CancelledError()
                await self._observe_consumer_use(context)
                observed = True

        async def watch_disconnect():
            nonlocal disconnected
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    disconnect.set()
                    stop_application()
                    return

        async def watch_authority():
            nonlocal failed
            await stream_ready.wait()
            while True:
                await asyncio.sleep(self.recheck_interval)
                try:
                    await recheck()
                except _Denied:
                    failed = True
                    stop_application()
                    return

        app_task = asyncio.create_task(self.app(scope, replay, guarded_send))
        watchers = [asyncio.create_task(watch_disconnect())]
        if recheck:
            watchers.append(asyncio.create_task(watch_authority()))
        try:
            await app_task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or not (disconnected or failed):
                raise
        finally:
            for task in [app_task, *watchers]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(app_task, *watchers, return_exceptions=True)
        if failed and started and not complete and not disconnected:
            try:
                async with asyncio.timeout(self.close_timeout):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            except (OSError, TimeoutError):
                pass
