"""Legacy graph path plugin. C11 must inject an authenticated principal resolver.

X-Project/body/repo are selectors checked against Core; none supplies authority.
Error/prune/storage deltas remain proposed C02 envelopes until its wire gate.
"""
import asyncio
import json

from starlette.responses import JSONResponse
from starlette.routing import Route, Router
from cortex_core.embeddings.pg_search import CoreUnavailable
from .pg_graph import GraphUnavailable, StaleGraph


def boolean(value):
    if isinstance(value, str) and value.lower() in {"true", "false", "1", "0"}:
        return value.lower() in {"true", "1"}
    if type(value) is not bool:
        raise ValueError("Boolean required")
    return value


async def body(request, allowed):
    data = bytearray()
    async with asyncio.timeout(2):
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 16384:
                raise ValueError("Graph request exceeds bounds")
    result = json.loads(data or b"{}")
    if not isinstance(result, dict) or set(result)-set(allowed):
        raise ValueError("Invalid graph request fields")
    return result


def graph_routes(graph, principal_resolver):
    if not callable(principal_resolver):
        raise TypeError("Authenticated principal resolver is mandatory")

    async def endpoint(request):
        try:
            try:
                principal = await principal_resolver(request)
            except PermissionError:
                raise
            except Exception:
                raise GraphUnavailable("principal_resolver_unavailable") from None
            if not isinstance(principal, str) or not principal.strip():
                raise PermissionError("Authenticated principal required")
            project = request.headers.get("X-Project")
            path = request.url.path
            if path == "/cortex-graph/stats":
                result = await graph.stats(principal, project=project)
            elif path == "/graph/stats":
                result = await graph.repository_stats(principal, project=project)
            elif path == "/cortex-graph/memory":
                limit = int(request.query_params.get("limit", 500))
                if not 1 <= limit <= 2000:
                    raise ValueError("Invalid memory limit")
                result = await graph.memory(principal, limit=min(limit, 1000), project=project)
            elif path == "/cortex-graph-search":
                q = request.query_params
                limit = int(q.get("limit", 100))
                if not 1 <= limit <= 500:
                    raise ValueError("Invalid search limit")
                result = await graph.search(principal, q.get("q", ""), limit=limit,
                    expand=boolean(q.get("expand", "false")), high=boolean(q.get("high", "false")),
                    low=boolean(q.get("low", "false")), depth=int(q.get("depth", 1)), project=project)
            elif path == "/cortex-graph-extract":
                options = await body(request, {"project", "source", "limit", "backfill", "dry_run", "reprocess", "use_llm", "model"})
                selector = options.pop("project", project)
                if project is not None and selector != project:
                    raise PermissionError("Conflicting project selectors")
                for key in ("backfill", "dry_run", "reprocess", "use_llm"):
                    if key in options:
                        options[key] = boolean(options[key])
                result = await graph.extract(principal, project=selector, **options)
            elif path == "/graph/prune":
                options = await body(request, {"dry_run", "keep_projects"})
                if "dry_run" in options:
                    options["dry_run"] = boolean(options["dry_run"])
                result = await graph.prune(principal, project=project, **options)
            elif path == "/graph/build":
                options = await body(request, {"repo", "full", "embed", "import_existing", "async_job", "sync"})
                result = await graph.build(principal, options, project=project)
                return JSONResponse(result, status_code=202 if result.get("status") == "queued" else 200)
            else:
                result = await graph.job(principal, request.path_params["job_id"], project=project)
                if result is None:
                    return JSONResponse({"error": {"code": "not_found", "message": "Graph build job not found", "retryable": False}}, status_code=404)
            return JSONResponse(result)
        except PermissionError:
            status, code, message = 403, "forbidden", "Graph access refused"
        except (ValueError, TypeError):
            status, code, message = 400, "invalid_request", "Invalid graph request"
        except StaleGraph:
            status, code, message = 409, "conflict", "Graph source or generation changed"
        except CoreUnavailable:
            status, code, message = 503, "core_unavailable", "Authoritative Core unavailable"
        except (GraphUnavailable, TimeoutError):
            status, code, message = 503, "capability_unavailable", "Graph capability unavailable"
        return JSONResponse({"error": {"code": code, "message": message, "retryable": status == 503},
            "capability": {"module": "graph", "state": "unavailable", "complete": False}}, status_code=status)

    return Router(routes=[Route(path, endpoint, methods=[method]) for method, path in [
        ("GET", "/cortex-graph/stats"), ("GET", "/cortex-graph/memory"),
        ("GET", "/cortex-graph-search"), ("GET", "/graph/stats"),
        ("POST", "/cortex-graph-extract"), ("POST", "/graph/build"),
        ("POST", "/graph/prune"), ("GET", "/graph/build/jobs/{job_id}")]])
