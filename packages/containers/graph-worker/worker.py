"""cortex-graph-worker — L3 code graph internal API.

Internal-only, no auth (cortex-api is the trusted proxy). Exposes:
  GET  /health                      → {ok: true, graphs_dir: str, projects_seen: int}
  GET  /stats?storage_key=<project>  → one-project graph statistics
  POST /build {repo, registered_repo, storage_key, ...} → build/update graph
  POST /blast {repo, registered_repo, storage_key, files} → impact radius
  POST /callers {repo, registered_repo, storage_key, target} → graph query
  POST /impact {repo, registered_repo, storage_key, base} → review context
  POST /large-fn {repo, registered_repo, storage_key} → largest functions

This is the scaffold (Phase C.2 deliverable). The DuckDB ATTACH stats are
live and use the migrated graph.db files. /build/blast/callers/impact/large-fn
delegate to better-code-review-graph via subprocess until Phase C.2 wires
them in-process.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

GRAPHS_DIR = Path(os.environ.get("CORTEX_GRAPHS_DIR", "/var/lib/cortex/graphs"))
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/projects"))
_configured_host_projects_root = os.environ.get("HOST_PROJECTS_ROOT", "").strip()
HOST_PROJECTS_ROOT = (
    os.path.normpath(_configured_host_projects_root)
    if _configured_host_projects_root
    else ""
)
_configured_registered_projects_root = os.environ.get(
    "REGISTERED_PROJECTS_ROOT", ""
).strip()
REGISTERED_PROJECTS_ROOT = (
    os.path.normpath(_configured_registered_projects_root)
    if _configured_registered_projects_root
    else ""
)
BCRG_PYTHON = os.environ.get("BCRG_PYTHON", "/opt/bcrg/bin/python")
QWEN_CACHE_DIR = "/opt/kaidera-qwen3/cache"

MAX_REPO_INPUT_CHARS = 4096
MAX_PROJECT_NAME_CHARS = 255
MAX_GRAPH_PROJECTS = 4096
MAX_IMPORT_BYTES = 8 * 1024 * 1024 * 1024
MAX_SUBPROCESS_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
MAX_GRAPH_RESPONSE_BYTES = 512 * 1024
MAX_GRAPH_RESPONSE_ITEMS = 256
MAX_GRAPH_RESPONSE_TEXT_CHARS = 4096
SINGLE_FLIGHT_ADMISSION_SECONDS = 0.05
SUBPROCESS_READ_CHUNK = 64 * 1024

RepoText = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_REPO_INPUT_CHARS)
]
BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
StorageKey = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]+$",
    ),
]

app = FastAPI(title="cortex-graph-worker", version="0.1.0")
_bcrg_slots = asyncio.Semaphore(1)


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject declared and chunked HTTP bodies before FastAPI buffers JSON."""

    def __init__(self, app, *, max_body_bytes: int):
        if max_body_bytes < 0:
            raise ValueError("max_body_bytes must be non-negative")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        content_lengths = [
            value.strip() for key, value in headers if key.lower() == b"content-length"
        ]
        has_transfer_encoding = any(
            key.lower() == b"transfer-encoding" for key, _value in headers
        )
        if len(content_lengths) > 1 or (content_lengths and has_transfer_encoding):
            response = JSONResponse(
                {"detail": "invalid content-length"}, status_code=400
            )
            await response(scope, receive, send)
            return
        if content_lengths:
            declared = content_lengths[0]
            if not declared.isdigit():
                response = JSONResponse(
                    {"detail": "invalid content-length"}, status_code=400
                )
                await response(scope, receive, send)
                return
            normalized = declared.lstrip(b"0") or b"0"
            limit = str(self.max_body_bytes).encode("ascii")
            if len(normalized) > len(limit) or (
                len(normalized) == len(limit) and normalized > limit
            ):
                response = JSONResponse(
                    {"detail": "request body is too large"}, status_code=413
                )
                await response(scope, receive, send)
                return

        received = 0
        request_too_large = False
        response_started = False

        async def limited_receive():
            nonlocal received, request_too_large
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    request_too_large = True
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            # FastAPI can translate a receive failure during JSON parsing into
            # a generic 400. Suppress that response once the byte gate trips so
            # the outer middleware emits exactly one authoritative 413.
            if request_too_large:
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            pass
        if request_too_large:
            if response_started:
                raise _RequestBodyTooLarge
            response = JSONResponse(
                {"detail": "request body is too large"}, status_code=413
            )
            await response(scope, receive, send)


app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=MAX_REQUEST_BODY_BYTES)


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ScopedRepoBody(RequestBody):
    repo: RepoText
    registered_repo: RepoText
    storage_key: StorageKey


class BuildBody(ScopedRepoBody):
    full: bool = False
    embed: bool = True
    import_existing: bool = False

    @model_validator(mode="after")
    def validate_build_mode(self):
        if self.import_existing and self.full:
            raise ValueError("full and import_existing graph modes are mutually exclusive")
        if self.import_existing and self.embed:
            raise ValueError(
                "import_existing requires embed=false; reviewed imports do not run embeddings"
            )
        return self


class PruneBody(RequestBody):
    active_projects: list[StorageKey] = Field(max_length=4096)
    dry_run: bool = True


class BlastBody(ScopedRepoBody):
    files: list[BoundedText] = Field(min_length=1, max_length=512)
    depth: int = Field(default=2, ge=0, le=16)
    max_results: int = Field(default=100, ge=1, le=10_000)


class CallersBody(ScopedRepoBody):
    target: BoundedText
    pattern: ShortText = "callers_of"
    max_results: int = Field(default=100, ge=1, le=10_000)


class ImpactBody(ScopedRepoBody):
    base: BoundedText = "HEAD~1"
    max_results: int = Field(default=100, ge=1, le=10_000)


class LargeFnBody(ScopedRepoBody):
    min_lines: int = Field(default=200, ge=1, le=10_000_000)
    kind: Optional[ShortText] = None
    limit: int = Field(default=100, ge=1, le=10_000)


# The test harness loads this module through ``exec_module`` without registering
# it in ``sys.modules``.  Give Pydantic the concrete namespace explicitly so
# postponed annotations resolve identically in tests and in Uvicorn imports.
for _request_model in (
    ScopedRepoBody,
    BuildBody,
    PruneBody,
    BlastBody,
    CallersBody,
    ImpactBody,
    LargeFnBody,
):
    _request_model.model_rebuild(_types_namespace=globals())


def _project_graph_db(storage_key: str) -> Path:
    """Per-project graph.db path inside the cortex-graphs volume."""
    return _safe_graph_dir(storage_key) / "graph.db"


def _resolved_graphs_root(*, create: bool = False) -> Path:
    """Return the canonical graph-volume root, optionally creating its leaf."""
    if create:
        try:
            GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                503, f"graphs directory is unavailable: {GRAPHS_DIR}"
            ) from exc
    try:
        root = GRAPHS_DIR.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            503, f"graphs directory is unavailable: {GRAPHS_DIR}"
        ) from exc
    if not root.is_dir():
        raise HTTPException(503, f"graphs directory is not a directory: {GRAPHS_DIR}")
    return root


def _open_graphs_root_fd(*, create: bool = False) -> tuple[Path, int]:
    root = _resolved_graphs_root(create=create)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return root, os.open(root, flags)
    except OSError as exc:
        raise HTTPException(503, f"cannot open graphs directory: {root}") from exc


def _ensure_graph_dir(name: str) -> Path:
    """Create/open one direct, non-symlink graph directory by root descriptor."""
    name = _validate_project_name(name)
    root, root_fd = _open_graphs_root_fd(create=True)
    try:
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise HTTPException(
                409, f"cannot inspect graph project directory: {name!r}"
            ) from exc
        if not stat.S_ISDIR(entry.st_mode):
            raise HTTPException(
                409, f"graph project entry is not a strict directory: {name!r}"
            )
    finally:
        os.close(root_fd)
    return root / name


