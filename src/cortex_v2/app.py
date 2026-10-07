from __future__ import annotations

import asyncio
import importlib
import logging
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Annotated, Any, get_origin

import asyncpg
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .config import PRODUCTION_INSTANCE, SANDBOX_INSTANCE, Settings, active_profile
from .content import (
    change_status,
    create_content,
    get_content,
    ingest_content,
    revise_content,
    search_content,
)
from .identity import (
    allow_project_create,
    bootstrap_installation,
    create_project,
    reissue_project_creation_keys,
    bind_scope,
    enact_roster,
    enact_writer_policy,
    enroll_principal,
    list_privileged_actions,
    read_roster,
    recover_owner,
    register_connector,
    rename_scope,
    revoke_credential,
    revoke_grant,
    revoke_principal,
    rotate_credential,
    self_profile,
)
from .project_registry import list_projects
from .key_lifetime import due_state
from .models import (
    AllowCreateRequest,
    BootstrapRequest,
    BindScopeRequest,
    ContentSearchRequest,
    ContentStatusRequest,
    CreateContentRequest,
    CreateProjectRequest,
    ReissueProjectKeysRequest,
    CreateMemoryRecord,
    EnactRosterRequest,
    EnactWriterPolicyRequest,
    EnrollPrincipalRequest,
    IngestContentRequest,
    RecoverOwnerRequest,
    RegisterConnectorRequest,
    RenameScopeRequest,
    ReviseContentRequest,
    RevokeCredentialRequest,
    RevokeGrantRequest,
    RevokePrincipalRequest,
    RotateCredentialRequest,
    SearchRequest,
)
from .receipts import token_fingerprint
from .store import (
    ApiProblem,
    Principal,
    authenticate,
    create_record,
    resolve_scopes,
    search_records,
    token_digest,
)

logger = logging.getLogger("cortex_v2.api")
BEARER_PATTERN = re.compile(r"^Bearer ([A-Za-z0-9._~-]{43,128})$")
DATABASE_FAILURES = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    asyncio.TimeoutError,
)


async def _run_enroll(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: EnrollPrincipalRequest,
    key: str,
    settings: Settings,
) -> tuple[int, dict[str, Any], bool]:
    return await enroll_principal(
        connection, principal, settings.token_pepper, payload, key
    )


async def _run_rotate(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: RotateCredentialRequest,
    key: str,
    settings: Settings,
) -> tuple[int, dict[str, Any], bool]:
    return await rotate_credential(
        connection, principal, settings.token_pepper, payload, key
    )


async def _run_revoke_credential(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: RevokeCredentialRequest,
    key: str,
    settings: Settings,
) -> tuple[int, dict[str, Any], bool]:
    return await revoke_credential(connection, principal, payload, key)


async def _run_revoke_principal(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: RevokePrincipalRequest,
    key: str,
    settings: Settings,
) -> tuple[int, dict[str, Any], bool]:
    return await revoke_principal(connection, principal, payload, key)


async def _run_bind_scope(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: BindScopeRequest,
    key: str,
    settings: Settings,
) -> tuple[int, dict[str, Any], bool]:
    return await bind_scope(connection, principal, payload, key)


async def _run_revoke_grant(
    connection: asyncpg.Connection,
    principal: Principal,
    payload: RevokeGrantRequest,
    key: str,
    settings: Settings,
) -> tuple[int, dict[str, Any], bool]:
    return await revoke_grant(connection, principal, payload, key)


def _unwrap_annotation(annotation: Any) -> Any:
    seen = set()
    while getattr(annotation, "__metadata__", None) is not None:
        if id(annotation) in seen:
            break
        seen.add(id(annotation))
        annotation = annotation.__origin__
    return annotation


def _coerce_query_value(value: str, annotation: Any) -> Any:
    base = _unwrap_annotation(annotation)
    if base is bool:
        lowered = value.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        return value
    if base is int:
        try:
            return int(value)
        except ValueError:
            return value
    if base is float:
        try:
            return float(value)
        except ValueError:
            return value
    return value


