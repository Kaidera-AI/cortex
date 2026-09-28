"""Code graph use cases (R17/F09).

Generations are commit/snapshot-bound and immutable once published; a query
always names the generation it was served from, and a HEAD the index has not
seen is reported stale instead of being silently served as current. Extraction
uses the stdlib ``ast`` module only: deterministic, bounded and offline.
Static-analysis limits stay explicit: unresolved dynamic dispatch keeps its
callee key with ``resolved=False``, unparseable and non-Python files are
reported in coverage, and zero hits are only meaningful for the declared
coverage.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

import asyncpg

from ..store import ApiProblem, ScopeContext
from .models import CodeAssessChangeRequest, MemorySearchRequest

EXTRACTOR_PROFILE = "python-ast-v1"
MAX_FILES = 64
MAX_FILE_BYTES = 262_144
TEST_FILE_PATTERN = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$")


class SnapshotTooLarge(Exception):
    """The submitted snapshot exceeds the bounded indexing quota."""


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    symbol_key: str
    symbol_kind: str
    file_path: str
    span_start_line: int
    span_end_line: int
    parent_key: str | None


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    caller_key: str
    edge_kind: str
    callee_key: str
    source_line: int
    resolved: bool


@dataclass(frozen=True, slots=True)
class FileExtraction:
    symbols: tuple[SymbolRecord, ...]
    edges: tuple[EdgeRecord, ...]
    parse_error: str | None


@dataclass(frozen=True, slots=True)
class SnapshotExtraction:
    symbols: tuple[SymbolRecord, ...]
    edges: tuple[EdgeRecord, ...]
    excluded: list[dict[str, str]]
    language_coverage: list[str]


@dataclass(frozen=True, slots=True)
class ResolvedGeneration:
    generation: dict[str, Any] | None
    graph_status: str  # fresh | stale | unavailable


def _dotted_module(file_path: str) -> str:
    stem = file_path[:-3] if file_path.endswith(".py") else file_path
    parts = [part for part in stem.split("/") if part not in ("", "__init__")]
    return ".".join(parts)


def _join_member(base: str, attr: str) -> str:
    """Join a resolved base to a member: file-path bases use the ``::``
    symbol separator, dotted module bases use ``.``."""
    if base.endswith(".py"):
        return f"{base}::{attr}"
    return f"{base}.{attr}"


def extract_python_module(
    file_path: str,
    source: str,
    *,
    module_aliases: dict[str, str] | None = None,
) -> FileExtraction:
    """Extract symbols and edges from one Python file, deterministically."""
    aliases = module_aliases or {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return FileExtraction((), (), f"syntax_error: line {exc.lineno}")

    module_key = file_path
    symbols: list[SymbolRecord] = []
    edges: list[EdgeRecord] = []
    defined: dict[str, str] = {}
    imports: dict[str, str] = {}
    class_keys: set[str] = set()

    last_line = max(
        (getattr(node, "end_lineno", node.lineno) or 1 for node in tree.body),
        default=1,
    )
    symbols.append(
        SymbolRecord(module_key, "module", file_path, 1, max(1, last_line), None)
    )

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            defined[node.name] = f"{module_key}::{node.name}"
            class_keys.add(defined[node.name])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined[node.name] = f"{module_key}::{node.name}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                imports[local] = aliases.get(alias.name, alias.name)
                edges.append(
                    EdgeRecord(
                        module_key, "imports", alias.name, node.lineno, False
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            module = ("." * (node.level or 0)) + (node.module or "")
            if module:
                edges.append(
                    EdgeRecord(module_key, "imports", module, node.lineno, False)
                )
            target_module = aliases.get(node.module or "", node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                if target_module:
                    if node.module in aliases:
                        imports[local] = f"{target_module}::{alias.name}"
                    else:
                        imports[local] = f"{target_module}.{alias.name}"
                else:
                    imports[local] = alias.name

    def add_definitions(body, qual: str, parent_key: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                key = f"{module_key}::{qual}{node.name}"
                class_keys.add(key)
                defined.setdefault(node.name, key)
                symbols.append(
                    SymbolRecord(
                        key,
                        "class",
                        file_path,
                        node.lineno,
                        getattr(node, "end_lineno", node.lineno) or node.lineno,
                        parent_key,
                    )
                )
                edges.append(EdgeRecord(parent_key, "contains", key, node.lineno, True))
                add_definitions(node.body, f"{qual}{node.name}.", key)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"{module_key}::{qual}{node.name}"
                kind = "method" if qual else "function"
                symbols.append(
                    SymbolRecord(
                        key,
                        kind,
                        file_path,
                        node.lineno,
                        getattr(node, "end_lineno", node.lineno) or node.lineno,
                        parent_key,
                    )
                )
                edges.append(EdgeRecord(parent_key, "contains", key, node.lineno, True))

    add_definitions(tree.body, "", module_key)
    local_keys = {symbol.symbol_key for symbol in symbols}

    def resolve_name(name: str) -> str:
        if name in defined:
            return defined[name]
        if name in imports:
            return imports[name]
        return name

    def collect_calls(body, caller_key: str, class_qual: str) -> None:
        local_aliases: dict[str, str] = {}

        def resolve_callee(func) -> str | None:
            if isinstance(func, ast.Name):
                return resolve_name(func.id)
            if isinstance(func, ast.Attribute):
                attr = func.attr
                value = func.value
                if isinstance(value, ast.Name):
                    name = value.id
                    if name == "self" and class_qual:
                        return f"{module_key}::{class_qual}{attr}"
                    if name in local_aliases:
                        return _join_member(local_aliases[name], attr)
                    if name in defined:
                        return _join_member(defined[name], attr)
                    if name in imports:
                        return _join_member(imports[name], attr)
                    return f"{name}.{attr}"
                try:
                    return f"{ast.unparse(value)}.{attr}"[:512]
                except (ValueError, RecursionError):
                    return None
            return None

        for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                target = None
                if isinstance(func, ast.Name):
                    resolved_target = resolve_name(func.id)
                    if resolved_target in class_keys:
                        target = resolved_target
                if target is not None:
                    for assign_target in node.targets:
                        if isinstance(assign_target, ast.Name):
                            local_aliases[assign_target.id] = target
            elif isinstance(node, ast.Call):
                callee_key = resolve_callee(node.func)
                if callee_key is None:
                    continue
                edges.append(
                    EdgeRecord(
                        caller_key,
                        "calls",
                        callee_key,
                        node.lineno,
                        callee_key in local_keys,
                    )
                )

    def visit_functions(body, owner_key: str, class_qual: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit_functions(
                    node.body,
                    f"{module_key}::{class_qual}{node.name}",
                    f"{class_qual}{node.name}.",
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"{module_key}::{class_qual}{node.name}"
                collect_calls(node.body, key, class_qual)

    visit_functions(tree.body, module_key, "")
    for node in tree.body:
        if not isinstance(
            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            collect_calls([node], module_key, "")

    ordered_symbols = tuple(
        sorted(symbols, key=lambda symbol: (symbol.span_start_line, symbol.symbol_key))
    )
    ordered_edges = tuple(
        sorted(
            dict.fromkeys(edges),
            key=lambda edge: (
                edge.caller_key,
                edge.edge_kind,
                edge.callee_key,
                edge.source_line,
            ),
        )
    )
    return FileExtraction(ordered_symbols, ordered_edges, None)


def extract_snapshot(files: Sequence[tuple[str, str]]) -> SnapshotExtraction:
    """Bounded multi-file extraction with explicit coverage exclusions."""
    if len(files) > MAX_FILES:
        raise SnapshotTooLarge(f"snapshots are bounded to {MAX_FILES} files")
    module_aliases: dict[str, str] = {}
    for path, _ in files:
        if (
            path.endswith(".py")
            and not path.startswith("/")
            and ".." not in path.split("/")
        ):
            module_aliases[_dotted_module(path)] = path

    symbols: list[SymbolRecord] = []
    edges: list[EdgeRecord] = []
    excluded: list[dict[str, str]] = []
    for path, source in sorted(files, key=lambda item: item[0]):
        segments = path.replace("\\", "/").split("/")
        if path.startswith(("/", "\\")) or any(
            segment in ("", ".", "..") for segment in segments
        ):
            excluded.append({"path": path, "reason": "invalid_path"})
            continue
        if not path.endswith(".py"):
            excluded.append({"path": path, "reason": "unsupported_language"})
            continue
        if len(source.encode("utf-8")) > MAX_FILE_BYTES:
            excluded.append({"path": path, "reason": "file_too_large"})
            continue
        extraction = extract_python_module(path, source, module_aliases=module_aliases)
        if extraction.parse_error is not None:
            excluded.append({"path": path, "reason": "syntax_error"})
            continue
        symbols.extend(extraction.symbols)
        edges.extend(extraction.edges)

    all_keys = {symbol.symbol_key for symbol in symbols}
    resolved_edges = tuple(
        sorted(
            dict.fromkeys(
                EdgeRecord(
                    edge.caller_key,
                    edge.edge_kind,
                    edge.callee_key,
                    edge.source_line,
                    edge.resolved or edge.callee_key in all_keys,
                )
                for edge in edges
            ),
            key=lambda edge: (
                edge.caller_key,
                edge.edge_kind,
                edge.callee_key,
                edge.source_line,
            ),
        )
    )
    return SnapshotExtraction(
        tuple(
            sorted(
                symbols,
                key=lambda symbol: (
                    symbol.file_path,
                    symbol.span_start_line,
                    symbol.symbol_key,
                ),
            )
        ),
        resolved_edges,
        excluded,
        ["python"],
    )


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


async def resolve_repository(
    connection: asyncpg.Connection, *, scope_id: uuid.UUID, repository_key: str
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """
        SELECT repository.repository_id, repository.repository_key
          FROM cortex_retrieval.code_repositories AS repository
         WHERE repository.scope_id = $1
           AND repository.repository_key = $2
        """,
        scope_id,
        repository_key,
    )
    return dict(row) if row else None


async def publish_extracted(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_key: str,
    commit_sha: str,
    symbols: Sequence[SymbolRecord],
    edges: Sequence[EdgeRecord],
    excluded: Sequence[dict[str, str]],
    language_coverage: Sequence[str] = ("python",),
    extractor_profile: str = EXTRACTOR_PROFILE,
) -> dict[str, Any]:
    """Publish one immutable generation; retire the previously published one."""
    repository_id = uuid.uuid4()
    inserted = await connection.fetchrow(
        """
        INSERT INTO cortex_retrieval.code_repositories
            (scope_id, repository_id, repository_key)
        VALUES ($1, $2, $3)
        ON CONFLICT (scope_id, repository_key) DO NOTHING
        RETURNING repository_id
        """,
        scope_id,
        repository_id,
        repository_key,
    )
    if inserted is None:
        existing = await connection.fetchrow(
            """
            SELECT existing.repository_id
              FROM cortex_retrieval.code_repositories AS existing
             WHERE existing.scope_id = $1
               AND existing.repository_key = $2
            """,
            scope_id,
            repository_key,
        )
        repository_id = existing["repository_id"]
    else:
        repository_id = inserted["repository_id"]

    max_generation = await connection.fetchval(
        """
        SELECT max(generation.generation) AS max_generation
          FROM cortex_retrieval.code_generations AS generation
         WHERE generation.scope_id = $1
           AND generation.repository_id = $2
        """,
        scope_id,
        repository_id,
    )
    generation = (max_generation or 0) + 1
    file_count = sum(1 for symbol in symbols if symbol.symbol_kind == "module")
    await connection.execute(
        """
        INSERT INTO cortex_retrieval.code_generations
            (scope_id, repository_id, generation, commit_sha, extractor_profile,
             language_coverage, status, file_count, excluded_files)
        VALUES ($1, $2, $3, $4, $5, $6, 'building', $7, $8::jsonb)
        """,
        scope_id,
        repository_id,
        generation,
        commit_sha,
        extractor_profile,
        list(language_coverage),
        file_count,
        json.dumps(list(excluded), sort_keys=True),
    )

    symbol_ids = {symbol.symbol_key: uuid.uuid4() for symbol in symbols}
    ordered = sorted(
        symbols,
        key=lambda symbol: (
            symbol.symbol_key.count("::") + symbol.symbol_key.count("."),
            symbol.span_start_line,
            symbol.symbol_key,
        ),
    )
    if ordered:
        await connection.execute(
            """
            INSERT INTO cortex_retrieval.code_symbols
                (scope_id, repository_id, generation, symbol_id, symbol_key,
                 symbol_kind, file_path, span_start_line, span_end_line,
                 parent_symbol_id)
            SELECT $1, $2, $3, candidate.symbol_id, candidate.symbol_key,
                   candidate.symbol_kind, candidate.file_path,
                   candidate.span_start_line, candidate.span_end_line,
                   candidate.parent_symbol_id
              FROM unnest(
                       $4::uuid[], $5::text[], $6::text[], $7::text[],
                       $8::integer[], $9::integer[], $10::uuid[]
                   ) AS candidate(symbol_id, symbol_key, symbol_kind, file_path,
                                  span_start_line, span_end_line, parent_symbol_id)
            """,
            scope_id,
            repository_id,
            generation,
            [symbol_ids[symbol.symbol_key] for symbol in ordered],
            [symbol.symbol_key for symbol in ordered],
            [symbol.symbol_kind for symbol in ordered],
            [symbol.file_path for symbol in ordered],
            [symbol.span_start_line for symbol in ordered],
            [symbol.span_end_line for symbol in ordered],
            [
                symbol_ids.get(symbol.parent_key) if symbol.parent_key else None
                for symbol in ordered
            ],
        )
    edge_rows = [
        (edge, symbol_ids[edge.caller_key])
        for edge in edges
        if edge.caller_key in symbol_ids
    ]
    if edge_rows:
        await connection.execute(
            """
            INSERT INTO cortex_retrieval.code_edges
                (scope_id, repository_id, generation, edge_id, caller_symbol_id,
                 edge_kind, callee_key, callee_symbol_id, source_line)
            SELECT $1, $2, $3, candidate.edge_id, candidate.caller_symbol_id,
                   candidate.edge_kind, candidate.callee_key,
                   candidate.callee_symbol_id, candidate.source_line
              FROM unnest(
                       $4::uuid[], $5::uuid[], $6::text[], $7::text[],
                       $8::uuid[], $9::integer[]
                   ) AS candidate(edge_id, caller_symbol_id, edge_kind,
                                  callee_key, callee_symbol_id, source_line)
            """,
            scope_id,
            repository_id,
            generation,
            [uuid.uuid4() for edge, _ in edge_rows],
            [caller_id for _, caller_id in edge_rows],
            [edge.edge_kind for edge, _ in edge_rows],
            [edge.callee_key[:512] for edge, _ in edge_rows],
            [symbol_ids.get(edge.callee_key) for edge, _ in edge_rows],
            [edge.source_line for edge, _ in edge_rows],
        )

    await connection.execute(
        """
        UPDATE cortex_retrieval.code_generations
           SET status = 'published', published_at = now()
         WHERE scope_id = $1 AND repository_id = $2 AND generation = $3
        """,
        scope_id,
        repository_id,
        generation,
    )
    await connection.execute(
        """
        UPDATE cortex_retrieval.code_generations
           SET status = 'retired'
         WHERE scope_id = $1
           AND repository_id = $2
           AND generation <> $3
           AND status = 'published'
        """,
        scope_id,
        repository_id,
        generation,
    )
    return {
        "repository_id": str(repository_id),
        "generation": generation,
        "commit_sha": commit_sha,
        "extractor_profile": extractor_profile,
        "symbol_count": len(symbols),
        "edge_count": len(edge_rows),
        "excluded": list(excluded),
        "status": "published",
    }


# ---------------------------------------------------------------------------
# Generation resolution and freshness
# ---------------------------------------------------------------------------


def _graph_state(
    generation_row: dict[str, Any] | None, graph_status: str, head_commit: str | None
) -> dict[str, Any]:
    if generation_row is None:
        return {
            "generation": None,
            "commit_sha": None,
            "indexed_commit_sha": None,
            "extractor_profile": EXTRACTOR_PROFILE,
            "status": graph_status,
            "head_commit": head_commit,
            "file_count": None,
            "excluded_files": [],
        }
    excluded = generation_row.get("excluded_files") or []
    if isinstance(excluded, str):
        excluded = json.loads(excluded)
    return {
        "generation": generation_row["generation"],
        "commit_sha": generation_row["commit_sha"],
        "indexed_commit_sha": generation_row["commit_sha"],
        "extractor_profile": generation_row["extractor_profile"],
        "status": graph_status,
        "head_commit": head_commit,
        "file_count": generation_row["file_count"],
        "excluded_files": list(excluded),
    }


async def resolve_generation(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_id: uuid.UUID,
    head_commit: str | None,
) -> ResolvedGeneration:
    if head_commit is not None:
        exact = await connection.fetchrow(
            """
            SELECT generation.scope_id, generation.repository_id,
                   generation.generation, generation.commit_sha,
                   generation.extractor_profile, generation.language_coverage,
                   generation.status, generation.file_count,
                   generation.excluded_files, generation.published_at
              FROM cortex_retrieval.code_generations AS generation
             WHERE generation.scope_id = $1
               AND generation.repository_id = $2
               AND generation.commit_sha = $3
               AND generation.status IN ('published', 'retired')
             ORDER BY (generation.status = 'published') DESC,
                      generation.generation DESC
             LIMIT 1
            """,
            scope_id,
            repository_id,
            head_commit,
        )
        if exact is not None:
            return ResolvedGeneration(dict(exact), "fresh")
    latest = await connection.fetchrow(
        """
        SELECT generation.scope_id, generation.repository_id,
               generation.generation, generation.commit_sha,
               generation.extractor_profile, generation.language_coverage,
               generation.status, generation.file_count,
               generation.excluded_files, generation.published_at
          FROM cortex_retrieval.code_generations AS generation
         WHERE generation.scope_id = $1
           AND generation.repository_id = $2
           AND generation.status = 'published'
         ORDER BY generation.generation DESC
         LIMIT 1
        """,
        scope_id,
        repository_id,
    )
    if latest is None:
        return ResolvedGeneration(None, "unavailable")
    return ResolvedGeneration(
        dict(latest), "stale" if head_commit is not None else "fresh"
    )


def _degraded_for(graph_status: str) -> list[str]:
    if graph_status == "stale":
        return ["code_graph_stale"]
    if graph_status == "unavailable":
        return ["code_graph_not_built"]
    return []


INTERPRETATION = (
    "Impacts are a lower bound for the declared coverage: an absent edge is "
    "not proof of absence, dynamic dispatch and unindexed languages are "
    "explicit limits, and stale or partial coverage never means safe to "
    "change."
)


# ---------------------------------------------------------------------------
# Read use cases
# ---------------------------------------------------------------------------


async def callers_of_symbol(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_id: uuid.UUID,
    generation: int,
    symbol_key: str,
    limit: int,
    head_commit: str | None = None,
) -> dict[str, Any]:
    generation_row = await connection.fetchrow(
        """
        SELECT generation.generation, generation.commit_sha,
               generation.extractor_profile, generation.status,
               generation.file_count, generation.excluded_files
          FROM cortex_retrieval.code_generations AS generation
         WHERE generation.scope_id = $1
           AND generation.repository_id = $2
           AND generation.generation = $3
        """,
        scope_id,
        repository_id,
        generation,
    )
    if generation_row is None:
        return {
            "graph": _graph_state(None, "unavailable", head_commit),
            "callers": [],
            "unresolved_symbols": [symbol_key],
            "degraded": _degraded_for("unavailable"),
            "interpretation": INTERPRETATION,
        }
    graph_status = (
        "fresh"
        if head_commit is None or head_commit == generation_row["commit_sha"]
        else "stale"
    )
    symbol_row = await connection.fetchrow(
        """
        SELECT symbol.symbol_id
          FROM cortex_retrieval.code_symbols AS symbol
         WHERE symbol.scope_id = $1
           AND symbol.repository_id = $2
           AND symbol.generation = $3
           AND symbol.symbol_key = $4
        """,
        scope_id,
        repository_id,
        generation,
        symbol_key,
    )
    graph = _graph_state(dict(generation_row), graph_status, head_commit)
    if symbol_row is None:
        return {
            "graph": graph,
            "callers": [],
            "unresolved_symbols": [symbol_key],
            "degraded": _degraded_for(graph_status),
            "interpretation": INTERPRETATION,
        }
    rows = await connection.fetch(
        """
        SELECT caller.symbol_id AS caller_symbol_id, caller.symbol_key,
               caller.symbol_kind, caller.file_path,
               edge.edge_kind, edge.source_line
          FROM cortex_retrieval.code_edges AS edge
          JOIN cortex_retrieval.code_symbols AS caller
            ON caller.scope_id = edge.scope_id
           AND caller.repository_id = edge.repository_id
           AND caller.generation = edge.generation
           AND caller.symbol_id = edge.caller_symbol_id
         WHERE edge.scope_id = $1
           AND edge.repository_id = $2
           AND edge.generation = $3
           AND edge.callee_symbol_id = $4
           AND edge.edge_kind = 'calls'
         ORDER BY caller.symbol_key, edge.source_line
         LIMIT $5
        """,
        scope_id,
        repository_id,
        generation,
        symbol_row["symbol_id"],
        limit,
    )
    return {
        "graph": graph,
        "callers": [
            {
                "symbol_id": str(row["caller_symbol_id"]),
                "symbol_key": row["symbol_key"],
                "symbol_kind": row["symbol_kind"],
                "file_path": row["file_path"],
                "edge_kind": row["edge_kind"],
                "source_line": row["source_line"],
                "hop": 1,
            }
            for row in rows
        ],
        "unresolved_symbols": [],
        "degraded": _degraded_for(graph_status),
        "interpretation": INTERPRETATION,
    }


async def resolve_change_seeds(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_id: uuid.UUID,
    generation: int,
    changed_files: Sequence[str],
    changed_symbols: Sequence[str],
) -> tuple[list[uuid.UUID], list[str], list[str], list[str]]:
    """Resolve changed files/symbols to graph nodes; report what did not
    resolve instead of treating it as unaffected."""
    rows = await connection.fetch(
        """
        SELECT symbol.symbol_id, symbol.symbol_key, symbol.file_path
          FROM cortex_retrieval.code_symbols AS symbol
         WHERE symbol.scope_id = $1
           AND symbol.repository_id = $2
           AND symbol.generation = $3
           AND (symbol.file_path = ANY($4::text[])
                OR symbol.symbol_key = ANY($5::text[]))
         ORDER BY symbol.symbol_key
        """,
        scope_id,
        repository_id,
        generation,
        list(changed_files),
        list(changed_symbols),
    )
    seed_ids = [row["symbol_id"] for row in rows]
    seed_keys = [row["symbol_key"] for row in rows]
    matched_files = {row["file_path"] for row in rows}
    matched_keys = {row["symbol_key"] for row in rows}
    unresolved_files = [
        path for path in changed_files if path not in matched_files
    ]
    unresolved_symbols = [
        key for key in changed_symbols if key not in matched_keys
    ]
    return seed_ids, seed_keys, unresolved_files, unresolved_symbols


async def assess_impact(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_id: uuid.UUID,
    generation_row: dict[str, Any] | None,
    graph_status: str,
    seed_symbol_ids: Sequence[uuid.UUID],
    seed_symbol_keys: Sequence[str],
    unresolved_files: Sequence[str],
    unresolved_symbols: Sequence[str],
    max_hops: int,
    limit: int,
    head_commit: str | None,
) -> dict[str, Any]:
    """Bounded reverse-dependency traversal (callers/importers) with hop
    counts and edge evidence."""
    graph = _graph_state(generation_row, graph_status, head_commit)
    base = {
        "graph": graph,
        "direct_impacts": [],
        "transitive_impacts": [],
        "unresolved_files": list(unresolved_files),
        "unresolved_symbols": list(unresolved_symbols),
        "truncated": False,
        "degraded": _degraded_for(graph_status),
        "interpretation": INTERPRETATION,
    }
    if generation_row is None or graph_status == "unavailable":
        return base

    generation = generation_row["generation"]
    seeds = set(seed_symbol_ids)
    visited: dict[uuid.UUID, dict[str, Any]] = {}
    frontier = list(seed_symbol_ids)
    truncated = False
    for hop in range(1, max_hops + 1):
        if not frontier:
            break
        rows = await connection.fetch(
            """
            SELECT edge.caller_symbol_id, caller.symbol_key, caller.symbol_kind,
                   caller.file_path, edge.edge_kind, edge.source_line,
                   edge.callee_symbol_id
              FROM cortex_retrieval.code_edges AS edge
              JOIN cortex_retrieval.code_symbols AS caller
                ON caller.scope_id = edge.scope_id
               AND caller.repository_id = edge.repository_id
               AND caller.generation = edge.generation
               AND caller.symbol_id = edge.caller_symbol_id
             WHERE edge.scope_id = $1
               AND edge.repository_id = $2
               AND edge.generation = $3
               AND edge.callee_symbol_id = ANY($4::uuid[])
               AND edge.edge_kind IN ('calls', 'imports')
             ORDER BY caller.symbol_key, edge.source_line
            """,
            scope_id,
            repository_id,
            generation,
            frontier,
        )
        next_frontier: list[uuid.UUID] = []
        for row in rows:
            caller_id = row["caller_symbol_id"]
            if caller_id in seeds or caller_id in visited:
                if caller_id in visited:
                    via = visited[caller_id]["via"]
                    entry = {
                        "edge_kind": row["edge_kind"],
                        "source_line": row["source_line"],
                        "callee_symbol_id": str(row["callee_symbol_id"]),
                    }
                    if entry not in via:
                        via.append(entry)
                continue
            if len(visited) >= limit:
                truncated = True
                continue
            visited[caller_id] = {
                "symbol_id": str(caller_id),
                "symbol_key": row["symbol_key"],
                "symbol_kind": row["symbol_kind"],
                "file_path": row["file_path"],
                "hop": hop,
                "via": [
                    {
                        "edge_kind": row["edge_kind"],
                        "source_line": row["source_line"],
                        "callee_symbol_id": str(row["callee_symbol_id"]),
                    }
                ],
            }
            next_frontier.append(caller_id)
        frontier = next_frontier
    nodes = sorted(
        visited.values(), key=lambda node: (node["hop"], node["symbol_key"])
    )
    base["direct_impacts"] = [node for node in nodes if node["hop"] == 1]
    base["transitive_impacts"] = [node for node in nodes if node["hop"] > 1]
    base["truncated"] = truncated
    base["changed_symbols"] = list(seed_symbol_keys)
    return base


async def annotations_for_symbols(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_id: uuid.UUID,
    symbol_keys: Sequence[str],
) -> list[dict[str, Any]]:
    if not symbol_keys:
        return []
    rows = await connection.fetch(
        """
        SELECT annotation.symbol_key, annotation.annotation,
               annotation.author_principal_id, annotation.created_at
          FROM cortex_retrieval.code_annotations AS annotation
         WHERE annotation.scope_id = $1
           AND annotation.repository_id = $2
           AND annotation.symbol_key = ANY($3::text[])
         ORDER BY annotation.symbol_key, annotation.created_at
        """,
        scope_id,
        repository_id,
        list(symbol_keys),
    )
    return [
        {
            "symbol_key": row["symbol_key"],
            "annotation": row["annotation"],
            "author_principal_id": str(row["author_principal_id"]),
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


async def blast_radius(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_id: uuid.UUID,
    generation_row: dict[str, Any] | None,
    graph_status: str,
    seed_symbol_ids: Sequence[uuid.UUID],
    seed_symbol_keys: Sequence[str],
    unresolved_files: Sequence[str],
    unresolved_symbols: Sequence[str],
    max_hops: int,
    limit: int,
    head_commit: str | None,
) -> dict[str, Any]:
    impact = await assess_impact(
        connection,
        scope_id=scope_id,
        repository_id=repository_id,
        generation_row=generation_row,
        graph_status=graph_status,
        seed_symbol_ids=seed_symbol_ids,
        seed_symbol_keys=seed_symbol_keys,
        unresolved_files=unresolved_files,
        unresolved_symbols=unresolved_symbols,
        max_hops=max_hops,
        limit=limit,
        head_commit=head_commit,
    )
    impacted = impact["direct_impacts"] + impact["transitive_impacts"]
    keys = list(seed_symbol_keys) + [node["symbol_key"] for node in impacted]
    impact["annotations"] = await annotations_for_symbols(
        connection,
        scope_id=scope_id,
        repository_id=repository_id,
        symbol_keys=sorted(set(keys)),
    )
    impact["files_affected"] = sorted(
        {node["file_path"] for node in impacted}
    )
    impact["symbol_count"] = len(impacted)
    return impact


async def hotspots(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_id: uuid.UUID,
    generation: int,
    limit: int,
    head_commit: str | None = None,
    graph_status: str = "fresh",
    generation_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = await connection.fetch(
        """
        SELECT symbol.symbol_id, symbol.symbol_key, symbol.symbol_kind,
               symbol.file_path, count(edge.edge_id) AS fan_in
          FROM cortex_retrieval.code_symbols AS symbol
          LEFT JOIN cortex_retrieval.code_edges AS edge
            ON edge.scope_id = symbol.scope_id
           AND edge.repository_id = symbol.repository_id
           AND edge.generation = symbol.generation
           AND edge.callee_symbol_id = symbol.symbol_id
           AND edge.edge_kind = 'calls'
         WHERE symbol.scope_id = $1
           AND symbol.repository_id = $2
           AND symbol.generation = $3
           AND symbol.symbol_kind <> 'module'
         GROUP BY symbol.symbol_id, symbol.symbol_key, symbol.symbol_kind,
                  symbol.file_path
         ORDER BY fan_in DESC, symbol.symbol_key
         LIMIT $4
        """,
        scope_id,
        repository_id,
        generation,
        limit,
    )
    return {
        "graph": _graph_state(generation_row, graph_status, head_commit),
        "hotspots": [
            {
                "symbol_id": str(row["symbol_id"]),
                "symbol_key": row["symbol_key"],
                "symbol_kind": row["symbol_kind"],
                "file_path": row["file_path"],
                "fan_in": int(row["fan_in"]),
            }
            for row in rows
        ],
        "interpretation": (
            "fan_in counts static call edges inside the indexed generation; "
            "dynamic dispatch can only raise real coupling, never lower it."
        ),
    }


async def explore_code(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_id: uuid.UUID,
    generation_row: dict[str, Any],
    graph_status: str,
    symbol_keys: Sequence[str],
    edge_kinds: Sequence[str],
    direction: str,
    hops: int,
    limit: int,
    head_commit: str | None,
) -> dict[str, Any]:
    generation = generation_row["generation"]
    seeds = await connection.fetch(
        """
        SELECT symbol.symbol_id, symbol.symbol_key, symbol.symbol_kind,
               symbol.file_path
          FROM cortex_retrieval.code_symbols AS symbol
         WHERE symbol.scope_id = $1
           AND symbol.repository_id = $2
           AND symbol.generation = $3
           AND symbol.symbol_key = ANY($4::text[])
        """,
        scope_id,
        repository_id,
        generation,
        list(symbol_keys),
    )
    nodes: dict[uuid.UUID, dict[str, Any]] = {
        row["symbol_id"]: {
            "symbol_id": str(row["symbol_id"]),
            "symbol_key": row["symbol_key"],
            "symbol_kind": row["symbol_kind"],
            "file_path": row["file_path"],
        }
        for row in seeds
    }
    unresolved = [
        key
        for key in symbol_keys
        if key not in {row["symbol_key"] for row in seeds}
    ]
    edges: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    expanded: set[uuid.UUID] = set()
    frontier = list(nodes)
    kinds = list(edge_kinds) or ["calls", "imports", "contains"]
    for _ in range(max(1, hops)):
        if not frontier or len(edges) >= limit:
            break
        if direction in ("out", "both"):
            rows = await connection.fetch(
                """
                SELECT edge.caller_symbol_id, caller.symbol_key AS caller_key,
                       edge.edge_kind, edge.callee_key, edge.callee_symbol_id,
                       edge.source_line, callee.symbol_key AS resolved_key,
                       callee.symbol_kind AS resolved_kind,
                       callee.file_path AS resolved_path
                  FROM cortex_retrieval.code_edges AS edge
                  JOIN cortex_retrieval.code_symbols AS caller
                    ON caller.scope_id = edge.scope_id
                   AND caller.repository_id = edge.repository_id
                   AND caller.generation = edge.generation
                   AND caller.symbol_id = edge.caller_symbol_id
                  LEFT JOIN cortex_retrieval.code_symbols AS callee
                    ON callee.scope_id = edge.scope_id
                   AND callee.repository_id = edge.repository_id
                   AND callee.generation = edge.generation
                   AND callee.symbol_id = edge.callee_symbol_id
                 WHERE edge.scope_id = $1
                   AND edge.repository_id = $2
                   AND edge.generation = $3
                   AND edge.caller_symbol_id = ANY($4::uuid[])
                   AND edge.edge_kind = ANY($5::text[])
                 ORDER BY caller.symbol_key, edge.source_line
                 LIMIT $6
                """,
                scope_id,
                repository_id,
                generation,
                frontier,
                kinds,
                limit,
            )
            _absorb_explore_rows(rows, nodes, edges, "caller_symbol_id", "caller_key",
                                 "callee_symbol_id", "resolved_key")
        if direction in ("in", "both"):
            rows = await connection.fetch(
                """
                SELECT edge.caller_symbol_id, caller.symbol_key AS caller_key,
                       edge.edge_kind, edge.callee_key, edge.callee_symbol_id,
                       edge.source_line, callee.symbol_key AS resolved_key,
                       callee.symbol_kind AS resolved_kind,
                       callee.file_path AS resolved_path
                  FROM cortex_retrieval.code_edges AS edge
                  JOIN cortex_retrieval.code_symbols AS caller
                    ON caller.scope_id = edge.scope_id
                   AND caller.repository_id = edge.repository_id
                   AND caller.generation = edge.generation
                   AND caller.symbol_id = edge.caller_symbol_id
                  LEFT JOIN cortex_retrieval.code_symbols AS callee
                    ON callee.scope_id = edge.scope_id
                   AND callee.repository_id = edge.repository_id
                   AND callee.generation = edge.generation
                   AND callee.symbol_id = edge.callee_symbol_id
                 WHERE edge.scope_id = $1
                   AND edge.repository_id = $2
                   AND edge.generation = $3
                   AND edge.callee_symbol_id = ANY($4::uuid[])
                   AND edge.edge_kind = ANY($5::text[])
                 ORDER BY caller.symbol_key, edge.source_line
                 LIMIT $6
                """,
                scope_id,
                repository_id,
                generation,
                frontier,
                kinds,
                limit,
            )
        expanded.update(frontier)
        frontier = [
            symbol_id for symbol_id in nodes if symbol_id not in expanded
        ]
    return {
        "kind": "code",
        "graph": _graph_state(generation_row, graph_status, head_commit),
        "nodes": sorted(nodes.values(), key=lambda node: node["symbol_key"]),
        "edges": sorted(
            edges.values(),
            key=lambda edge: (
                edge["caller_key"],
                edge["edge_kind"],
                edge["callee_key"],
            ),
        ),
        "unresolved_symbols": unresolved,
        "degraded": _degraded_for(graph_status),
        "interpretation": INTERPRETATION,
    }


def _absorb_explore_rows(rows, nodes, edges, caller_id_key, caller_key_key,
                         callee_id_key, callee_key_key) -> None:
    for row in rows:
        caller_id = row[caller_id_key]
        if caller_id not in nodes:
            nodes[caller_id] = {
                "symbol_id": str(caller_id),
                "symbol_key": row[caller_key_key],
                "symbol_kind": None,
                "file_path": None,
            }
        callee_id = row[callee_id_key]
        if callee_id is not None and callee_id not in nodes:
            nodes[callee_id] = {
                "symbol_id": str(callee_id),
                "symbol_key": row[callee_key_key],
                "symbol_kind": row["resolved_kind"],
                "file_path": row["resolved_path"],
            }
        identity = (
            str(caller_id),
            row["edge_kind"],
            row["callee_key"],
            row["source_line"],
        )
        edges[identity] = {
            "caller_symbol_id": str(caller_id),
            "caller_key": row[caller_key_key],
            "edge_kind": row["edge_kind"],
            "callee_key": row["callee_key"],
            "callee_symbol_id": str(callee_id) if callee_id else None,
            "resolved": callee_id is not None,
            "source_line": row["source_line"],
        }


# ---------------------------------------------------------------------------
# Change assessment (code.assess-change building block)
# ---------------------------------------------------------------------------


def change_digest(
    scope_id: uuid.UUID, payload: CodeAssessChangeRequest
) -> bytes:
    canonical = json.dumps(
        {
            "scope_id": str(scope_id),
            "repository_key": payload.repository_key,
            "base_commit": payload.base_commit,
            "target_commit": payload.target_commit,
            "changed_files": sorted(payload.changed_files or []),
            "changed_symbols": sorted(payload.changed_symbols or []),
            "max_hops": payload.max_hops,
            "limit": payload.limit,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


async def find_assessment(
    connection: asyncpg.Connection, *, scope_id: uuid.UUID, digest: bytes
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """
        SELECT assessment.assessment_id, assessment.result
          FROM cortex_retrieval.code_assessments AS assessment
         WHERE assessment.scope_id = $1
           AND assessment.change_digest = $2
        """,
        scope_id,
        digest,
    )
    if row is None:
        return None
    result = row["result"]
    if isinstance(result, str):
        result = json.loads(result)
    return result


async def store_assessment(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    repository_id: uuid.UUID,
    digest: bytes,
    generation: int | None,
    graph_status: str,
    result: dict[str, Any],
) -> uuid.UUID:
    assessment_id = uuid.uuid4()
    await connection.execute(
        """
        INSERT INTO cortex_retrieval.code_assessments
            (scope_id, assessment_id, repository_id, change_digest,
             generation, graph_status, result)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (scope_id, change_digest) DO NOTHING
        """,
        scope_id,
        assessment_id,
        repository_id,
        digest,
        generation,
        graph_status,
        json.dumps(result, ensure_ascii=False, sort_keys=True),
    )
    return assessment_id


async def build_assessment(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CodeAssessChangeRequest,
    planner: Any,
) -> dict[str, Any]:
    """Compose the change-impact assessment (worker-api §5): generation and
    coverage, structural impacts and callers, related memories through scoped
    search, verification candidates, unknowns and whitelisted next actions."""
    scope_id = context.selected.scope_id
    repository = await resolve_repository(
        connection, scope_id=scope_id, repository_key=payload.repository_key
    )
    if repository is None:
        raise ApiProblem(
            404,
            "repository_not_found",
            "The code repository is unavailable in the selected scope.",
        )
    resolved = await resolve_generation(
        connection,
        scope_id=scope_id,
        repository_id=repository["repository_id"],
        head_commit=payload.target_commit,
    )
    generation_row = resolved.generation
    graph_status = resolved.graph_status
    graph = _graph_state(generation_row, graph_status, payload.target_commit)

    unknowns: list[str] = []
    degraded = list(_degraded_for(graph_status))
    impacts: dict[str, Any] = {
        "graph": graph,
        "direct_impacts": [],
        "transitive_impacts": [],
        "unresolved_files": list(payload.changed_files or []),
        "unresolved_symbols": list(payload.changed_symbols or []),
        "truncated": False,
        "degraded": degraded,
        "interpretation": INTERPRETATION,
    }
    if generation_row is not None:
        seed_ids, seed_keys, unresolved_files, unresolved_symbols = (
            await resolve_change_seeds(
                connection,
                scope_id=scope_id,
                repository_id=repository["repository_id"],
                generation=generation_row["generation"],
                changed_files=payload.changed_files or [],
                changed_symbols=payload.changed_symbols or [],
            )
        )
        impacts = await assess_impact(
            connection,
            scope_id=scope_id,
            repository_id=repository["repository_id"],
            generation_row=generation_row,
            graph_status=graph_status,
            seed_symbol_ids=seed_ids,
            seed_symbol_keys=seed_keys,
            unresolved_files=unresolved_files,
            unresolved_symbols=unresolved_symbols,
            max_hops=payload.max_hops,
            limit=payload.limit,
            head_commit=payload.target_commit,
        )
        degraded = list(impacts["degraded"])
        excluded = graph["excluded_files"]
        if excluded:
            unknowns.append(
                "indexed generation excluded files: "
                + ", ".join(sorted({item["path"] for item in excluded}))
            )
        if unresolved_files or unresolved_symbols:
            unknowns.append(
                "changed inputs not present in the indexed generation"
            )
    else:
        unknowns.append("no published code generation exists for this repository")

    if graph_status == "stale":
        unknowns.append(
            f"indexed commit {graph['indexed_commit_sha']} differs from the "
            f"requested head {payload.target_commit}"
        )

    related_memories: list[dict[str, Any]] = []
    memory_degraded: list[str] = []
    terms = list(payload.changed_symbols or []) + [
        path.rsplit("/", 1)[-1] for path in (payload.changed_files or [])
    ]
    if terms:
        query = " ".join(terms)[:512]
        search = await planner.search(
            connection,
            context,
            MemorySearchRequest(
                query=query,
                intent="symbol",
                limit=5,
                graph_hops=0,
                deadline_ms=max(200, payload.deadline_ms // 2),
            ),
        )
        related_memories = [
            {
                "citation": hit["citation"],
                "content_class": hit["content_class"],
                "status": hit["status"],
                "excerpt": hit["excerpt"],
                "stages": hit["stages"],
            }
            for hit in search["hits"]
        ]
        memory_degraded = list(search["degraded"])
    else:
        unknowns.append(
            "no changed symbols or files were supplied for related-memory search"
        )

    impacted = impacts["direct_impacts"] + impacts["transitive_impacts"]
    verification_candidates = [
        {
            "symbol_key": node["symbol_key"],
            "file_path": node["file_path"],
            "hop": node["hop"],
            "evidence_kind": "dependency",
        }
        for node in impacted
        if TEST_FILE_PATTERN.search(node["file_path"])
    ]

    if graph_status == "unavailable":
        status = "unavailable"
    elif graph_status == "stale":
        status = "stale"
    elif (
        impacts["unresolved_files"]
        or impacts["unresolved_symbols"]
        or graph["excluded_files"]
        or impacts["truncated"]
    ):
        status = "partial"
    else:
        status = "complete-for-declared-coverage"

    suggested_actions: list[dict[str, Any]] = []
    if graph_status in ("stale", "unavailable"):
        suggested_actions.append(
            {
                "operation_id": "code.publish-index",
                "reason": (
                    "publish an index generation for the requested head commit "
                    "before relying on this assessment"
                ),
                "arguments": {"repository_key": payload.repository_key},
            }
        )
    for node in impacted[:3]:
        suggested_actions.append(
            {
                "operation_id": "code.callers",
                "reason": f"inspect direct callers of {node['symbol_key']}",
                "arguments": {
                    "repository_key": payload.repository_key,
                    "symbol_key": node["symbol_key"],
                },
            }
        )
    for memory in related_memories[:3]:
        suggested_actions.append(
            {
                "operation_id": "memory.inspect-hit",
                "reason": "verify the original before relying on this memory",
                "arguments": {"citation": memory["citation"]},
            }
        )

    digest = change_digest(scope_id, payload)
    result = {
        "assessment_ref": f"assess:{digest.hex()[:32]}",
        "change_digest": digest.hex(),
        "repository_key": payload.repository_key,
        "repository_id": str(repository["repository_id"]),
        "base_commit": payload.base_commit,
        "target_commit": payload.target_commit,
        "status": status,
        "graph": graph,
        "coverage": {
            "indexed_commit": graph["indexed_commit_sha"],
            "generation": graph["generation"],
            "languages": ["python"],
            "file_count": graph["file_count"],
            "excluded_files": graph["excluded_files"],
        },
        "direct_impacts": impacts["direct_impacts"],
        "transitive_impacts": impacts["transitive_impacts"],
        "callers": impacts["direct_impacts"],
        "related_memories": related_memories,
        "verification_candidates": verification_candidates,
        "unknowns": unknowns,
        "degraded": degraded + [f"memory:{item}" for item in memory_degraded],
        "interpretation": INTERPRETATION,
        "suggested_actions": suggested_actions,
    }
    await store_assessment(
        connection,
        scope_id=scope_id,
        repository_id=repository["repository_id"],
        digest=digest,
        generation=generation_row["generation"] if generation_row else None,
        graph_status=graph_status,
        result=result,
    )
    return result


__all__ = [
    "ApiProblem",
    "EXTRACTOR_PROFILE",
    "INTERPRETATION",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "EdgeRecord",
    "FileExtraction",
    "ResolvedGeneration",
    "SnapshotExtraction",
    "SnapshotTooLarge",
    "SymbolRecord",
    "annotations_for_symbols",
    "assess_impact",
    "blast_radius",
    "build_assessment",
    "callers_of_symbol",
    "change_digest",
    "explore_code",
    "extract_python_module",
    "extract_snapshot",
    "find_assessment",
    "hotspots",
    "publish_extracted",
    "resolve_change_seeds",
    "resolve_generation",
    "resolve_repository",
    "store_assessment",
]