def _prepare_materialization_graph_db(name: str) -> tuple[Path, tuple[int, int]]:
    """Create/open the exact owned graph DB inode before BCRG may mutate it."""
    name = _validate_project_name(name)
    graph_dir = _ensure_graph_dir(name)
    _root, root_fd = _open_graphs_root_fd()
    graph_fd = db_fd = -1
    created = False
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        graph_fd = os.open(name, directory_flags, dir_fd=root_fd)
        try:
            db_fd = os.open("graph.db", file_flags, dir_fd=graph_fd)
        except FileNotFoundError:
            try:
                db_fd = os.open(
                    "graph.db",
                    file_flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=graph_fd,
                )
                created = True
            except OSError as exc:
                raise HTTPException(
                    409, "cannot create the managed graph database safely"
                ) from exc
        except OSError as exc:
            raise HTTPException(
                409, "managed graph database is not a strict regular file"
            ) from exc

        db_stat = os.fstat(db_fd)
        named_stat = os.stat("graph.db", dir_fd=graph_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(db_stat.st_mode)
            or db_stat.st_nlink != 1
            or db_stat.st_uid != os.geteuid()
            or stat.S_IMODE(db_stat.st_mode) & 0o022
            or (db_stat.st_dev, db_stat.st_ino)
            != (named_stat.st_dev, named_stat.st_ino)
        ):
            raise HTTPException(
                409,
                "managed graph database must be a single-link, owner-controlled regular file",
            )
        if created:
            os.fsync(db_fd)
            os.fsync(graph_fd)
        return graph_dir, (db_stat.st_dev, db_stat.st_ino)
    finally:
        if db_fd >= 0:
            os.close(db_fd)
        if graph_fd >= 0:
            os.close(graph_fd)
        os.close(root_fd)


def _external_relative_candidate(repo: str) -> Path | None:
    """Translate one exact registered path into the authoritative projects mount.

    Example: HOST_PROJECTS_ROOT=/workspace/projects and repo=/workspace/projects/Drive/App
    resolves below PROJECTS_DIR inside a host-bind worker. The appliance instead
    registers /projects/<project> and sets REGISTERED_PROJECTS_ROOT=/projects while
    PROJECTS_DIR=/state/projects. This is string-based on purpose: the registered
    external path need not exist under that spelling inside the worker.
    """
    roots = {
        os.path.normpath(root)
        for root in (HOST_PROJECTS_ROOT, REGISTERED_PROJECTS_ROOT)
        if root
    }
    if not roots or not os.path.isabs(repo):
        return None
    if len(roots) != 1:
        raise HTTPException(503, "conflicting external projects root configuration")
    raw = os.path.normpath(repo)
    if raw != repo:
        return None
    external_root = roots.pop()
    if raw == external_root:
        return PROJECTS_DIR
    try:
        if os.path.commonpath((external_root, raw)) == external_root:
            return PROJECTS_DIR / os.path.relpath(raw, external_root)
    except ValueError:
        return None
    return None


def _validate_project_name(name: str) -> str:
    """Validate the single path component used as a graph-volume key."""
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise HTTPException(400, f"unsafe graph project name: {name!r}")
    try:
        name_bytes = os.fsencode(name)
    except UnicodeEncodeError as exc:
        raise HTTPException(
            400, "graph project name is not filesystem encodable"
        ) from exc
    if len(name_bytes) > MAX_PROJECT_NAME_CHARS:
        raise HTTPException(
            400, "graph project name exceeds the filesystem component limit"
        )
    return name


def _resolved_projects_root() -> Path:
    """Return the real projects root while preserving its outer mount alias."""
    try:
        root = PROJECTS_DIR.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            503, f"projects directory is unavailable: {PROJECTS_DIR}"
        ) from exc
    if not root.is_dir():
        raise HTTPException(
            503, f"projects directory is not a directory: {PROJECTS_DIR}"
        )
    return root


def _safe_repo_candidate(candidate: Path, resolved_root: Path) -> Path | None:
    """Resolve one strict child without accepting aliases inside the mount.

    ``PROJECTS_DIR`` itself may be an outer bind-mount alias.  Comparing the path
    relative to that lexical root with the path relative to its resolved root keeps
    that one mapping, while rejecting an inner symlink to a sibling or outside tree.
    """
    try:
        lexical_relative = candidate.relative_to(PROJECTS_DIR)
    except ValueError:
        return None
    if not lexical_relative.parts or ".." in lexical_relative.parts:
        return None

    try:
        resolved = candidate.resolve(strict=True)
        resolved_relative = resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None

    if not resolved_relative.parts or lexical_relative != resolved_relative:
        return None
    return resolved if resolved.is_dir() else None


def _resolve_exact_host_repo(repo: str, *, field_name: str) -> Path:
    """Map an API-proven host path without basename or recursive substitution."""
    if "\x00" in repo:
        raise HTTPException(400, f"{field_name} contains a NUL byte")
    if not os.path.isabs(repo):
        raise HTTPException(400, f"{field_name} must be an absolute host path")
    candidate = _external_relative_candidate(repo)
    if candidate is None or candidate == PROJECTS_DIR:
        raise HTTPException(
            403,
            f"{field_name} is outside the configured registered projects root",
        )
    safe_candidate = _safe_repo_candidate(candidate, _resolved_projects_root())
    if safe_candidate is None:
        if candidate.exists() or candidate.is_symlink():
            raise HTTPException(
                400, f"{field_name} is not a strict project path: {candidate}"
            )
        raise HTTPException(404, f"{field_name} is not mounted at {candidate}")
    return safe_candidate


def _git_common_dir(git_dir: Path) -> Path:
    """Return the canonical Git common directory for a root or linked worktree."""
    common = git_dir
    commondir = git_dir / "commondir"
    if commondir.exists():
        raw_common = _read_small_regular_file(commondir, max_bytes=4096).strip()
        if not raw_common or "\x00" in raw_common:
            raise HTTPException(400, "Git commondir marker is malformed")
        common = Path(raw_common)
        if not common.is_absolute():
            common = git_dir / common
    try:
        common = common.resolve(strict=True)
        common.relative_to(_resolved_projects_root())
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(400, "Git common directory escapes the projects trust root") from exc
    if not common.is_dir():
        raise HTTPException(400, "Git common directory is not a directory")
    return common


def _git_common_dir_identity(repo_path: Path) -> tuple[int, int] | None:
    git_dir = _git_metadata_path(repo_path)
    if git_dir is None:
        return None
    common = _git_common_dir(git_dir)
    common_stat = common.stat()
    return common_stat.st_dev, common_stat.st_ino


def _resolve_repo(repo: str, registered_repo: str) -> Path:
    """Retain an exact root or a worktree proven to share registry Git custody."""
    requested_path = _resolve_exact_host_repo(repo, field_name="repo")
    registered_path = _resolve_exact_host_repo(
        registered_repo, field_name="registered_repo"
    )
    if requested_path != registered_path:
        raise HTTPException(
            403,
            "repo must exactly match the registered project root; register a "
            "worktree as a separate project before graphing it",
        )
    return requested_path