def query_payload(request: Request, model: Any) -> dict[str, Any]:
    """Map a GET operation's request_model fields onto query parameters."""
    raw: dict[str, Any] = {}
    for field_name, field in model.model_fields.items():
        values = request.query_params.getlist(field_name)
        if not values:
            continue
        base = _unwrap_annotation(field.annotation)
        if get_origin(base) in (list, set, tuple, frozenset):
            raw[field_name] = values
        else:
            raw[field_name] = _coerce_query_value(values[-1], field.annotation)
    return raw


def build_operation_endpoint(operation: dict[str, Any], helpers: SimpleNamespace) -> Any:
    kind = operation["kind"]
    if kind not in ("scoped_write", "scoped_read"):
        raise ValueError(f"unsupported operation kind: {kind}")
    request_model = operation.get("request_model")
    handler = operation["handler"]
    operation_id = operation["operation_id"]
    method = operation["method"].upper()

    async def endpoint(request: Request) -> JSONResponse:
        payload = None
        if request_model is not None:
            if method == "GET":
                raw: Any = query_payload(request, request_model)
            else:
                try:
                    raw = await request.json()
                except ValueError as exc:
                    raise ApiProblem(
                        400, "invalid_request", "The request body must be valid JSON."
                    ) from exc
            try:
                payload = request_model.model_validate(raw)
            except ValidationError as exc:
                return helpers.validation_response(request, exc.errors())
        path_params = dict(request.path_params)
        digest = await helpers.token_hash(
            request, request.headers.get("authorization")
        )
        selected_alias = request.headers.get("x-cortex-scope")
        if not selected_alias:
            raise ApiProblem(400, "scope_required", "Select a primary Cortex scope.")
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await helpers.authenticate(request, connection, digest)
                if kind == "scoped_write":
                    key = helpers.required_idempotency_key(
                        request.headers.get("idempotency-key")
                    )
                    context = await resolve_scopes(
                        connection,
                        principal,
                        selected_alias,
                        [selected_alias],
                        write=True,
                    )
                    status, data, replayed = await handler(
                        connection, context, key, payload, path_params
                    )
                else:
                    read_header = request.headers.get("x-cortex-read-scopes")
                    aliases = [
                        part.strip()
                        for part in (read_header or selected_alias).split(",")
                    ]
                    context = await resolve_scopes(
                        connection, principal, selected_alias, aliases, write=False
                    )
                    data = await handler(connection, context, payload, path_params)
                    status, replayed = 200, False
        if (
            status == 422
            and operation_id.startswith("ingest.")
            and isinstance(data, dict)
            and data.get("state") == "quarantined"
        ):
            run_id = data["run_id"]
            field_path = {
                "ingest.local_state": "capture",
                "ingest.diary": "entries",
                "ingest.save_chat": "messages",
            }.get(operation_id, "transcript")
            fields = [{"path": "run_id", "type": "quarantine_run", "value": run_id}]
            fields.extend(
                {
                    "path": field_path,
                    "type": failure["code"],
                    "line": failure.get("line"),
                }
                for failure in data["failures"]
            )
            return helpers.replay_headers(
                JSONResponse(
                    status_code=status,
                    content={
                        "error": {
                            "code": data["error_code"],
                            "message": (
                                f"Upload quarantined as run {run_id}; "
                                "inspect ingest.run."
                            ),
                            "retryable": False,
                            "fields": fields,
                        },
                    },
                ),
                replayed,
            )
        return helpers.replay_headers(
            JSONResponse(status_code=status, content=helpers.envelope(request, data)),
            replayed,
        )

    endpoint.__name__ = operation_id.replace(".", "_").replace("-", "_")
    endpoint.__doc__ = operation.get("summary")
    return endpoint


