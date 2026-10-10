"""C11a source-only consumer route and capability boundary.

The accepted handlers are injected ports. An unbound route refuses explicitly;
this source is not an installed API server or a compatibility qualification.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re


class GatewayError(ValueError):
    """A route inventory or committed-write boundary is invalid."""


@dataclass(frozen=True)
class C05Committed:
    """The injected C05 port returns this only after its transaction commits."""
    request_key: str
    receipt: object
    committed: bool
    response: dict | None = None


CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
UNLANDED = frozenset({"qdrant", "c10", "valkey", "documents", "duckdb",
                      "local_model", "graph_engine", "media"})
PG_CAPABILITIES = frozenset({"pg_search", "pg_graph"})
OPENKAI_PRODUCTION = frozenset({"C01-R002", "C01-R006", "C01-R013", "C01-R015",
                                "C01-R022", "C01-R030", "C01-R072", "C01-R082",
                                "C01-R102"})
PG_SEARCH_ROUTES = frozenset({"C01-R021", "C01-R022"})
PG_GRAPH_ROUTES = frozenset({"C01-R023", "C01-R024", "C01-R025", "C01-R026",
                             "C01-R034", "C01-R035", "C01-R036", "C01-R037"})
RETIRED_SQL_ROUTES = frozenset({"C01-R125", "C01-R126"})
D1_ROUTE = {"id": "C11-D1", "method": "GET", "path": "/records/{id}",
            "disposition": "add", "observed": False, "openkai_production": False,
            "effect": "read", "capability": None}


def _matrix_routes(matrix):
    """Read the frozen C01 table rather than trusting manifest route fields."""
    result = {}
    for line in matrix.decode().splitlines():
        if not re.match(r"\| C01-R\d{3} \|", line):
            continue
        parts = [part.strip() for part in line.split("|")[1:-1]]
        if len(parts) != 7 or not parts[2].startswith("`") or not parts[2].endswith("`"):
            raise GatewayError("C01 matrix row is invalid")
        result[parts[0]] = {"method": parts[1], "path": parts[2][1:-1],
                            "disposition": parts[4],
                            "observed": "Not observed" not in parts[5]}
    return result


def load_routes():
    """Bind the observed released route surface to the frozen C01 inventory."""
    manifest = json.loads((CONTRACTS / "c11a-consumer-routes.json").read_text())
    matrix = (CONTRACTS / "route-matrix.md").read_bytes()
    rows = manifest.get("routes")
    if (manifest.get("format") != "c11a-consumer-routes-v1"
            or manifest.get("released_cortex_tag") != "v0.1.002"
            or hashlib.sha256(matrix).hexdigest() != manifest.get("c01_matrix_sha256")
            or not isinstance(rows, list) or len(rows) != 88
            or sum(row.get("observed") is True for row in rows) != 82
            or sum(row.get("openkai_production") is True for row in rows) != 9):
        raise GatewayError("consumer route inventory differs")
    ids, pairs = set(), set()
    matrix_routes = _matrix_routes(matrix)
    for row in rows:
        if row == D1_ROUTE:
            if row["id"] in ids or (row["method"], row["path"]) in pairs:
                raise GatewayError("consumer route entry is invalid")
            ids.add(row["id"])
            pairs.add((row["method"], row["path"]))
            continue
        if (not isinstance(row, dict) or set(row) != {"id", "method", "path", "disposition",
                                                  "observed", "openkai_production", "effect", "capability"}
                or not re.fullmatch(r"C01-R\d{3}", str(row["id"]))
                or row["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                or not isinstance(row["path"], str) or not row["path"].startswith("/")
                or row["effect"] not in {"health", "read", "write", "retired_sql"}
                or row["capability"] not in {None, "pg_search", "pg_graph"}
                or row["id"] in ids or (row["method"], row["path"]) in pairs):
            raise GatewayError("consumer route entry is invalid")
        ids.add(row["id"])
        pairs.add((row["method"], row["path"]))
        route_id = row["id"]
        expected = matrix_routes.get(route_id)
        effect = ("retired_sql" if route_id in RETIRED_SQL_ROUTES else
                  "health" if route_id == "C01-R002" else
                  "read" if row["method"] == "GET" or route_id == "C01-R022" else "write")
        capability = ("pg_search" if route_id in PG_SEARCH_ROUTES else
                      "pg_graph" if route_id in PG_GRAPH_ROUTES else None)
        if (expected is None
                or any(row[key] != value for key, value in expected.items())
                or row["openkai_production"] != (route_id in OPENKAI_PRODUCTION)
                or row["effect"] != effect or row["capability"] != capability):
            raise GatewayError("consumer route differs from frozen source")
    if ("C01-R001" not in ids or "C01-R002" not in ids or "C11-D1" not in ids
            or {row["id"] for row in rows if row["effect"] == "retired_sql"}
            != {"C01-R125", "C01-R126"}):
        raise GatewayError("required guarded route is absent")
    return rows


def _unavailable(name, reason):
    return {"capability": name, "state": "unavailable", "complete": False,
            "code": "capability_unavailable", "reason": reason, "retryable": reason != "not_landed"}


async def capability_state(name, source, recheck, principal):
    """Only PostgreSQL paths can be ready, after an authoritative recheck."""
    if name in UNLANDED:
        return _unavailable(name, "not_landed")
    if name not in PG_CAPABILITIES:
        return _unavailable(name, "unknown_capability")
    try:
        value = await source(name, principal)
    except Exception:
        return _unavailable(name, "status_unavailable")
    if (not isinstance(value, dict) or value.get("state") != "ready"
            or not isinstance(value.get("model_id"), str) or not value["model_id"]
            or not isinstance(value.get("generation"), str) or not value["generation"]
            or not isinstance(value.get("freshness"), dict)
            or value["freshness"].get("complete") is not True
            or not isinstance(value["freshness"].get("applied_cursor"), str)
            or not value["freshness"]["applied_cursor"]):
        return _unavailable(name, "not_ready")
    try:
        allowed = await recheck(principal, name)
    except Exception:
        return _unavailable(name, "permission_recheck_unavailable")
    if allowed is not True:
        return {"capability": name, "state": "forbidden", "complete": False,
                "code": "forbidden", "reason": "permission_denied", "retryable": False}
    return {"capability": name, "state": "ready", "complete": True,
            "model_id": value["model_id"], "generation": value["generation"],
            "freshness": value["freshness"]}


def _compile_path(template):
    parts = []
    for part in template.strip("/").split("/"):
        if part.startswith("{") and part.endswith("}") and re.fullmatch(r"[a-z_]+", part[1:-1]):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(part))
    return re.compile("^/" + "/".join(parts) + "$" if template != "/" else "^/$")


def _packet(code, *, capability=None, reason=None, retryable=True, message=None):
    error = {"code": code, "retryable": retryable}
    if capability is not None:
        error["capability"] = capability
    if reason is not None:
        error["reason"] = reason
    if message is not None:
        error["message"] = message
    return {"error": error}


def _error_status(error):
    code = getattr(error, 'code', None)
    if code == 'unauthenticated':
        return 401, _packet('credential_required', retryable=False,
                            message='Configure a scoped Cortex credential or upgrade this client.')
    if code in {'forbidden', 'scope_mismatch'}:
        return 403, _packet('forbidden', retryable=False)
    if code == 'conflict':
        return 409, _packet('conflict', retryable=False)
    if code == 'invalid_input':
        return 400, _packet('invalid_input', retryable=False)
    if code == 'gone':
        return 410, _packet('gone', retryable=False)
    if code == 'capability_unavailable':
        return 503, _packet('capability_unavailable',
                            capability=getattr(error, 'capability', None),
                            reason=getattr(error, 'reason', None))
    return 503, _packet('core_unavailable')


async def _memory_body(receive, max_bytes=1024 * 1024):
    body = bytearray()
    while True:
        message = await receive()
        if message.get('type') != 'http.request':
            raise GatewayError('request body interrupted')
        body.extend(message.get('body', b''))
        if len(body) > max_bytes:
            raise GatewayError('request body too large')
        if not message.get('more_body', False):
            break
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise GatewayError('duplicate JSON key')
            result[key] = value
        return result
    try:
        value = json.loads(body.decode('utf-8'), object_pairs_hook=unique)
    except (UnicodeError, ValueError) as error:
        raise GatewayError('invalid JSON body') from error
    if not isinstance(value, dict):
        raise GatewayError('JSON object required')
    return value


async def _respond(send, status, body):
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": data})


class ConsumerGateway:
    """Fail-closed ASGI adapter around explicit Core and module ports."""

    def __init__(self, *, core_probe, principal_resolver, permission_recheck,
                 capability_source, health, handlers, record_reader=None,
                 allow_legacy_idempotency=False, parse_search_body=False):
        if (any(not callable(value) for value in (core_probe, principal_resolver,
                                                   permission_recheck, capability_source, health))
                or not isinstance(handlers, dict)
                or any(not callable(value) for value in handlers.values())):
            raise GatewayError("gateway ports are incomplete")
        self.routes = [(row, _compile_path(row["path"])) for row in load_routes()]
        self.core_probe, self.principal_resolver = core_probe, principal_resolver
        self.permission_recheck, self.capability_source = permission_recheck, capability_source
        self.health, self.handlers = health, handlers
        self.record_reader = record_reader
        self.allow_legacy_idempotency = allow_legacy_idempotency
        self.parse_search_body = parse_search_body
        self.app = self._app

    def _d1_refusal(self, error):
        code = getattr(error, 'code', None)
        if code in {'forbidden', 'scope_mismatch'}:
            return 404, _packet('not_found', retryable=False)
        return _error_status(error)

    async def _app(self, scope, receive, send):
        if scope.get("type") != "http":
            return await _respond(send, 404, _packet("not_found", retryable=False))
        method, path = scope.get("method"), scope.get("path")
        if method == "GET" and path == "/health":
            try:
                value = await self.health()
                if not isinstance(value, dict) or value.get("component") != "cortex":
                    raise GatewayError("health receipt is invalid")
                return await _respond(send, 503 if value.get("status") == "unavailable" else 200, value)
            except Exception:
                return await _respond(send, 503, {"component": "cortex", "status": "unavailable",
                                                  "reason": "core_unavailable"})
        try:
            core = await self.core_probe()
        except Exception:
            core = False
        if core is not True:
            return await _respond(send, 503, _packet("core_unavailable"))
        row = next((row for row, pattern in self.routes
                    if method == row["method"] and pattern.fullmatch(path or "")), None)
        if row is None:
            return await _respond(send, 404, _packet("not_found", retryable=False))
        if row is not None and row["effect"] == "retired_sql":
            return await _respond(send, 410, _packet("retired_route", retryable=False))
        try:
            principal = await self.principal_resolver(scope)
        except Exception as error:
            if row['id'] == 'C11-D1':
                status, packet = self._d1_refusal(error)
                return await _respond(send, status, packet)
            if getattr(error, 'code', None) == 'unauthenticated':
                status, packet = _error_status(error)
                return await _respond(send, status, packet)
            if getattr(error, 'code', None) == 'core_unavailable':
                return await _respond(send, 503, _packet('core_unavailable'))
            principal = None
        if not isinstance(principal, dict) or not principal.get("principal_id") or not principal.get("project_id"):
            if row['id'] == 'C11-D1':
                return await _respond(send, 404, _packet('not_found', retryable=False))
            return await _respond(send, 403, _packet("forbidden", retryable=False))
        if row['id'] == 'C11-D1':
            if self.record_reader is None:
                return await _respond(send, 503, _packet('capability_unavailable',
                                                        reason='adapter_unbound'))
            try:
                value = await self.record_reader(principal, scope, path.rsplit('/', 1)[1])
                if value is None:
                    return await _respond(send, 404, _packet('not_found', retryable=False))
                if not isinstance(value, dict):
                    raise GatewayError('record read receipt is invalid')
                return await _respond(send, 200, value)
            except Exception as error:
                status, packet = self._d1_refusal(error)
                return await _respond(send, status, packet)
        if row["capability"]:
            state = await capability_state(row["capability"], self.capability_source,
                                           self.permission_recheck, principal)
            if state["state"] != "ready":
                status = 403 if state["state"] == "forbidden" else 503
                return await _respond(send, status, _packet(state["code"],
                    capability=row["capability"], reason=state["reason"], retryable=state["retryable"]))
        if row["id"] == "C01-R006":
            names = ("pg_search", "pg_graph", *sorted(UNLANDED))
            values = [await capability_state(name, self.capability_source,
                                              self.permission_recheck, principal) for name in names]
            return await _respond(send, 200, {"capabilities": values})
        handler = self.handlers.get(row["id"])
        if handler is None:
            return await _respond(send, 503, _packet("capability_unavailable",
                                  reason="adapter_unbound"))
        request_key = None
        if row["effect"] == "write":
            headers = dict(scope.get("headers") or [])
            raw_key = headers.get(b"idempotency-key")
            try:
                request_key = raw_key.decode("utf-8") if raw_key is not None else None
            except UnicodeError:
                return await _respond(send, 400, _packet('invalid_input', retryable=False))
            if row['id'] == 'C01-R102' and self.allow_legacy_idempotency:
                try:
                    scope['_c11b_body'] = await _memory_body(receive)
                except GatewayError:
                    return await _respond(send, 400, _packet('invalid_input', retryable=False))
                if request_key is None:
                    try:
                        writer = headers.get(b'x-agent-name', b'').decode('utf-8', 'strict')
                    except UnicodeError:
                        return await _respond(send, 400, _packet('invalid_input', retryable=False))
                    canonical = json.dumps([scope['_c11b_body'], writer],
                        sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
                    request_key = 'legacy-memory:' + hashlib.sha256(canonical).hexdigest()
            elif row['id'] == 'C01-R013' and self.allow_legacy_idempotency:
                try:
                    scope['_c11b_body'] = await _memory_body(receive, 8 * 1024 * 1024)
                except GatewayError:
                    return await _respond(send, 400, _packet('invalid_input', retryable=False))
                if request_key is None and self.allow_legacy_idempotency:
                    canonical = json.dumps(scope['_c11b_body'], sort_keys=True,
                        separators=(',', ':'), ensure_ascii=False).encode()
                    request_key = 'legacy-session:' + hashlib.sha256(canonical).hexdigest()
            if (not request_key or not 1 <= len(request_key) <= 256
                    or any(ord(char) < 32 for char in request_key)):
                return await _respond(send, 400, _packet("idempotency_key_required", retryable=False))
        try:
            if row['id'] == 'C01-R022' and self.parse_search_body:
                try:
                    scope['_c11b_body'] = await _memory_body(receive)
                except GatewayError:
                    return await _respond(send, 400, _packet('invalid_input', retryable=False))
            value = await handler(principal, scope, request_key)
            if row["effect"] == "write":
                if (type(value) is not C05Committed or value.committed is not True
                        or value.request_key != request_key or not isinstance(value.receipt, dict)
                        or not value.receipt):
                    raise GatewayError("write did not commit through C05")
                if value.response is not None:
                    if not isinstance(value.response, dict):
                        raise GatewayError('write response is invalid')
                    return await _respond(send, 200, value.response)
                return await _respond(send, 200, {"receipt": value.receipt})
            if not isinstance(value, dict):
                raise GatewayError("read receipt is invalid")
            return await _respond(send, 200, value)
        except PermissionError:
            return await _respond(send, 403, _packet("forbidden", retryable=False))
        except Exception as error:
            status, packet = _error_status(error)
            return await _respond(send, status, packet)