def _tool_preamble(
    storage_key: str,
    repo_root: Path,
    *,
    materialize: bool = False,
    expected_db_identity: tuple[int, int] | None = None,
) -> str:
    """Patch better-code-review-graph storage away from the read-only repo mount."""
    if materialize:
        graph_dir, observed_identity = _prepare_materialization_graph_db(storage_key)
        if (
            expected_db_identity is not None
            and expected_db_identity != observed_identity
        ):
            raise HTTPException(
                409, "managed graph database identity changed before materialization"
            )
        expected_db_identity = observed_identity
    else:
        existing_db = _require_populated_graph_db(storage_key)
        graph_dir = existing_db.parent
        existing_stat = existing_db.lstat()
        expected_db_identity = (existing_stat.st_dev, existing_stat.st_ino)
    git_dir = _git_metadata_path(repo_root)
    if git_dir is not None:
        _validate_git_configuration(git_dir)
    graphs_dir = str(graph_dir.parent)
    storage_key_json = json.dumps(storage_key)
    graphs_json = json.dumps(graphs_dir)
    repo_root_json = json.dumps(str(repo_root))
    git_dir_expr = (
        f"Path({json.dumps(str(git_dir))})" if git_dir is not None else "None"
    )
    qwen_cache_json = json.dumps(QWEN_CACHE_DIR)
    expected_db_identity_expr = repr(expected_db_identity)
    materialize_expr = "True" if materialize else "False"
    return f"""
from pathlib import Path
import os
import stat
import better_code_review_graph.embeddings as embeddings
import better_code_review_graph.tools as tools

_CORTEX_GRAPH_STORAGE_KEY = {storage_key_json}
_CORTEX_GRAPHS_DIR = Path({graphs_json})
_CORTEX_REPO_ROOT = Path({repo_root_json})
_CORTEX_GIT_DIR = {git_dir_expr}
_CORTEX_MATERIALIZE = {materialize_expr}
_CORTEX_EXPECTED_DB_IDENTITY = {expected_db_identity_expr}
_CORTEX_MAX_RESPONSE_BYTES = {MAX_GRAPH_RESPONSE_BYTES - 1}
_CORTEX_MAX_RESPONSE_ITEMS = {MAX_GRAPH_RESPONSE_ITEMS}
_CORTEX_MAX_RESPONSE_TEXT_CHARS = {MAX_GRAPH_RESPONSE_TEXT_CHARS}

def _cortex_attest_graph_db(require_populated=False):
    graph_dir = _CORTEX_GRAPHS_DIR / _CORTEX_GRAPH_STORAGE_KEY
    graphs_root = _CORTEX_GRAPHS_DIR.resolve(strict=True)
    if graph_dir.is_symlink() or graph_dir.resolve(strict=True).parent != graphs_root:
        raise RuntimeError("graph project directory lost descriptor-safe custody")
    graph_db = graph_dir / "graph.db"
    try:
        db_stat = graph_db.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError("authorized graph database is missing") from exc
    if (
        not stat.S_ISREG(db_stat.st_mode)
        or db_stat.st_nlink != 1
        or db_stat.st_uid != os.geteuid()
        or stat.S_IMODE(db_stat.st_mode) & 0o022
    ):
        raise RuntimeError("authorized graph database is not an owner-controlled regular file")
    if (db_stat.st_dev, db_stat.st_ino) != _CORTEX_EXPECTED_DB_IDENTITY:
        raise RuntimeError("authorized graph database identity changed")
    if (require_populated or not _CORTEX_MATERIALIZE) and db_stat.st_size <= 0:
        raise RuntimeError("authorized graph database is not populated")
    return graph_db

def _cortex_volume_db_path(_repo_root):
    return _cortex_attest_graph_db()

def _cortex_prepare_graph_git_marker():
    graph_dir = _CORTEX_GRAPHS_DIR / _CORTEX_GRAPH_STORAGE_KEY
    graphs_root = _CORTEX_GRAPHS_DIR.resolve(strict=True)
    if graph_dir.is_symlink() or graph_dir.resolve(strict=True).parent != graphs_root:
        raise RuntimeError("graph project directory lost descriptor-safe custody")
    graph_git = graph_dir / ".git"
    if _CORTEX_GIT_DIR is None:
        return
    try:
        marker_stat = graph_git.lstat()
    except FileNotFoundError:
        marker_stat = None
    if marker_stat is not None and not stat.S_ISREG(marker_stat.st_mode):
        raise RuntimeError("managed graph Git marker is not a strict regular file")

    marker = f"gitdir: {{_CORTEX_GIT_DIR}}\\n".encode("utf-8")
    marker_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    marker_flags |= getattr(os, "O_NOFOLLOW", 0)
    marker_fd = os.open(graph_git, marker_flags, 0o600)
    try:
        if os.write(marker_fd, marker) != len(marker):
            raise RuntimeError("short write for managed graph Git marker")
        os.fsync(marker_fd)
    finally:
        os.close(marker_fd)

_cortex_prepare_graph_git_marker()
tools.get_db_path = _cortex_volume_db_path

def _cortex_offline_qwen_model(self):
    cache_dir = os.environ.get("QWEN3_EMBED_CACHE_PATH")
    if cache_dir != {qwen_cache_json}:
        raise RuntimeError("graph embedding requires the receipt-pinned immutable Qwen cache")
    if self._model is None:
        from qwen3_embed import TextEmbedding
        self._model = TextEmbedding(
            model_name=self._model_name,
            cache_dir=cache_dir,
            local_files_only=True,
        )
    return self._model

embeddings.Qwen3EmbedBackend._get_model = _cortex_offline_qwen_model

def _cortex_bound_json(value, depth=0):
    global _CORTEX_BOUND_TRUNCATED
    if depth > 8:
        _CORTEX_BOUND_TRUNCATED = True
        return "[nested value truncated]"
    if isinstance(value, str):
        if len(value) > _CORTEX_MAX_RESPONSE_TEXT_CHARS:
            _CORTEX_BOUND_TRUNCATED = True
            return value[:_CORTEX_MAX_RESPONSE_TEXT_CHARS]
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        limit = _CORTEX_MAX_RESPONSE_ITEMS if depth <= 3 else 32
        if len(value) > limit:
            _CORTEX_BOUND_TRUNCATED = True
        return [_cortex_bound_json(item, depth + 1) for item in value[:limit]]
    if isinstance(value, dict):
        items = list(value.items())
        if len(items) > 64:
            _CORTEX_BOUND_TRUNCATED = True
        return {{
            str(key)[:256]: _cortex_bound_json(item, depth + 1)
            for key, item in items[:64]
        }}
    _CORTEX_BOUND_TRUNCATED = True
    return str(value)[:_CORTEX_MAX_RESPONSE_TEXT_CHARS]

def _cortex_response_lists(value):
    if isinstance(value, list):
        yield value
        for item in value:
            yield from _cortex_response_lists(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _cortex_response_lists(item)

def _cortex_emit(value):
    global _CORTEX_BOUND_TRUNCATED
    _cortex_attest_graph_db(require_populated=True)
    _CORTEX_BOUND_TRUNCATED = False
    bounded = _cortex_bound_json(value)
    if not isinstance(bounded, dict):
        bounded = {{"status": "error", "error": "invalid graph result"}}
        _CORTEX_BOUND_TRUNCATED = True
    if _CORTEX_BOUND_TRUNCATED:
        bounded["_cortex_output_truncated"] = True
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    while len(encoded.encode("utf-8")) > _CORTEX_MAX_RESPONSE_BYTES:
        collections = [items for items in _cortex_response_lists(bounded) if items]
        if not collections:
            raise RuntimeError("bounded graph response exceeds its byte ceiling")
        largest = max(collections, key=len)
        del largest[max(0, len(largest) // 2):]
        bounded["_cortex_output_truncated"] = True
        encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    print(encoded)
"""


def _read_small_regular_file(path: Path, *, max_bytes: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HTTPException(400, f"unsafe Git metadata file: {path}") from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > max_bytes:
            raise HTTPException(400, f"unsafe Git metadata file: {path}")
        data = os.read(fd, max_bytes + 1)
        if len(data) != file_stat.st_size:
            raise HTTPException(409, f"Git metadata changed while it was read: {path}")
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, f"Git metadata is not UTF-8: {path}") from exc


def _git_metadata_path(repo_path: Path) -> Path | None:
    """Resolve .git without following an attacker-controlled marker or symlink."""
    repo_git = repo_path / ".git"
    try:
        entry = repo_git.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HTTPException(400, f"cannot inspect Git metadata: {repo_git}") from exc
    if stat.S_ISLNK(entry.st_mode):
        raise HTTPException(400, "repo .git entry must not be a symlink")
    if stat.S_ISDIR(entry.st_mode):
        try:
            resolved_git = repo_git.resolve(strict=True)
            resolved_git.relative_to(_resolved_projects_root())
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                400, "repo .git directory escapes the projects trust root"
            ) from exc
        return resolved_git
    if not stat.S_ISREG(entry.st_mode) or entry.st_size > 4096:
        raise HTTPException(
            400, "repo .git entry is not a bounded Git directory marker"
        )

    content = _read_small_regular_file(repo_git, max_bytes=4096)
    lines = content.splitlines()
    if len(lines) != 1 or not lines[0].lower().startswith("gitdir:"):
        raise HTTPException(400, "repo .git marker is malformed")
    raw_target = lines[0][len("gitdir:") :].strip()
    if not raw_target or "\x00" in raw_target:
        raise HTTPException(400, "repo .git marker target is malformed")
    target = Path(raw_target)
    if not target.is_absolute():
        target = repo_git.parent / target
    try:
        resolved_target = target.resolve(strict=True)
        resolved_target.relative_to(_resolved_projects_root())
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            400, "repo .git marker escapes the projects trust root"
        ) from exc
    if not resolved_target.is_dir():
        raise HTTPException(400, "repo .git marker target is not a directory")
    return resolved_target