def mount_operations(
    application: FastAPI, profile: Any, helpers: SimpleNamespace
) -> list[str]:
    registered = {
        (method, route.path)
        for route in application.routes
        for method in getattr(route, "methods", ()) or ()
    }
    mounted: list[str] = []
    mounted_modules: list[str] = []
    for module_name in profile.operation_modules:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            logger.warning("operation module absent from this build: %s", module_name)
            continue
        for operation in module.OPERATIONS:
            if operation.get("handler") is None:
                # Descriptor-only entry (mounted='app'): describes a route that
                # this module already registers directly; never mount it twice.
                continue
            method = operation["method"].upper()
            if (method, operation["path"]) in registered:
                raise ValueError(
                    "operation route conflicts with an existing route: "
                    f"{method} {operation['path']}"
                )
            registered.add((method, operation["path"]))
            application.add_api_route(
                operation["path"],
                build_operation_endpoint(operation, helpers),
                methods=[method],
                name=operation["operation_id"],
            )
            mounted.append(operation["operation_id"])
        parts = module_name.split(".")
        if len(parts) >= 2 and parts[0] == "cortex_v2":
            mounted_modules.append(parts[1])
    if mounted_modules:
        # Discovery must never advertise a module whose routes failed to
        # mount, so the mount state is published only after success.
        try:
            from .interface import registry as interface_registry
        except ModuleNotFoundError:
            logger.debug("interface registry absent; mount state unpublished")
        else:
            interface_registry.mark_modules_mounted(mounted_modules)
    return mounted


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = Settings.from_env()
    application.state.settings = settings
    application.state.profile = active_profile()
    application.state.pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=0,
        max_size=12,
        command_timeout=10,
        server_settings={"application_name": "cortex-v2-sandbox-api"},
    )
    try:
        yield
    finally:
        await application.state.pool.close()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Cortex v2 sandbox",
        version=__version__,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    due_day: date | None = None
    due_seen: set[bytes] = set()
    due_lock = threading.Lock()

    @application.middleware("http")
    async def attach_request_id(request: Request, call_next: Any):
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        expires_at = getattr(request.state, "credential_expires_at", None)
        if expires_at is not None:
            response.headers["Cortex-Key-Expires"] = expires_at.isoformat()
        return response

    def envelope(request: Request, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "data": data,
            "request_id": request.state.request_id,
            "contract_version": request.app.state.profile.contract_version,
        }

    def problem_response(request: Request, problem: ApiProblem) -> JSONResponse:
        return JSONResponse(
            status_code=problem.status,
            content={
                "error": {
                    "code": problem.code,
                    "message": problem.message,
                    "retryable": problem.retryable,
                },
                "request_id": request.state.request_id,
            },
        )

    @application.exception_handler(ApiProblem)
    async def api_problem_handler(request: Request, exc: ApiProblem) -> JSONResponse:
        return problem_response(request, exc)

    def validation_problem_body(request: Request, errors: list) -> JSONResponse:
        fields = [
            {
                "path": ".".join(str(part) for part in error["loc"]),
                "type": error["type"],
            }
            for error in errors
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request does not match the operation contract.",
                    "retryable": False,
                    "fields": fields,
                },
                "request_id": request.state.request_id,
            },
        )

    @application.exception_handler(RequestValidationError)
    async def validation_problem_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return validation_problem_body(request, exc.errors())

    async def database_problem_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(
            "database request failed request_id=%s failure=%s sqlstate=%s",
            request.state.request_id,
            type(exc).__name__,
            getattr(exc, "sqlstate", None) or "unknown",
        )
        return problem_response(
            request,
            ApiProblem(
                503,
                "storage_unavailable",
                "The request was not committed; retry with the same idempotency key.",
                retryable=True,
            ),
        )

    for failure in DATABASE_FAILURES:
        application.add_exception_handler(failure, database_problem_handler)

    @application.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @application.get("/health/ready", response_model=None)
    async def ready(request: Request) -> JSONResponse | dict[str, str]:
        try:
            async with request.app.state.pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
        except DATABASE_FAILURES:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready", "database": "ready"}

    @application.get("/v1/protocol")
    async def protocol(request: Request) -> dict[str, Any]:
        return envelope(
            request,
            {
                "product_version": __version__,
                "api_version": "v1",
                "deployment_class": request.app.state.profile.deployment_class,
            },
        )

    async def token_hash(request: Request, authorization: str | None) -> bytes:
        match = BEARER_PATTERN.fullmatch(authorization or "")
        if not match:
            raise ApiProblem(
                401, "invalid_credential", "A valid bearer credential is required."
            )
        settings: Settings = request.app.state.settings
        return token_digest(match.group(1), settings.token_pepper)

    def warn_if_key_due(request: Request, principal: Principal, digest: bytes) -> None:
        nonlocal due_day
        expires_at = principal.expires_at
        if expires_at is None:
            return
        now = datetime.now(timezone.utc)
        if due_state(expires_at, now) != "due":
            return
        with due_lock:
            day = now.date()
            if due_day != day:
                due_seen.clear()
                due_day = day
            if digest in due_seen:
                return
            due_seen.add(digest)
            logger.warning(
                "observed_due_key principal_id=%s token_fingerprint=%s expires_at=%s",
                principal.principal_id,
                token_fingerprint(
                    request.headers["authorization"][len("Bearer "):]
                ),
                expires_at.isoformat(),
            )

    async def authenticated_principal(
        request: Request,
        connection: asyncpg.Connection,
        digest: bytes,
    ) -> Principal:
        principal = await authenticate(
            connection, digest,
            legacy_schema=application.state.profile.instance_id == SANDBOX_INSTANCE,
        )
        request.state.credential_expires_at = principal.expires_at
        warn_if_key_due(request, principal, digest)
        return principal

    @application.get("/boot/{agent}")
    async def own_agent_boot(
        request: Request, agent: str,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        agent_label: Annotated[str | None, Header(alias="X-Agent-Name")] = None,
        budget: int = 1200, query: str | None = None, full: bool = False,
    ) -> JSONResponse:
        from .agent_boot import read_boot
        digest = await token_hash(request, authorization)
        if not selected_alias:
            raise ApiProblem(400, "scope_required", "Select a primary Cortex scope.")
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                context = await resolve_scopes(connection, principal, selected_alias, [selected_alias], write=False)
                result = await read_boot(connection, context, agent, budget=budget, query=query, full=full,
                                         agent_label=agent_label)
        return JSONResponse(content=result)

    @application.get("/projects")
    async def legacy_project_reader(request: Request) -> JSONResponse:
        digest = await token_hash(request, request.headers.get("authorization"))
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                projects = await list_projects(connection, principal)
        return JSONResponse(content={"projects": projects})

    @application.post("/v1/memory/records")
    async def record_memory(
        request: Request,
        payload: CreateMemoryRecord,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        digest = await token_hash(request, authorization)
        if not idempotency_key or len(idempotency_key) > 128:
            raise ApiProblem(
                400,
                "idempotency_key_required",
                "Provide an Idempotency-Key of at most 128 characters.",
            )
        if not selected_alias:
            raise ApiProblem(400, "scope_required", "Select a primary Cortex scope.")

        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                context = await resolve_scopes(
                    connection,
                    principal,
                    selected_alias,
                    [selected_alias],
                    write=True,
                )
                status, data, replayed = await create_record(
                    connection,
                    context,
                    idempotency_key,
                    payload.record_type,
                    payload.body,
                )
        response = JSONResponse(status_code=status, content=envelope(request, data))
        if replayed:
            response.headers["Idempotent-Replay"] = "true"
        return response

    @application.get("/v1/memory/records/{record_id}")
    async def inspect_memory(
        request: Request,
        record_id: uuid.UUID,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        read_scope_header: Annotated[
            str | None, Header(alias="X-Cortex-Read-Scopes")
        ] = None,
    ) -> dict[str, Any]:
        digest = await token_hash(request, authorization)
        if not selected_alias:
            raise ApiProblem(400, "scope_required", "Select a primary Cortex scope.")
        aliases = [
            part.strip()
            for part in (read_scope_header or selected_alias).split(",")
        ]
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                await resolve_scopes(
                    connection, principal, selected_alias, aliases, write=False
                )
                row = await connection.fetchrow(
                    """
                    SELECT scope_id, record_id, record_type, revision, body, created_at
                      FROM cortex_core.memory_records
                     WHERE record_id = $1
                     ORDER BY revision DESC
                     LIMIT 1
                    """,
                    record_id,
                )
                if row is None:
                    raise ApiProblem(
                        404, "record_not_found", "The record is unavailable."
                    )
                data = {
                    "record_id": str(row["record_id"]),
                    "record_type": row["record_type"],
                    "body": row["body"],
                    "scope_id": str(row["scope_id"]),
                    "revision": row["revision"],
                    "created_at": row["created_at"].isoformat(),
                }
        return envelope(request, data)

    @application.post("/v1/searches")
    async def search(
        request: Request,
        payload: SearchRequest,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
    ) -> dict[str, Any]:
        digest = await token_hash(request, authorization)
        if not selected_alias:
            raise ApiProblem(400, "scope_required", "Select a primary Cortex scope.")
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                context = await resolve_scopes(
                    connection,
                    principal,
                    selected_alias,
                    payload.read_scopes,
                    write=False,
                )
                hits = await search_records(connection, payload.query, payload.limit)
        return envelope(
            request,
            {
                "hits": hits,
                "read_scopes": [scope.alias for scope in context.read_scopes],
                "stages_completed": ["lexical"],
                "degraded": ["vectors_not_configured", "graphs_not_built"],
                "limit": payload.limit,
            },
        )

    def required_idempotency_key(idempotency_key: str | None) -> str:
        if not idempotency_key or len(idempotency_key) > 128:
            raise ApiProblem(
                400,
                "idempotency_key_required",
                "Provide an Idempotency-Key of at most 128 characters.",
            )
        return idempotency_key

    def replay_headers(response: JSONResponse, replayed: bool) -> JSONResponse:
        if replayed:
            response.headers["Idempotent-Replay"] = "true"
        return response

    async def installation_command(
        request: Request,
        authorization: str | None,
        idempotency_key: str | None,
        payload: Any,
        runner: Any,
    ) -> JSONResponse:
        digest = await token_hash(request, authorization)
        key = required_idempotency_key(idempotency_key)
        settings: Settings = request.app.state.settings
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                status, data, replayed = await runner(
                    connection, principal, payload, key, settings
                )
        return replay_headers(
            JSONResponse(status_code=status, content=envelope(request, data)), replayed
        )

    async def registry_command(
        request: Request,
        authorization: str | None,
        idempotency_key: str | None,
        command: Any,
    ) -> JSONResponse:
        digest = await token_hash(request, authorization)
        key = required_idempotency_key(idempotency_key)
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                status, data, replayed = await command(connection, principal, key)
        return replay_headers(
            JSONResponse(status_code=status, content=envelope(request, data)), replayed
        )

    async def scoped_write_command(
        request: Request,
        authorization: str | None,
        selected_alias: str | None,
        idempotency_key: str | None,
        command: Any,
    ) -> JSONResponse:
        digest = await token_hash(request, authorization)
        key = required_idempotency_key(idempotency_key)
        if not selected_alias:
            raise ApiProblem(400, "scope_required", "Select a primary Cortex scope.")
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                context = await resolve_scopes(
                    connection, principal, selected_alias, [selected_alias], write=True
                )
                status, data, replayed = await command(connection, context, key)
        return replay_headers(
            JSONResponse(status_code=status, content=envelope(request, data)), replayed
        )

    @application.post("/v1/projects/{project}:allow-create")
    async def project_allow_create(
        request: Request,
        project: str,
        payload: AllowCreateRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await registry_command(
            request,
            authorization,
            idempotency_key,
            lambda connection, principal, key: allow_project_create(
                connection, principal, project, payload, key
            ),
        )

    @application.get("/v1/auth/principal")
    async def auth_principal(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        digest = await token_hash(request, authorization)
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                data = await self_profile(connection, principal)
        return envelope(request, data)

    @application.post("/v1/auth/principals:enroll")
    async def auth_enroll(
        request: Request,
        payload: EnrollPrincipalRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await installation_command(
            request, authorization, idempotency_key, payload, _run_enroll
        )

    @application.post("/v1/auth/credentials:rotate")
    async def auth_rotate(
        request: Request,
        payload: RotateCredentialRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await installation_command(
            request, authorization, idempotency_key, payload, _run_rotate
        )

    @application.post("/v1/auth/credentials:revoke")
    async def auth_revoke_credential(
        request: Request,
        payload: RevokeCredentialRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await installation_command(
            request, authorization, idempotency_key, payload, _run_revoke_credential
        )

    @application.post("/v1/auth/principals:revoke")
    async def auth_revoke_principal(
        request: Request,
        payload: RevokePrincipalRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await installation_command(
            request, authorization, idempotency_key, payload, _run_revoke_principal
        )

    @application.post("/v1/auth/scopes:bind")
    async def auth_bind_scope(
        request: Request,
        payload: BindScopeRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await installation_command(
            request, authorization, idempotency_key, payload, _run_bind_scope
        )

    @application.post("/v1/auth/scopes:revoke")
    async def auth_revoke_scope(
        request: Request,
        payload: RevokeGrantRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await installation_command(
            request, authorization, idempotency_key, payload, _run_revoke_grant
        )

    @application.post("/v1/auth/owner:recover")
    async def auth_recover(
        request: Request,
        payload: RecoverOwnerRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        key = required_idempotency_key(idempotency_key)
        settings: Settings = request.app.state.settings
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                status, data, replayed = await recover_owner(
                    connection, settings.token_pepper, payload, key
                )
        return replay_headers(
            JSONResponse(status_code=status, content=envelope(request, data)), replayed
        )

    @application.get("/v1/auth/privileged-actions")
    async def auth_privileged_actions(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        digest = await token_hash(request, authorization)
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                actions = await list_privileged_actions(connection, principal, limit)
        return envelope(request, {"actions": actions, "limit": limit})

    @application.post("/v1/scopes/{alias}:rename")
    async def scope_rename(
        request: Request,
        alias: str,
        payload: RenameScopeRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await registry_command(
            request,
            authorization,
            idempotency_key,
            lambda connection, principal, key: rename_scope(
                connection, principal, alias, payload, key
            ),
        )

    @application.post("/v1/scopes/{alias}/roster")
    async def scope_roster_enact(
        request: Request,
        alias: str,
        payload: EnactRosterRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await registry_command(
            request,
            authorization,
            idempotency_key,
            lambda connection, principal, key: enact_roster(
                connection, principal, alias, payload, key
            ),
        )

    @application.post("/v1/scopes/{alias}/writer-policy")
    async def scope_writer_policy(
        request: Request,
        alias: str,
        payload: EnactWriterPolicyRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await registry_command(
            request,
            authorization,
            idempotency_key,
            lambda connection, principal, key: enact_writer_policy(
                connection, principal, alias, payload, key
            ),
        )

    @application.get("/v1/scopes/{alias}/roster")
    async def scope_roster_read(
        request: Request,
        alias: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        digest = await token_hash(request, authorization)
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                await resolve_scopes(
                    connection, principal, alias, [alias], write=False
                )
                data = await read_roster(connection, principal, alias)
        return envelope(request, data)

    @application.post("/v1/connectors")
    async def connectors_register(
        request: Request,
        payload: RegisterConnectorRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await registry_command(
            request,
            authorization,
            idempotency_key,
            lambda connection, principal, key: register_connector(
                connection, principal, payload, key
            ),
        )

    @application.post("/v1/content")
    async def content_create(
        request: Request,
        payload: CreateContentRequest,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await scoped_write_command(
            request,
            authorization,
            selected_alias,
            idempotency_key,
            lambda connection, context, key: create_content(
                connection, context, payload, key
            ),
        )

    @application.post("/v1/content/{content_id}/revisions")
    async def content_revise(
        request: Request,
        content_id: uuid.UUID,
        payload: ReviseContentRequest,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await scoped_write_command(
            request,
            authorization,
            selected_alias,
            idempotency_key,
            lambda connection, context, key: revise_content(
                connection, context, content_id, payload, key
            ),
        )

    @application.post("/v1/content/ingest")
    async def content_ingest(
        request: Request,
        payload: IngestContentRequest,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
    ) -> JSONResponse:
        digest = await token_hash(request, authorization)
        if not selected_alias:
            raise ApiProblem(400, "scope_required", "Select a primary Cortex scope.")
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                context = await resolve_scopes(
                    connection, principal, selected_alias, [selected_alias], write=True
                )
                status, data, replayed = await ingest_content(
                    connection, context, payload
                )
        return replay_headers(
            JSONResponse(status_code=status, content=envelope(request, data)), replayed
        )

    @application.post("/v1/content/{content_id}:invalidate")
    async def content_invalidate(
        request: Request,
        content_id: uuid.UUID,
        payload: ContentStatusRequest,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await scoped_write_command(
            request,
            authorization,
            selected_alias,
            idempotency_key,
            lambda connection, context, key: change_status(
                connection, context, content_id, "invalidate",
                payload.reason, payload.successor_content_id, key
            ),
        )

    @application.post("/v1/content/{content_id}:restore")
    async def content_restore(
        request: Request,
        content_id: uuid.UUID,
        payload: ContentStatusRequest,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await scoped_write_command(
            request,
            authorization,
            selected_alias,
            idempotency_key,
            lambda connection, context, key: change_status(
                connection, context, content_id, "restore", payload.reason, None, key
            ),
        )

    @application.post("/v1/content/{content_id}:supersede")
    async def content_supersede(
        request: Request,
        content_id: uuid.UUID,
        payload: ContentStatusRequest,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await scoped_write_command(
            request,
            authorization,
            selected_alias,
            idempotency_key,
            lambda connection, context, key: change_status(
                connection, context, content_id, "supersede",
                payload.reason, payload.successor_content_id, key
            ),
        )

    @application.post("/v1/content/{content_id}:tombstone")
    async def content_tombstone(
        request: Request,
        content_id: uuid.UUID,
        payload: ContentStatusRequest,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        return await scoped_write_command(
            request,
            authorization,
            selected_alias,
            idempotency_key,
            lambda connection, context, key: change_status(
                connection, context, content_id, "tombstone", payload.reason, None, key
            ),
        )

    @application.get("/v1/content/{content_id}")
    async def content_inspect(
        request: Request,
        content_id: uuid.UUID,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
        read_scope_header: Annotated[
            str | None, Header(alias="X-Cortex-Read-Scopes")
        ] = None,
        revision: Annotated[int | None, Query(ge=1)] = None,
        history: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
        digest = await token_hash(request, authorization)
        if not selected_alias:
            raise ApiProblem(400, "scope_required", "Select a primary Cortex scope.")
        aliases = [
            part.strip()
            for part in (read_scope_header or selected_alias).split(",")
        ]
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                await resolve_scopes(
                    connection, principal, selected_alias, aliases, write=False
                )
                data = await get_content(connection, content_id, revision, history)
        return envelope(request, data)

    @application.post("/v1/content/searches")
    async def content_search(
        request: Request,
        payload: ContentSearchRequest,
        authorization: Annotated[str | None, Header()] = None,
        selected_alias: Annotated[str | None, Header(alias="X-Cortex-Scope")] = None,
    ) -> dict[str, Any]:
        digest = await token_hash(request, authorization)
        if not selected_alias:
            raise ApiProblem(400, "scope_required", "Select a primary Cortex scope.")
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticated_principal(request, connection, digest)
                context = await resolve_scopes(
                    connection,
                    principal,
                    selected_alias,
                    payload.read_scopes,
                    write=False,
                )
                hits = await search_content(
                    connection,
                    payload.query,
                    payload.limit,
                    payload.include_historical,
                )
        return envelope(
            request,
            {
                "hits": hits,
                "read_scopes": [scope.alias for scope in context.read_scopes],
                "stages_completed": ["lexical"],
                "degraded": ["vectors_not_configured", "graphs_not_built"],
                "include_historical": payload.include_historical,
                "limit": payload.limit,
            },
        )

    try:
        from .interface import registry as interface_registry
    except ModuleNotFoundError:
        interface_registry = None
    if interface_registry is not None:
        # The static W1 routes above are registered by this composition root;
        # the registry no longer assumes that, so mark it explicitly before
        # generic mounting adds the module operations.
        interface_registry.mark_modules_mounted((interface_registry.W1_MODULE,))

    operation_helpers = SimpleNamespace(
        token_hash=token_hash,
        authenticate=authenticated_principal,
        envelope=envelope,
        replay_headers=replay_headers,
        required_idempotency_key=required_idempotency_key,
        validation_response=validation_problem_body,
    )
    application.state.mounted_operations = mount_operations(
        application, active_profile(), operation_helpers
    )

    from .agent_log import mount_log_routes
    mount_log_routes(application, operation_helpers,
                     lambda *args, **kwargs: resolve_scopes(*args, **kwargs))

    return application


def create_bootstrap_app() -> FastAPI:
    """Serve only on a private Unix socket, never on the public API listener."""
    application = FastAPI(
        title="Cortex first-install control socket",
        version=__version__,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def attach_request_id(request: Request, call_next: Any):
        request.state.request_id = str(uuid.uuid4())
        if request.method == "POST" and len(await request.body()) > 65_536:
            return JSONResponse(status_code=413, content={"error": {"code": "request_too_large",
                "message": "The private control request exceeds 65536 bytes.", "retryable": False},
                "request_id": request.state.request_id})
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        expiry = getattr(request.state, "credential_expires_at", None)
        if expiry is not None:
            response.headers["Cortex-Key-Expires"] = expiry.isoformat()
        return response

    @application.exception_handler(ApiProblem)
    async def api_problem_handler(request: Request, exc: ApiProblem) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                },
                "request_id": request.state.request_id,
            },
        )

    @application.exception_handler(RequestValidationError)
    async def validation_problem_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request does not match the operation contract.",
                    "retryable": False,
                    "fields": [
                        {
                            "path": ".".join(str(part) for part in error["loc"]),
                            "type": error["type"],
                        }
                        for error in exc.errors()
                    ],
                },
                "request_id": request.state.request_id,
            },
        )

    async def private_project_command(request: Request, payload: Any,
                                      operation_id: uuid.UUID | None = None) -> JSONResponse:
        if request.app.state.profile.instance_id != PRODUCTION_INSTANCE:
            raise ApiProblem(403, "project_create_not_allowed", "Use the private project helper.")
        match = BEARER_PATTERN.fullmatch(request.headers.get("authorization", ""))
        if match is None:
            raise ApiProblem(401, "invalid_credential", "A valid bearer credential is required.")
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 128:
            raise ApiProblem(400, "idempotency_key_required", "Provide an Idempotency-Key of at most 128 characters.")
        settings: Settings = request.app.state.settings
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await authenticate(connection, token_digest(match.group(1), settings.token_pepper))
                request.state.credential_expires_at = principal.expires_at
                if operation_id is None:
                    status, data, replayed = await create_project(connection, principal, settings.token_pepper, payload, key)
                else:
                    status, data, replayed = await reissue_project_creation_keys(
                        connection, principal, settings.token_pepper, operation_id, payload, key)
        response = JSONResponse(status_code=status, content={"data": data,
            "request_id": request.state.request_id, "contract_version": request.app.state.profile.contract_version})
        response.headers["Cache-Control"] = "no-store"
        if replayed:
            response.headers["Idempotent-Replay"] = "true"
        return response

    @application.post("/v1/projects")
    async def private_create_project(request: Request, payload: CreateProjectRequest) -> JSONResponse:
        return await private_project_command(request, payload)

    @application.post("/v1/projects/{operation_id}:reissue-keys")
    async def private_reissue_project_keys(request: Request, operation_id: uuid.UUID,
                                          payload: ReissueProjectKeysRequest) -> JSONResponse:
        return await private_project_command(request, payload, operation_id)

    async def private_database_failure(request: Request, exc: Exception) -> JSONResponse:
        logger.error("private database request failed request_id=%s failure=%s sqlstate=%s",
                     request.state.request_id, type(exc).__name__, getattr(exc, "sqlstate", None))
        return await api_problem_handler(request, ApiProblem(503, "storage_unavailable",
            "The result is uncertain; retry with the same idempotency key.", retryable=True))

    for failure in DATABASE_FAILURES:
        application.add_exception_handler(failure, private_database_failure)

    @application.post("/v1/auth/bootstrap")
    async def bootstrap(request: Request, payload: BootstrapRequest) -> JSONResponse:
        if request.app.state.profile.instance_id != PRODUCTION_INSTANCE:
            raise ApiProblem(403, "bootstrap_denied", "First install is unavailable.")
        settings: Settings = request.app.state.settings
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                data = await bootstrap_installation(connection, settings, payload)
        return JSONResponse(
            status_code=201,
            content={
                "data": data,
                "request_id": request.state.request_id,
                "contract_version": request.app.state.profile.contract_version,
            },
        )

    return application


app = create_app()