def _git_config_paths(git_dir: Path) -> list[Path]:
    paths = [git_dir / "config", git_dir / "config.worktree"]
    common = _git_common_dir(git_dir)
    if common != git_dir:
        paths.extend((common / "config", common / "config.worktree"))
    return list(dict.fromkeys(path for path in paths if path.exists()))


def _dangerous_git_config_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.startswith(("include.", "includeif.")):
        return True
    if lowered.startswith("filter.") and lowered.rsplit(".", 1)[-1] in {
        "clean",
        "smudge",
        "process",
    }:
        return True
    if lowered.startswith("diff.") and lowered.rsplit(".", 1)[-1] in {
        "command",
        "textconv",
    }:
        return True
    if lowered.startswith(("difftool.", "mergetool.")) and lowered.endswith(".cmd"):
        return True
    if lowered.startswith("merge.") and lowered.endswith(".driver"):
        return True
    if lowered.startswith("submodule.") and lowered.endswith(".update"):
        return True
    if lowered.startswith("remote.") and lowered.rsplit(".", 1)[-1] in {
        "proxy",
        "receivepack",
        "uploadpack",
        "vcs",
    }:
        return True
    return False


def _validate_git_configuration(git_dir: Path) -> None:
    """Reject repo-local Git options that can execute donor-controlled commands."""
    git_binary = Path("/usr/bin/git")
    if not git_binary.is_file():
        raise HTTPException(503, "the pinned Git runtime is unavailable")
    for config_path in _git_config_paths(git_dir):
        try:
            config_stat = config_path.lstat()
        except OSError as exc:
            raise HTTPException(
                400, f"cannot inspect Git config: {config_path}"
            ) from exc
        if (
            stat.S_ISLNK(config_stat.st_mode)
            or not stat.S_ISREG(config_stat.st_mode)
            or config_stat.st_size > 1024 * 1024
        ):
            raise HTTPException(
                400, f"Git config is not a bounded regular file: {config_path}"
            )
        returncode, stdout, stderr = _run_process_bounded(
            [
                str(git_binary),
                "config",
                "--file",
                str(config_path),
                "--no-includes",
                "--null",
                "--name-only",
                "--list",
            ],
            timeout=5,
            env=_bcrg_environment(),
        )
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-500:]
            raise HTTPException(400, f"cannot parse Git config safely: {detail}")
        try:
            keys = [item.decode("utf-8") for item in stdout.split(b"\x00") if item]
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "Git config contains a non-UTF-8 key") from exc
        dangerous = sorted(key for key in keys if _dangerous_git_config_key(key))
        if dangerous:
            raise HTTPException(
                400,
                f"Git config contains executable directives: {', '.join(dangerous[:5])}",
            )


def _has_git_repo(repo_path: Path) -> bool:
    git_dir = _git_metadata_path(repo_path)
    if git_dir is None:
        return False
    _validate_git_configuration(git_dir)
    return True


def _open_existing_graph_fd(repo_path: Path) -> tuple[int, os.stat_result] | None:
    """Open repo-local graph.db through no-follow directory descriptors."""
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    repo_fd = graph_fd = source_fd = -1
    source_transferred = False
    try:
        repo_fd = os.open(repo_path, directory_flags)
        try:
            graph_fd = os.open(".code-review-graph", directory_flags, dir_fd=repo_fd)
        except FileNotFoundError:
            return None
        try:
            source_fd = os.open("graph.db", file_flags, dir_fd=graph_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HTTPException(
                400, "repo-local graph.db is not a strict regular file"
            ) from exc

        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 0:
            raise HTTPException(
                400, "repo-local graph.db is not a populated regular file"
            )
        if source_stat.st_size > MAX_IMPORT_BYTES:
            raise HTTPException(
                413, "repo-local graph.db exceeds the import size limit"
            )

        # A byte-copy cannot safely combine an SQLite database with a changing
        # WAL/journal. Require the donor workflow to checkpoint/close it first.
        for sidecar in ("graph.db-wal", "graph.db-shm", "graph.db-journal"):
            try:
                os.stat(sidecar, dir_fd=graph_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise HTTPException(
                    400, f"cannot inspect repo-local SQLite sidecar: {sidecar}"
                ) from exc
            raise HTTPException(
                409,
                f"repo-local graph is not checkpointed; close/remove SQLite sidecar {sidecar}",
            )
        source_transferred = True
        return source_fd, source_stat
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(
            400, "repo-local graph directory is not descriptor-safe"
        ) from exc
    finally:
        if source_fd >= 0 and not source_transferred:
            os.close(source_fd)
        if graph_fd >= 0:
            os.close(graph_fd)
        if repo_fd >= 0:
            os.close(repo_fd)


def _existing_graph_db(repo_path: Path) -> Path | None:
    opened = _open_existing_graph_fd(repo_path)
    if opened is None:
        return None
    source_fd, _source_stat = opened
    os.close(source_fd)
    return repo_path / ".code-review-graph" / "graph.db"


def _sqlite_count(db_path: Path, table: str) -> int:
    if table not in {"nodes", "edges"}:
        raise ValueError(f"unsupported graph table: {table}")
    uri_path = quote(os.fspath(db_path), safe="/")
    try:
        con = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=1.0)
        try:
            deadline = time.monotonic() + 5.0
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA trusted_schema=OFF")
            con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1_000)
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read {table} from graph database: {exc}") from exc


def _validate_import_db(db_path: Path) -> tuple[int, int]:
    """Fail closed unless the copied donor is a coherent graph SQLite DB."""
    uri_path = quote(os.fspath(db_path), safe="/")
    try:
        con = sqlite3.connect(
            f"file:{uri_path}?mode=ro&immutable=1",
            uri=True,
            timeout=1.0,
        )
        try:
            deadline = time.monotonic() + 30.0
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA trusted_schema=OFF")
            con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1_000)
            integrity = con.execute("PRAGMA quick_check(1)").fetchone()
            if integrity != ("ok",):
                raise ValueError(f"SQLite quick_check failed: {integrity!r}")
            tables = {
                str(row[0])
                for row in con.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name IN ('nodes','edges')"
                )
            }
            if tables != {"nodes", "edges"}:
                raise ValueError("graph database must contain nodes and edges tables")
            nodes = int(con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
            edges = int(con.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
            return nodes, edges
        finally:
            con.close()
    except (sqlite3.Error, ValueError) as exc:
        raise HTTPException(
            400, f"repo-local graph.db failed validation: {exc}"
        ) from exc


def _descriptor_path(fd: int) -> Path:
    """Return this process's stable path alias for an already-open descriptor."""
    for root in (Path("/proc/self/fd"), Path("/dev/fd")):
        if root.is_dir():
            return root / str(fd)
    raise HTTPException(503, "the runtime has no descriptor filesystem")


def _copy_fd_bounded(source_fd: int, dest_fd: int, expected_size: int) -> None:
    copied = 0
    os.lseek(source_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(source_fd, min(1024 * 1024, MAX_IMPORT_BYTES - copied + 1))
        if not chunk:
            break
        copied += len(chunk)
        if copied > MAX_IMPORT_BYTES or copied > expected_size:
            raise HTTPException(
                409, "repo-local graph.db changed while it was imported"
            )
        view = memoryview(chunk)
        while view:
            written = os.write(dest_fd, view)
            if written <= 0:
                raise OSError("short write while importing graph database")
            view = view[written:]
    if copied != expected_size:
        raise HTTPException(409, "repo-local graph.db changed while it was imported")


def _import_existing_graph(storage_key: str, repo_path: Path) -> dict:
    """Import a repo-local .code-review-graph DB into the managed graph volume.

    Some turnkey/customer project folders are workspaces rather than git repos.
    better-code-review-graph migrations need git metadata, but those workspaces
    may already carry a valid `.code-review-graph/graph.db` produced by the old
    host workflow. In that case, reuse it instead of forcing a doomed rebuild.
    """
    opened = _open_existing_graph_fd(repo_path)
    if opened is None:
        raise HTTPException(
            400,
            "repo is not a git repository and no .code-review-graph/graph.db exists to import",
        )
    source_fd, source_before = opened
    source = repo_path / ".code-review-graph" / "graph.db"
    graph_dir = _ensure_graph_dir(storage_key)
    graph_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    graph_fd = os.open(graph_dir, graph_flags)
    temp_fd = -1
    temp_name: str | None = None
    try:
        temp_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        for _attempt in range(16):
            candidate_name = f".graph.db.import-{secrets.token_hex(8)}"
            try:
                temp_fd = os.open(candidate_name, temp_flags, 0o600, dir_fd=graph_fd)
            except FileExistsError:
                continue
            temp_name = candidate_name
            break
        if temp_fd < 0 or temp_name is None:
            raise HTTPException(503, "cannot allocate a managed graph import file")
        _copy_fd_bounded(source_fd, temp_fd, source_before.st_size)
        source_after = os.fstat(source_fd)
        identity_before = (
            source_before.st_dev,
            source_before.st_ino,
            source_before.st_size,
            source_before.st_mtime_ns,
            source_before.st_ctime_ns,
        )
        identity_after = (
            source_after.st_dev,
            source_after.st_ino,
            source_after.st_size,
            source_after.st_mtime_ns,
            source_after.st_ctime_ns,
        )
        if identity_after != identity_before:
            raise HTTPException(
                409, "repo-local graph.db changed while it was imported"
            )
        os.fsync(temp_fd)
        nodes, edges = _validate_import_db(_descriptor_path(temp_fd))

        dest = graph_dir / "graph.db"
        for sidecar in ("graph.db-wal", "graph.db-shm", "graph.db-journal"):
            try:
                os.stat(sidecar, dir_fd=graph_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise HTTPException(
                409,
                f"managed graph is active or uncheckpointed; preserve SQLite sidecar {sidecar}",
            )
        os.replace(temp_name, "graph.db", src_dir_fd=graph_fd, dst_dir_fd=graph_fd)
        temp_name = None
        os.fsync(graph_fd)
    finally:
        os.close(source_fd)
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=graph_fd)
            except FileNotFoundError:
                pass
        os.close(graph_fd)

    dest = graph_dir / "graph.db"
    return {
        "status": "imported-existing-graph",
        "storage_key": storage_key,
        "repo": str(repo_path),
        "source": str(source),
        "graph_db": str(dest),
        "nodes": nodes,
        "edges": edges,
    }


class _SubprocessOutputTooLarge(Exception):
    pass


def _bcrg_environment() -> dict[str, str]:
    """Return a deterministic environment that disables Git execution/egress seams."""
    blocked_names = {
        "BASH_ENV",
        "CDPATH",
        "ENV",
        "GIT_EXTERNAL_DIFF",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
    blocked_prefixes = ("DYLD_", "GIT_", "LD_", "PYTHON")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked_names and not key.startswith(blocked_prefixes)
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PROTOCOL_FROM_USER": "0",
        }
    )
    forced_git_config = (
        ("core.fsmonitor", "false"),
        ("core.hooksPath", "/dev/null"),
        ("core.pager", "cat"),
        ("interactive.diffFilter", ""),
        ("credential.helper", ""),
        ("core.sshCommand", "/bin/false"),
        ("gpg.program", "/bin/false"),
        ("sequence.editor", "/bin/false"),
        ("protocol.allow", "never"),
        ("protocol.file.allow", "never"),
        ("protocol.ext.allow", "never"),
        ("protocol.git.allow", "never"),
        ("protocol.http.allow", "never"),
        ("protocol.https.allow", "never"),
        ("protocol.ssh.allow", "never"),
    )
    env["GIT_CONFIG_COUNT"] = str(len(forced_git_config))
    for index, (key, value) in enumerate(forced_git_config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Terminate the isolated BCRG process group, including Git descendants."""
    process_group = proc.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        proc.poll()
        return
    except OSError:
        try:
            proc.terminate()
        except OSError:
            return

    # The group leader can exit before a descendant that ignored SIGTERM. Waiting
    # only for ``proc`` therefore does not prove the bounded process tree stopped.
    # Probe the process group itself for the full grace period, then kill whatever
    # remains before reaping the leader.
    deadline = time.monotonic() + 0.5
    group_exists = True
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            group_exists = False
            break
        except PermissionError:
            pass
        except OSError:
            group_exists = proc.poll() is None
            break
        proc.poll()
        time.sleep(0.01)

    if group_exists:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _run_process_bounded(
    command: list[str],
    *,
    timeout: float,
    env: dict[str, str],
) -> tuple[int, bytes, bytes]:
    """Capture finite output and enforce a wall clock over the whole process group."""
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            bufsize=0,
        )
    except OSError as exc:
        raise HTTPException(503, f"cannot start code graph runtime: {exc}") from exc

    assert proc.stdout is not None
    assert proc.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            events = selector.select(min(remaining, 0.2))
            for key, _mask in events:
                chunk = os.read(key.fd, SUBPROCESS_READ_CHUNK)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffers[key.data].extend(chunk)
                total = len(buffers["stdout"]) + len(buffers["stderr"])
                if total > MAX_SUBPROCESS_OUTPUT_BYTES:
                    raise _SubprocessOutputTooLarge

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        returncode = proc.wait(timeout=remaining)
    except (subprocess.TimeoutExpired, _SubprocessOutputTooLarge):
        _terminate_process_group(proc)
        raise
    except BaseException:
        _terminate_process_group(proc)
        raise
    finally:
        for key in list(selector.get_map().values()):
            try:
                selector.unregister(key.fileobj)
            except Exception:
                pass
            try:
                key.fileobj.close()
            except OSError:
                pass
        selector.close()

    # A malicious descendant can close inherited pipes and outlive its parent.
    # The BCRG call has no legitimate daemon contract, so reap the whole group.
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        pass
    else:
        _terminate_process_group(proc)
    return returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])


def _run_bcrg(code: str, *, timeout: float = 120) -> dict:
    command = [BCRG_PYTHON, "-I", "-c", code]
    try:
        returncode, stdout_bytes, stderr_bytes = _run_process_bounded(
            command,
            timeout=timeout,
            env=_bcrg_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            504,
            f"code graph operation exceeded {timeout}s; "
            "the bounded process was terminated; retry the request",
        ) from exc
    except _SubprocessOutputTooLarge as exc:
        raise HTTPException(
            502, "code graph runtime exceeded its output limit"
        ) from exc
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if returncode != 0:
        raise HTTPException(500, stderr[-1000:] or stdout[-1000:])
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise HTTPException(
            500, f"worker returned non-JSON output: {stdout[-1000:]}"
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(502, "code graph runtime returned a non-object response")
    return result


def _release_single_flight(task: asyncio.Task) -> None:
    """Release capacity only after the shielded worker thread has actually exited."""
    _bcrg_slots.release()
    if task.cancelled():
        return
    # A disconnected client no longer awaits the task. Retrieve a late worker
    # exception here so asyncio does not report it as an unhandled task failure.
    task.exception()


async def _run_single_flight(
    function, /, *args, _wall_timeout: int | None = None, **kwargs
):
    """Shed contention and keep mutation custody through client cancellation."""
    admission_started = time.monotonic()
    try:
        await asyncio.wait_for(
            _bcrg_slots.acquire(), timeout=SINGLE_FLIGHT_ADMISSION_SECONDS
        )
    except TimeoutError as exc:
        raise HTTPException(
            503,
            "code graph worker is busy; retry after the active operation completes",
            headers={"Retry-After": "1"},
        ) from exc

    if _wall_timeout is not None:
        remaining = _wall_timeout - (time.monotonic() - admission_started)
        if remaining <= 0:
            _bcrg_slots.release()
            raise HTTPException(504, "code graph operation exhausted its wall deadline")
        kwargs["timeout"] = remaining

    try:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    except BaseException:
        _bcrg_slots.release()
        raise
    task.add_done_callback(_release_single_flight)
    return await asyncio.shield(task)


async def _run_bcrg_single_flight(code: str, *, timeout: int) -> dict:
    return await _run_single_flight(
        _run_bcrg,
        code,
        timeout=timeout,
        _wall_timeout=timeout,
    )


def _remaining_operation_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HTTPException(504, "code graph operation exhausted its wall deadline")
    return remaining


def _bounded_response_text(value) -> tuple[str, bool]:
    if value is None:
        return "", False
    text = str(value)
    return text[:MAX_GRAPH_RESPONSE_TEXT_CHARS], len(text) > MAX_GRAPH_RESPONSE_TEXT_CHARS


def _bounded_response_value(value, *, depth: int = 0):
    """Bound nested JSON values returned by the pinned graph dependency."""
    if depth > 8:
        return "[nested value truncated]", True
    if isinstance(value, str):
        return _bounded_response_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, list):
        limit = MAX_GRAPH_RESPONSE_ITEMS if depth <= 3 else 32
        truncated = len(value) > limit
        out = []
        for item in value[:limit]:
            bounded, item_truncated = _bounded_response_value(item, depth=depth + 1)
            out.append(bounded)
            truncated = truncated or item_truncated
        return out, truncated
    if isinstance(value, dict):
        items = list(value.items())
        truncated = len(items) > 64
        out = {}
        for key, item in items[:64]:
            bounded, item_truncated = _bounded_response_value(item, depth=depth + 1)
            out[str(key)[:256]] = bounded
            truncated = truncated or item_truncated
        return out, truncated
    text, _ = _bounded_response_text(value)
    return text, True


def _response_list(raw: dict, key: str) -> list:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise HTTPException(502, f"code graph response field {key!r} is not a list")
    return value


def _bounded_response_list(raw: dict, key: str, *, limit: int) -> tuple[list, int, bool]:
    values = _response_list(raw, key)
    bounded = []
    truncated = len(values) > limit
    for item in values[:limit]:
        safe_item, item_truncated = _bounded_response_value(item, depth=1)
        bounded.append(safe_item)
        truncated = truncated or item_truncated
    return bounded, len(values), truncated


def _declared_total(raw: dict, key: str, observed: int) -> int:
    totals = raw.get("_cortex_totals", {})
    value = totals.get(key, observed) if isinstance(totals, dict) else observed
    if isinstance(value, bool) or not isinstance(value, int) or value < observed:
        return observed
    return value


def _mutable_response_lists(value):
    if isinstance(value, list):
        yield value
        for item in value:
            yield from _mutable_response_lists(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _mutable_response_lists(item)


def _enforce_response_size(response: dict) -> dict:
    """Guarantee the internal API never emits an oversized normalized payload."""
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    while len(encoded) > MAX_GRAPH_RESPONSE_BYTES:
        collections = [items for items in _mutable_response_lists(response) if items]
        if not collections:
            raise HTTPException(502, "normalized code graph response exceeds its byte ceiling")
        largest = max(collections, key=len)
        del largest[max(0, len(largest) // 2) :]
        response["truncated"] = True
        encoded = json.dumps(
            response, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    return response


def _normalize_blast_response(raw: dict, *, max_results: int) -> dict:
    status = raw.get("status")
    if status not in {"ok", "error"}:
        raise HTTPException(502, "code graph blast returned an unsupported status")
    summary, summary_truncated = _bounded_response_text(raw.get("summary"))
    if status == "error":
        error, error_truncated = _bounded_response_text(raw.get("error"))
        return _enforce_response_size(
            {
                "status": status,
                "summary": summary,
                "error": error,
                "changed_files": [],
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "edges": [],
                "totals": {
                    "changed_files": 0,
                    "changed_nodes": 0,
                    "impacted_nodes": 0,
                    "impacted_files": 0,
                    "edges": 0,
                },
                "total_impacted": 0,
                "truncated": bool(
                    raw.get("_cortex_output_truncated")
                    or summary_truncated
                    or error_truncated
                ),
            }
        )

    limit = min(max_results, MAX_GRAPH_RESPONSE_ITEMS)
    collections = {}
    totals = {}
    truncated = bool(
        raw.get("truncated")
        or raw.get("results_truncated")
        or raw.get("_cortex_output_truncated")
        or summary_truncated
    )
    for key in (
        "changed_files",
        "changed_nodes",
        "impacted_nodes",
        "impacted_files",
        "edges",
    ):
        values, observed, values_truncated = _bounded_response_list(
            raw, key, limit=limit
        )
        total = _declared_total(raw, key, observed)
        collections[key] = values
        totals[key] = total
        truncated = truncated or values_truncated or total > len(values)

    upstream_total = raw.get("total_impacted", totals["impacted_nodes"])
    if isinstance(upstream_total, bool) or not isinstance(upstream_total, int):
        upstream_total = totals["impacted_nodes"]
    total_impacted = max(upstream_total, totals["impacted_nodes"])
    response = {
        "status": status,
        "summary": summary,
        **collections,
        "totals": totals,
        "total_impacted": total_impacted,
        "truncated": truncated,
    }
    return _enforce_response_size(response)


def _normalize_callers_response(raw: dict, *, max_results: int) -> dict:
    status = raw.get("status")
    if status not in {"ok", "ambiguous", "not_found", "error"}:
        raise HTTPException(502, "code graph query returned an unsupported status")
    limit = min(max_results, MAX_GRAPH_RESPONSE_ITEMS)
    truncated = bool(raw.get("_cortex_output_truncated"))
    text_fields = {}
    for key, text_limit in (
        ("pattern", 256),
        ("target", MAX_GRAPH_RESPONSE_TEXT_CHARS),
        ("description", MAX_GRAPH_RESPONSE_TEXT_CHARS),
        ("summary", MAX_GRAPH_RESPONSE_TEXT_CHARS),
        ("reason", 256),
        ("error", MAX_GRAPH_RESPONSE_TEXT_CHARS),
        ("hint", MAX_GRAPH_RESPONSE_TEXT_CHARS),
    ):
        text_value = str(raw.get(key) or "")
        text_fields[key] = text_value[:text_limit]
        truncated = truncated or len(text_value) > text_limit
    response = {
        "status": status,
        "target_indexed": bool(
            status == "ok" and isinstance(raw.get("header"), dict)
        ),
        **text_fields,
        "results": [],
        "edges": [],
        "candidates": [],
        "indexed_kinds": [],
        "indexed_under": [],
        "totals": {
            "results": 0,
            "edges": 0,
            "candidates": 0,
            "indexed_kinds": 0,
            "indexed_under": 0,
        },
        "truncated": False,
    }
    for key in ("results", "edges", "candidates", "indexed_kinds", "indexed_under"):
        values, observed, values_truncated = _bounded_response_list(
            raw, key, limit=limit
        )
        total = _declared_total(raw, key, observed)
        response[key] = values
        response["totals"][key] = total
        truncated = truncated or values_truncated or total > len(values)

    for key in ("header", "dynamic_dispatch_hints"):
        if key in raw:
            value, value_truncated = _bounded_response_value(raw[key], depth=1)
            response[key] = value
            truncated = truncated or value_truncated
    if "resolved_from_unqualified" in raw:
        response["resolved_from_unqualified"] = bool(
            raw["resolved_from_unqualified"]
        )
    response["truncated"] = truncated
    return _enforce_response_size(response)


def _normalize_impact_response(raw: dict, *, max_results: int) -> dict:
    status = raw.get("status")
    if status not in {"ok", "error"}:
        raise HTTPException(502, "code graph review context returned an unsupported status")
    summary, summary_truncated = _bounded_response_text(raw.get("summary"))
    if status == "error":
        error, error_truncated = _bounded_response_text(raw.get("error"))
        return _enforce_response_size(
            {
                "status": status,
                "summary": summary,
                "error": error,
                "context": {
                    "changed_files": [],
                    "impacted_files": [],
                    "graph": {"changed_nodes": [], "impacted_nodes": [], "edges": []},
                    "untested_functions": [],
                    "review_guidance": "",
                },
                "totals": {
                    "changed_files": 0,
                    "impacted_files": 0,
                    "changed_nodes": 0,
                    "impacted_nodes": 0,
                    "edges": 0,
                    "untested_functions": 0,
                },
                "truncated": bool(
                    raw.get("_cortex_output_truncated")
                    or summary_truncated
                    or error_truncated
                ),
            }
        )

    context = raw.get("context", {})
    if not isinstance(context, dict):
        raise HTTPException(502, "code graph review context is not an object")
    graph = context.get("graph", {})
    if not isinstance(graph, dict):
        raise HTTPException(502, "code graph review context graph is not an object")
    limit = min(max_results, MAX_GRAPH_RESPONSE_ITEMS)
    truncated = bool(raw.get("_cortex_output_truncated") or summary_truncated)
    normalized_context = {}
    totals = {}
    for key in ("changed_files", "impacted_files", "untested_functions"):
        values, observed, values_truncated = _bounded_response_list(
            context, key, limit=limit
        )
        total = _declared_total(raw, key, observed)
        normalized_context[key] = values
        totals[key] = total
        truncated = truncated or values_truncated or total > len(values)

    normalized_graph = {}
    for key in ("changed_nodes", "impacted_nodes", "edges"):
        values, observed, values_truncated = _bounded_response_list(
            graph, key, limit=limit
        )
        total = _declared_total(raw, key, observed)
        normalized_graph[key] = values
        totals[key] = total
        truncated = truncated or values_truncated or total > len(values)
    guidance, guidance_truncated = _bounded_response_text(
        context.get("review_guidance")
    )
    normalized_context["graph"] = normalized_graph
    normalized_context["review_guidance"] = guidance
    truncated = truncated or guidance_truncated
    response = {
        "status": status,
        "summary": summary,
        "context": normalized_context,
        "totals": totals,
        "truncated": truncated,
    }
    return _enforce_response_size(response)


def _iter_graph_dirs():
    """Yield every strict generation directory with bounded-memory inventory.

    Failed builds can leave only the directory and its BCRG ``.git`` redirect.
    Those empty generations are still prune candidates, so inventory must not
    filter on ``graph.db`` or fail merely because more than the response-sized
    number of orphan directories exists.
    """
    if not GRAPHS_DIR.exists():
        return
    root = _resolved_graphs_root()
    try:
        with os.scandir(root) as iterator:
            for entry in iterator:
                try:
                    if entry.is_symlink():
                        raise HTTPException(
                            409, f"graph project entry is a symlink: {entry.name!r}"
                        )
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise HTTPException(
                        409, f"cannot inspect graph project: {entry.name!r}"
                    ) from exc
                name = _validate_project_name(entry.name)
                yield name, root / name, entry_stat
    except OSError as exc:
        raise HTTPException(503, f"cannot enumerate graph projects: {root}") from exc


def _graph_db_inventory(graph_dir: Path) -> tuple[Path, int, str]:
    """Describe a generation's graph.db without following an unsafe entry."""
    db = graph_dir / "graph.db"
    try:
        db_stat = db.lstat()
    except FileNotFoundError:
        return db, 0, "missing"
    except OSError as exc:
        raise HTTPException(409, f"cannot inspect graph database: {db}") from exc
    if stat.S_ISLNK(db_stat.st_mode):
        return db, 0, "symlink"
    if not stat.S_ISREG(db_stat.st_mode):
        return db, 0, "non-regular"
    return db, db_stat.st_size, "regular"


def _require_populated_graph_db(
    storage_key: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> Path:
    """Require an existing populated DB without creating generation state."""
    if not GRAPHS_DIR.exists():
        raise HTTPException(409, "authorized graph database is not materialized")
    graph_dir = _safe_graph_dir(storage_key)
    try:
        graph_stat = graph_dir.lstat()
    except FileNotFoundError:
        raise HTTPException(409, "authorized graph database is not materialized")
    except OSError as exc:
        raise HTTPException(
            409, f"cannot inspect graph project path: {graph_dir}"
        ) from exc
    if stat.S_ISLNK(graph_stat.st_mode) or not stat.S_ISDIR(graph_stat.st_mode):
        raise HTTPException(
            409, f"graph project path is not a strict directory: {graph_dir}"
        )
    db, size_bytes, state = _graph_db_inventory(graph_dir)
    if state in {"symlink", "non-regular"}:
        raise HTTPException(409, f"graph database is not a strict regular file: {db}")
    if state != "regular" or size_bytes <= 0:
        raise HTTPException(409, "authorized graph database is not materialized")
    try:
        db_stat = db.lstat()
    except OSError as exc:
        raise HTTPException(409, f"cannot attest graph database: {db}") from exc
    if (
        db_stat.st_nlink != 1
        or db_stat.st_uid != os.geteuid()
        or stat.S_IMODE(db_stat.st_mode) & 0o022
    ):
        raise HTTPException(
            409,
            "authorized graph database is not a single-link, owner-controlled file",
        )
    if expected_identity is not None and (
        db_stat.st_dev,
        db_stat.st_ino,
    ) != expected_identity:
        raise HTTPException(409, "authorized graph database identity changed")
    return db


def _safe_graph_dir(name: str) -> Path:
    """Resolve a graph project dir without allowing path traversal."""
    name = _validate_project_name(name)
    root = _resolved_graphs_root()
    path = root / name
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise HTTPException(409, f"cannot inspect graph project path: {path}") from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise HTTPException(
            409, f"graph project path is not a strict directory: {path}"
        )
    try:
        if path.resolve(strict=True).parent != root:
            raise HTTPException(
                409, f"graph project path escapes the graph volume: {path}"
            )
    except (OSError, RuntimeError) as exc:
        raise HTTPException(409, f"cannot resolve graph project path: {path}") from exc
    return path


@app.get("/health")
async def health():
    graphs_available = GRAPHS_DIR.is_dir() and os.access(GRAPHS_DIR, os.W_OK | os.X_OK)
    projects_available = PROJECTS_DIR.is_dir() and os.access(
        PROJECTS_DIR, os.R_OK | os.X_OK
    )
    bcrg_available = Path(BCRG_PYTHON).is_file() and os.access(BCRG_PYTHON, os.X_OK)
    return {
        "ok": graphs_available and projects_available and bcrg_available,
        "graphs_dir": str(GRAPHS_DIR),
        "projects_dir": str(PROJECTS_DIR),
        "graphs_dir_exists": GRAPHS_DIR.exists(),
        "projects_dir_exists": PROJECTS_DIR.exists(),
        "graphs_dir_available": graphs_available,
        "projects_dir_available": projects_available,
        "bcrg_python": BCRG_PYTHON,
        "bcrg_available": bcrg_available,
    }


@app.get("/stats")
async def stats(storage_key: StorageKey):
    return await _run_single_flight(_collect_stats, storage_key)


def _collect_stats(storage_key: str) -> dict:
    """Project-filtered aggregate via bounded SQLite reads.

    Keep this off the asyncio event loop: graph DBs can live on mounted drives,
    and a slow filesystem must not block `/health` or other worker requests.
    """
    storage_key = _validate_project_name(storage_key)
    path = _require_populated_graph_db(storage_key)
    out = {"repos": [], "total_nodes": 0, "total_edges": 0}
    try:
        nodes = _sqlite_count(path, "nodes")
        edges = _sqlite_count(path, "edges")
        out["repos"].append(
            {
                "name": storage_key,
                "nodes": nodes,
                "edges": edges,
                "path": str(path),
            }
        )
        out["total_nodes"] = nodes
        out["total_edges"] = edges
    except Exception as exc:
        out["repos"].append({"name": storage_key, "error": str(exc)})
    return out


def _remove_graph_dir(name: str, expected_identity: tuple[int, int]) -> None:
    """Delete one direct graph directory with fd-relative symlink resistance."""
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise HTTPException(503, "this Python runtime cannot prune symlink-safely")
    _root, root_fd = _open_graphs_root_fd()
    try:
        try:
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise HTTPException(
                409, f"graph project changed before prune: {name!r}"
            ) from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != expected_identity
        ):
            raise HTTPException(409, f"graph project changed before prune: {name!r}")
        shutil.rmtree(name, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _prune_graphs(body: PruneBody) -> dict:
    active = {_validate_project_name(project) for project in body.active_projects}
    candidates = []
    pruned = []
    candidate_count = 0
    pruned_count = 0

    for name, graph_dir, graph_stat in _iter_graph_dirs() or ():
        if name in active:
            continue
        db_path, size_bytes, db_state = _graph_db_inventory(graph_dir)
        item = {
            "name": name,
            "path": str(graph_dir),
            "graph_db": str(db_path),
            "size_bytes": size_bytes,
            "graph_db_state": db_state,
        }
        candidate_count += 1
        if len(candidates) < MAX_GRAPH_RESPONSE_ITEMS:
            candidates.append(item)
        if not body.dry_run:
            _remove_graph_dir(name, (graph_stat.st_dev, graph_stat.st_ino))
            pruned_count += 1
            if len(pruned) < MAX_GRAPH_RESPONSE_ITEMS:
                pruned.append(item)

    return {
        "dry_run": body.dry_run,
        "active_projects": sorted(active),
        "candidates": sorted(candidates, key=lambda item: item["name"]),
        "candidate_count": candidate_count,
        "pruned": sorted(pruned, key=lambda item: item["name"]),
        "pruned_count": pruned_count,
        "truncated": candidate_count > len(candidates) or pruned_count > len(pruned),
    }


@app.post("/prune")
async def prune(body: PruneBody):
    """Dry-run/apply deletion of graph.db dirs not present in active_projects."""
    return await _run_single_flight(_prune_graphs, body)


def _build_graph(body: BuildBody, *, timeout: float = 600) -> dict:
    deadline = time.monotonic() + timeout
    repo_path = _resolve_repo(body.repo, body.registered_repo)
    if body.import_existing:
        return _import_existing_graph(body.storage_key, repo_path)
    expected_db_identity = None
    if body.full:
        _graph_dir, expected_db_identity = _prepare_materialization_graph_db(
            body.storage_key
        )
    else:
        _require_populated_graph_db(body.storage_key)
    if not _has_git_repo(repo_path):
        raise HTTPException(
            400,
            "repo is not a Git repository; set import_existing=true only after "
            "reviewing the repo-local .code-review-graph/graph.db donor",
        )
    full_flag = "True" if body.full else "False"
    embed_flag = "True" if body.embed else "False"
    code = (
        _tool_preamble(
            body.storage_key,
            repo_path,
            materialize=body.full,
            expected_db_identity=expected_db_identity,
        )
        + f"""
import json
from better_code_review_graph.tools import build_or_update_graph, embed_graph

repo_root = {json.dumps(str(repo_path))}
r = build_or_update_graph(full_rebuild={full_flag}, repo_root=repo_root)
if isinstance(r, str):
    r = json.loads(r)
if {embed_flag}:
    e = embed_graph(repo_root=repo_root)
    if isinstance(e, str):
        e = json.loads(e)
    r["embeddings"] = e
_cortex_emit(r)
"""
    )
    result = _run_bcrg(code, timeout=_remaining_operation_timeout(deadline))
    _require_populated_graph_db(
        body.storage_key,
        expected_identity=expected_db_identity,
    )
    return result


@app.post("/build")
async def build(body: BuildBody):
    """Build/update a repo's graph using better-code-review-graph (host-bind)."""
    return await _run_single_flight(
        _build_graph,
        body,
        timeout=600,
        _wall_timeout=600,
    )


def _blast(body: BlastBody, *, timeout: float) -> dict:
    """Blast radius via the pinned better-code-review-graph contract."""
    deadline = time.monotonic() + timeout
    repo_path = _resolve_repo(body.repo, body.registered_repo)
    effective_limit = min(body.max_results, MAX_GRAPH_RESPONSE_ITEMS)
    code = (
        _tool_preamble(body.storage_key, repo_path)
        + f"""
import json
from better_code_review_graph.tools import get_impact_radius

r = get_impact_radius(
    changed_files={json.dumps(body.files)},
    max_depth={body.depth},
    max_results={effective_limit},
    repo_root={json.dumps(str(repo_path))},
)
if isinstance(r, str):
    r = json.loads(r)
if not isinstance(r, dict):
    r = {{"status": "error", "error": "invalid blast response"}}
r["_cortex_totals"] = {{
    key: len(r.get(key, [])) if isinstance(r.get(key, []), list) else 0
    for key in ("changed_files", "changed_nodes", "impacted_nodes", "impacted_files", "edges")
}}
_cortex_emit(r)
"""
    )
    raw = _run_bcrg(code, timeout=_remaining_operation_timeout(deadline))
    return _normalize_blast_response(raw, max_results=body.max_results)


@app.post("/blast")
async def blast(body: BlastBody):
    return await _run_single_flight(
        _blast,
        body,
        timeout=120,
        _wall_timeout=120,
    )


def _callers(body: CallersBody, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    repo_path = _resolve_repo(body.repo, body.registered_repo)
    code = (
        _tool_preamble(body.storage_key, repo_path)
        + f"""
import json
from better_code_review_graph.tools import query_graph

r = query_graph(
    pattern={json.dumps(body.pattern)},
    target={json.dumps(body.target)},
    repo_root={json.dumps(str(repo_path))},
)
if isinstance(r, str):
    r = json.loads(r)
if not isinstance(r, dict):
    r = {{"status": "error", "error": "invalid query response"}}
r["_cortex_totals"] = {{
    key: len(r.get(key, [])) if isinstance(r.get(key, []), list) else 0
    for key in ("results", "edges", "candidates", "indexed_kinds", "indexed_under")
}}
_cortex_emit(r)
"""
    )
    raw = _run_bcrg(code, timeout=_remaining_operation_timeout(deadline))
    return _normalize_callers_response(raw, max_results=body.max_results)


@app.post("/callers")
async def callers(body: CallersBody):
    return await _run_single_flight(
        _callers,
        body,
        timeout=60,
        _wall_timeout=60,
    )


def _impact(body: ImpactBody, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    repo_path = _resolve_repo(body.repo, body.registered_repo)
    code = (
        _tool_preamble(body.storage_key, repo_path)
        + f"""
import json
from better_code_review_graph.tools import get_review_context

r = get_review_context(
    max_depth=2,
    include_source=False,
    repo_root={json.dumps(str(repo_path))},
    base={json.dumps(body.base)},
)
if isinstance(r, str):
    r = json.loads(r)
if not isinstance(r, dict):
    r = {{"status": "error", "error": "invalid review-context response"}}
context = r.get("context", {{}}) if isinstance(r.get("context", {{}}), dict) else {{}}
graph = context.get("graph", {{}}) if isinstance(context.get("graph", {{}}), dict) else {{}}
r["_cortex_totals"] = {{
    "changed_files": len(context.get("changed_files", [])) if isinstance(context.get("changed_files", []), list) else 0,
    "impacted_files": len(context.get("impacted_files", [])) if isinstance(context.get("impacted_files", []), list) else 0,
    "untested_functions": len(context.get("untested_functions", [])) if isinstance(context.get("untested_functions", []), list) else 0,
    "changed_nodes": len(graph.get("changed_nodes", [])) if isinstance(graph.get("changed_nodes", []), list) else 0,
    "impacted_nodes": len(graph.get("impacted_nodes", [])) if isinstance(graph.get("impacted_nodes", []), list) else 0,
    "edges": len(graph.get("edges", [])) if isinstance(graph.get("edges", []), list) else 0,
}}
_cortex_emit(r)
"""
    )
    raw = _run_bcrg(code, timeout=_remaining_operation_timeout(deadline))
    return _normalize_impact_response(raw, max_results=body.max_results)


@app.post("/impact")
async def impact(body: ImpactBody):
    return await _run_single_flight(
        _impact,
        body,
        timeout=120,
        _wall_timeout=120,
    )


def _large_fn(body: LargeFnBody, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    repo_path = _resolve_repo(body.repo, body.registered_repo)
    code = (
        _tool_preamble(body.storage_key, repo_path)
        + f"""
import json
from better_code_review_graph.tools import find_large_functions

r = find_large_functions(
    min_lines={body.min_lines},
    kind={body.kind!r},
    limit={body.limit},
    repo_root={json.dumps(str(repo_path))},
)
if isinstance(r, str):
    r = json.loads(r)
if not isinstance(r, dict):
    r = {{"status": "error", "error": "invalid large-function response"}}
r["_cortex_totals"] = {{
    "results": len(r.get("results", [])) if isinstance(r.get("results", []), list) else 0
}}
_cortex_emit(r)
"""
    )
    raw = _run_bcrg(code, timeout=_remaining_operation_timeout(deadline))
    bounded, truncated = _bounded_response_value(raw)
    if not isinstance(bounded, dict):
        raise HTTPException(502, "code graph large-function response is not an object")
    bounded["truncated"] = bool(
        truncated or raw.get("_cortex_output_truncated")
    )
    bounded.pop("_cortex_output_truncated", None)
    bounded.pop("_cortex_totals", None)
    return _enforce_response_size(bounded)


@app.post("/large-fn")
async def large_fn(body: LargeFnBody):
    return await _run_single_flight(
        _large_fn,
        body,
        timeout=60,
        _wall_timeout=60,
    )
