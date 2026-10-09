"""Bounded PG graph beneath C03/C04/C07/C11; no header or filesystem authority.

Canonical hydration, facts, durable jobs and the existing extraction/build adapters
are injected Core ports. Missing write/execution ports are explicitly unavailable.
"""
import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import json
from functools import wraps
from uuid import UUID, uuid4, uuid5

import asyncpg
from cortex_core.embeddings.pg_search import CapabilityUnavailable, PostgresSearch, Scope

HIGH = ("concept", "epic", "service", "project", "product", "work_product")
LOW = ("file", "tool", "endpoint", "table", "branch", "model", "agent")
KINDS = {"decision": "decisions", "lesson": "lessons", "knowledge": "knowledge",
         "work_product": "work_products", "code": "code"}
ACTIVE = """WITH active AS (
 SELECT a.* FROM retrieval.graph_applied a
 JOIN retrieval.graph_state s ON (s.tenant_id,s.project_id,s.active_generation)=(a.tenant_id,a.project_id,a.generation)
 JOIN core.records r ON (r.tenant_id,r.project_id,r.id)=(a.tenant_id,a.project_id,a.record_id)
 WHERE a.tenant_id=$1 AND a.project_id=$2
 AND r.current_revision=a.source_revision AND NOT r.tombstone
 AND r.kind=a.source_kind
), nodes AS (
 SELECT n.name,min(n.entity_type) AS entity_type,min(n.description) AS description,
        count(DISTINCT a.record_id)::int AS source_count,max(a.applied_at)::text AS updated_at
 FROM active a JOIN retrieval.graph_nodes n USING(tenant_id,project_id,generation,record_id)
 GROUP BY n.name
), edges AS (
 SELECT e.source,e.target,e.relationship_type,min(e.description) AS description
 FROM active a JOIN retrieval.graph_edges e USING(tenant_id,project_id,generation,record_id)
 GROUP BY e.source,e.target,e.relationship_type
) """


@dataclass(frozen=True)
class GraphScope(Scope):
    project_key: str
    repo: str


@dataclass(frozen=True)
class Source:
    record_id: UUID
    revision: int
    kind: str
    label: str
    description: str
    content: str


@dataclass(frozen=True)
class SourceRef:
    record_id: UUID
    revision: int
    kind: str


@dataclass(frozen=True)
class Node:
    name: str
    entity_type: str
    description: str = ""


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    relationship_type: str
    description: str = ""


@dataclass(frozen=True)
class Extraction:
    identity: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]


class GraphUnavailable(RuntimeError):
    code = "capability_unavailable"
    capability = "graph"


class StaleGraph(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def retry(operation):
    @wraps(operation)
    async def wrapped(*args, **kwargs):
        for attempt in range(3):
            try:
                return await operation(*args, **kwargs)
            except asyncpg.SerializationError:
                if attempt == 2:
                    raise GraphUnavailable("concurrent_update") from None
                await asyncio.sleep(0.005 * (attempt + 1))
    return wrapped


def bounded_int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Integer outside graph bounds")
    return value


def text(value, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError("Invalid bounded graph text")
    return value


class PostgresGraph:
    def __init__(self, pool, *, authorize_read, authorize_control, authorize_writer,
                 extractor_identity, source_reader=None, extractor=None, fact_sink=None,
                 enqueue_job=None, read_job=None, build_executor=None):
        self.adapters = {op: PostgresSearch(pool, auth) for op, auth in
                         [("read", authorize_read), ("control", authorize_control), ("writer", authorize_writer)]}
        self.identity = text(extractor_identity, 512)
        self.source_reader, self.extractor, self.fact_sink = source_reader, extractor, fact_sink
        self.enqueue_job, self.read_job, self.build_executor = enqueue_job, read_job, build_executor

    @asynccontextmanager
    async def _tx(self, subject, operation="read", project=None):
        try:
            async with asyncio.timeout(2):
                async with self.adapters[operation]._request(subject) as (conn, scope):
                    if (not isinstance(scope, GraphScope) or not isinstance(scope.project_key, str)
                        or not scope.project_key.strip() or not isinstance(scope.repo, str) or not scope.repo.strip()):
                        raise PermissionError("Core registry scope required")
                    if project is not None and project != scope.project_key:
                        raise PermissionError("Project selector does not match authorized scope")
                    yield conn, scope
        except TimeoutError:
            raise GraphUnavailable("resource_timeout") from None
        except CapabilityUnavailable:
            raise GraphUnavailable("resource_timeout") from None
        except asyncpg.UndefinedTableError:
            raise GraphUnavailable("schema_unavailable") from None

    async def _state(self, conn, scope, *, lock=False):
        row = await conn.fetchrow("""SELECT s.active_generation,s.state,g.extractor_identity
            FROM retrieval.graph_state s JOIN retrieval.graph_generations g
            ON (g.tenant_id,g.project_id,g.id)=(s.tenant_id,s.project_id,s.active_generation)
            WHERE s.tenant_id=$1 AND s.project_id=$2""" + (" FOR UPDATE OF s" if lock else ""),
            scope.tenant_id, scope.project_id)
        if row is None:
            raise GraphUnavailable("not_configured")
        if row["state"] != "ready":
            raise GraphUnavailable(row["state"])
        if row["extractor_identity"] != self.identity:
            raise GraphUnavailable("identity_mismatch")
        return row["active_generation"]

    @retry
    async def configure(self, subject, state, *, project=None):
        if state not in {"ready", "disabled", "rebuilding"}:
            raise ValueError("Invalid graph state")
        generation = uuid4()
        async with self._tx(subject, "control", project) as (conn, scope):
            await conn.execute("INSERT INTO retrieval.graph_generations(tenant_id,project_id,id,extractor_identity) VALUES($1,$2,$3,$4)", scope.tenant_id, scope.project_id, generation, self.identity)
            await conn.execute("""INSERT INTO retrieval.graph_state VALUES($1,$2,$3,$4)
                ON CONFLICT(tenant_id,project_id) DO UPDATE SET active_generation=EXCLUDED.active_generation,state=EXCLUDED.state""", scope.tenant_id, scope.project_id, generation, state)
        return generation

    async def _stats(self, conn, scope, generation):
        counts = await conn.fetchrow(ACTIVE + "SELECT (SELECT count(*) FROM nodes)::int AS n,(SELECT count(*) FROM edges)::int AS e", scope.tenant_id, scope.project_id)
        rows = await conn.fetch("""SELECT r.kind,count(*)::int AS total,
            count(*) FILTER(WHERE a.record_id IS NULL)::int AS pending
            FROM core.records r LEFT JOIN retrieval.graph_applied a ON
             (a.tenant_id,a.project_id,a.generation,a.record_id,a.source_revision)=
             (r.tenant_id,r.project_id,$3,r.id,r.current_revision) AND a.source_kind=r.kind
            WHERE r.tenant_id=$1 AND r.project_id=$2 AND NOT r.tombstone AND r.kind=ANY($4::text[])
            GROUP BY r.kind""", scope.tenant_id, scope.project_id, generation, list(KINDS))
        source_counts = {v: 0 for k, v in KINDS.items() if k != "code"}
        backlog = dict(source_counts)
        total = pending = 0
        for row in rows:
            total += row["total"]
            pending += row["pending"]
            if row["kind"] != "code":
                source_counts[KINDS[row["kind"]]] = row["total"]
                backlog[KINDS[row["kind"]]] = row["pending"]
        return {"project": scope.project_key, "entity_count": counts["n"], "relationship_count": counts["e"],
                "source_counts": source_counts, "backlog": backlog,
                "freshness": {"state": "lagging" if pending else "current", "pending_records": pending,
                              "indexed_records": total-pending, "generation": str(generation), "extractor_identity": self.identity}}

    @retry
    async def stats(self, subject, *, project=None):
        async with self._tx(subject, project=project) as (conn, scope):
            generation = await self._state(conn, scope)
            return await self._stats(conn, scope, generation)

    @retry
    async def repository_stats(self, subject, *, project=None):
        async with self._tx(subject, project=project) as (conn, scope):
            generation = await self._state(conn, scope)
            stats = await self._stats(conn, scope, generation)
            n, e = stats["entity_count"], stats["relationship_count"]
            return {"total_nodes": n, "total_edges": e, "repos": [{"name": scope.project_key,
                "nodes": n, "edges": e, "path": f"pg://{scope.tenant_id}/{scope.project_id}/{generation}"}],
                "freshness": stats["freshness"]}

    def _node(self, scope, row):
        return {**dict(row), "id": row["name"], "label": row["name"],
                "entity_id": str(uuid5(scope.project_id, row["name"]))}

    def _edge(self, scope, row, types):
        return {**dict(row), "id": str(uuid5(scope.project_id, repr((row["source"], row["target"], row["relationship_type"])))),
                "source_type": types[row["source"]], "target_type": types[row["target"]],
                "source_entity_id": str(uuid5(scope.project_id, row["source"])),
                "target_entity_id": str(uuid5(scope.project_id, row["target"]))}

    @retry
    async def memory(self, subject, *, limit=500, project=None):
        bounded_int(limit, 1, 1000)
        async with self._tx(subject, project=project) as (conn, scope):
            generation = await self._state(conn, scope)
            rows = await conn.fetch(ACTIVE + "SELECT * FROM nodes ORDER BY name LIMIT $3", scope.tenant_id, scope.project_id, limit+1)
            truncated = len(rows) > limit
            nodes = [self._node(scope, r) for r in rows[:limit]]
            names, types = [n["name"] for n in nodes], {n["name"]: n["entity_type"] for n in nodes}
            rows = await conn.fetch(ACTIVE + "SELECT * FROM edges WHERE source=ANY($3::text[]) AND target=ANY($3::text[]) ORDER BY source,target,relationship_type LIMIT 3001", scope.tenant_id, scope.project_id, names)
            truncated |= len(rows) > 3000
            edges = [self._edge(scope, r, types) for r in rows[:3000]]
            refs = await conn.fetch(ACTIVE + """SELECT DISTINCT a.record_id,a.source_kind,a.source_label,a.source_description,
                a.applied_at::text AS updated_at,n.name FROM active a JOIN retrieval.graph_nodes n
                USING(tenant_id,project_id,generation,record_id) WHERE n.name=ANY($3::text[])
                ORDER BY a.record_id,n.name LIMIT 3001""", scope.tenant_id, scope.project_id, names)
            truncated |= len(refs) > 3000
            sources, source_edges = {}, []
            for row in refs[:3000]:
                table = KINDS.get(row["source_kind"], row["source_kind"])
                key = f"source:{table}:{row['record_id']}"
                if key not in sources and len(sources) == 1000:
                    truncated = True
                    continue
                sources[key] = {"id": key, "source_id": str(row["record_id"]), "source_table": table,
                    "source_type": row["source_kind"], "label": row["source_label"],
                    "description": row["source_description"], "updated_at": row["updated_at"]}
                source_edges.append({"id": f"source-edge:{table}:{row['record_id']}:{row['name']}",
                    "source": key, "target": row["name"], "relationship_type": "extracted_entity"})
            return {"project": scope.project_key, "nodes": nodes, "edges": edges, "sources": list(sources.values()),
                    "source_edges": source_edges, "truncated": truncated,
                    "freshness": (await self._stats(conn, scope, generation))["freshness"]}

    @retry
    async def search(self, subject, query, *, limit=100, expand=False, high=False, low=False, depth=1, project=None):
        text(query, 512)
        tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_.:/-]+", query) if len(t)>2] or [query.lower()]
        bounded_int(limit, 1, 1000)
        bounded_int(depth, 0, 3)
        mode = "high" if high and not low else "low" if low and not high else "both"
        types_allowed = list(HIGH if mode == "high" else LOW if mode == "low" else HIGH+LOW)
        async with self._tx(subject, project=project) as (conn, scope):
            generation = await self._state(conn, scope)
            seeds = await conn.fetch(ACTIVE + """SELECT * FROM nodes WHERE
                EXISTS(SELECT 1 FROM unnest($3::text[]) token WHERE strpos(lower(name || ' ' || description),token)>0)
                AND entity_type=ANY($4::text[])
                ORDER BY name LIMIT $5""", scope.tenant_id, scope.project_id, tokens, types_allowed, limit+1)
            truncated = len(seeds) > limit
            selected = {r["name"]: r for r in seeds[:limit]}
            frontier, relationships = list(selected), {}
            for _ in range(depth if expand else 0):
                rows = await conn.fetch(ACTIVE + """SELECT * FROM edges WHERE source=ANY($3::text[]) OR target=ANY($3::text[])
                    ORDER BY source,target,relationship_type LIMIT 3001""", scope.tenant_id, scope.project_id, frontier)
                truncated |= len(rows) > 3000
                names = set()
                for row in rows[:3000]:
                    key = (row["source"], row["target"], row["relationship_type"])
                    if len(relationships) < 3000 or key in relationships:
                        relationships[key] = row
                        names.update((row["source"], row["target"]))
                    else:
                        truncated = True
                neighbors = await conn.fetch(ACTIVE + "SELECT * FROM nodes WHERE name=ANY($3::text[]) ORDER BY name LIMIT 1001", scope.tenant_id, scope.project_id, list(names-set(selected)))
                frontier = []
                for row in neighbors:
                    if len(selected) == 1000:
                        truncated = True
                        break
                    selected[row["name"]] = row
                    frontier.append(row["name"])
                if not frontier:
                    break
            types = {name: row["entity_type"] for name, row in selected.items()}
            entities = [{"id": str(uuid5(scope.project_id, r["name"])), "name": r["name"],
                         "entity_type": r["entity_type"], "description": r["description"], "score": 1.0}
                        for r in selected.values()]
            return {"query": query, "project": scope.project_key, "mode": mode, "expanded": bool(expand),
                "high_level": [e for e in entities if e["entity_type"] in HIGH],
                "low_level": [e for e in entities if e["entity_type"] in LOW],
                "relationships": [self._edge(scope, r, types) for r in relationships.values()
                                  if r["source"] in selected and r["target"] in selected],
                "truncated": truncated, "freshness": (await self._stats(conn, scope, generation))["freshness"]}

    def _source_valid(self, source):
        if not isinstance(source, Source) or not isinstance(source.record_id, UUID) or source.kind not in KINDS:
            raise ValueError("Invalid Core source snapshot")
        bounded_int(source.revision, 1, 2**63-1)
        text(source.label, 180)
        text(source.description, 420, empty=True)
        text(source.content, 65536, empty=True)

    def _facts_valid(self, facts):
        if not isinstance(facts, Extraction) or facts.identity != self.identity:
            raise StaleGraph("stale_extractor")
        bounded_int(len(facts.nodes), 0, 1000)
        bounded_int(len(facts.edges), 0, 3000)
        names = set()
        for node in facts.nodes:
            if not isinstance(node, Node) or node.entity_type not in HIGH+LOW or node.name in names:
                raise ValueError("Invalid or duplicate entity")
            names.add(text(node.name, 256))
            text(node.description, 500, empty=True)
        keys = set()
        for edge in facts.edges:
            if not isinstance(edge, Edge) or edge.source not in names or edge.target not in names or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", edge.relationship_type):
                raise ValueError("Invalid relationship")
            text(edge.description, 500, empty=True)
            key = (edge.source, edge.target, edge.relationship_type)
            if key in keys:
                raise ValueError("Duplicate relationship")
            keys.add(key)

    @retry
    async def _select_sources(self, subject, kinds, limit, reprocess, project):
        if not callable(self.source_reader):
            raise GraphUnavailable("source_reader_unavailable")
        async with self._tx(subject, "control", project) as (conn, scope):
            generation = await self._state(conn, scope)
            before = await self._stats(conn, scope, generation)
            rows = await conn.fetch("""SELECT r.id,r.current_revision,r.kind FROM core.records r
                LEFT JOIN retrieval.graph_applied a ON
                 (a.tenant_id,a.project_id,a.generation,a.record_id,a.source_revision)=
                 (r.tenant_id,r.project_id,$3,r.id,r.current_revision) AND a.source_kind=r.kind
                WHERE r.tenant_id=$1 AND r.project_id=$2 AND NOT r.tombstone AND r.kind=ANY($4::text[])
                  AND ($5::boolean OR a.record_id IS NULL)
                ORDER BY r.id LIMIT $6""", scope.tenant_id, scope.project_id, generation, kinds, reprocess, limit)
            references = tuple(SourceRef(r["id"], r["current_revision"], r["kind"]) for r in rows)
            try:
                sources = await self.source_reader(conn, scope, references)
            except (PermissionError, asyncpg.SerializationError):
                raise
            except Exception:
                raise GraphUnavailable("source_reader_unavailable") from None
            if not isinstance(sources, (list, tuple)) or len(sources) != len(references):
                raise GraphUnavailable("invalid_source_receipt")
            selected, seen = [], set()
            expected = {r.record_id: r for r in references}
            for source in sources:
                try:
                    self._source_valid(source)
                except (ValueError, TypeError):
                    raise GraphUnavailable("invalid_source_receipt") from None
                ref = expected.get(source.record_id)
                if ref is None or (source.revision, source.kind) != (ref.revision, ref.kind) or source.record_id in seen:
                    raise GraphUnavailable("invalid_source_receipt")
                seen.add(source.record_id)
                current = await conn.fetchrow("SELECT current_revision,tombstone,kind FROM core.records WHERE tenant_id=$1 AND project_id=$2 AND id=$3", scope.tenant_id, scope.project_id, source.record_id)
                if current is None or current["current_revision"] != source.revision or current["tombstone"] or current["kind"] != source.kind:
                    raise StaleGraph("stale_source")
                applied = await conn.fetchval("SELECT source_revision FROM retrieval.graph_applied WHERE tenant_id=$1 AND project_id=$2 AND generation=$3 AND record_id=$4", scope.tenant_id, scope.project_id, generation, source.record_id)
                if reprocess or applied != source.revision:
                    selected.append(source)
            return scope, generation, selected, before

    @retry
    async def _publish(self, subject, original_scope, generation, source, facts, project, *, existing_fact_id=None):
        self._facts_valid(facts)
        if existing_fact_id is None and not callable(self.fact_sink):
            raise GraphUnavailable("canonical_fact_port_unavailable")
        async with self._tx(subject, "writer", project) as (conn, scope):
            if scope != original_scope:
                raise PermissionError("Core scope changed")
            current_generation = await self._state(conn, scope, lock=True)
            if current_generation != generation:
                raise StaleGraph("stale_generation")
            current = await conn.fetchrow("SELECT current_revision,tombstone,kind FROM core.records WHERE tenant_id=$1 AND project_id=$2 AND id=$3 FOR UPDATE", scope.tenant_id, scope.project_id, source.record_id)
            if current is None or current["current_revision"] != source.revision or current["tombstone"] or current["kind"] != source.kind:
                raise StaleGraph("stale_source")
            existing = await conn.fetch(ACTIVE + "SELECT name,entity_type FROM nodes WHERE name=ANY($3::text[])", scope.tenant_id, scope.project_id, [n.name for n in facts.nodes])
            new_types = {n.name: n.entity_type for n in facts.nodes}
            if any(new_types[r["name"]] != r["entity_type"] for r in existing):
                raise GraphUnavailable("entity_type_conflict")
            fact_id = existing_fact_id
            if fact_id is None:
                try:
                    fact_id = await self.fact_sink(conn, scope, source, facts)
                except (PermissionError, asyncpg.SerializationError):
                    raise
                except Exception:
                    raise GraphUnavailable("canonical_fact_port_unavailable") from None
            if not isinstance(fact_id, UUID):
                raise GraphUnavailable("invalid_canonical_fact_receipt")
            canonical = await conn.fetchrow("""SELECT f.record_id,f.source_revision,f.extractor_identity,p.body
                FROM core.extraction_facts f JOIN core.payloads p ON
                 (p.tenant_id,p.project_id,p.id)=(f.tenant_id,f.project_id,f.payload_ref)
                WHERE f.tenant_id=$1 AND f.project_id=$2 AND f.id=$3""", scope.tenant_id, scope.project_id, fact_id)
            if (canonical is None or canonical["record_id"] != source.record_id or
                canonical["source_revision"] != source.revision or canonical["extractor_identity"] != facts.identity):
                raise GraphUnavailable("invalid_canonical_fact_receipt")
            try:
                if len(canonical["body"]) > 4194304 or json.loads(bytes(canonical["body"])) != json.loads(json.dumps(asdict(facts))):
                    raise ValueError()
            except (ValueError, TypeError):
                raise GraphUnavailable("invalid_canonical_fact_receipt") from None
            args = (scope.tenant_id, scope.project_id, generation, source.record_id)
            await conn.execute("DELETE FROM retrieval.graph_applied WHERE tenant_id=$1 AND project_id=$2 AND generation=$3 AND record_id=$4", *args)
            await conn.execute("INSERT INTO retrieval.graph_applied(tenant_id,project_id,generation,record_id,source_revision,source_kind,source_label,source_description,fact_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)", *args, source.revision, source.kind, source.label, source.description, fact_id)
            await conn.executemany("INSERT INTO retrieval.graph_nodes VALUES($1,$2,$3,$4,$5,$6,$7)", [(*args, n.name, n.entity_type, n.description) for n in facts.nodes])
            await conn.executemany("INSERT INTO retrieval.graph_edges VALUES($1,$2,$3,$4,$5,$6,$7,$8)", [(*args, e.source, e.target, e.relationship_type, e.description) for e in facts.edges])

    @retry
    async def _rebuild_facts(self, subject, original_scope, generation, sources, project):
        async with self._tx(subject, "control", project) as (conn, scope):
            if scope != original_scope or await self._state(conn, scope) != generation:
                raise StaleGraph("stale_generation")
            packets = []
            for source in sources:
                packet = await conn.fetchrow("""SELECT f.id,p.body FROM core.extraction_facts f
                    JOIN core.payloads p ON (p.tenant_id,p.project_id,p.id)=(f.tenant_id,f.project_id,f.payload_ref)
                    WHERE f.tenant_id=$1 AND f.project_id=$2 AND f.record_id=$3
                      AND f.source_revision=$4 AND f.extractor_identity=$5 AND octet_length(p.body)<=4194304
                    ORDER BY f.created_at DESC,f.id DESC LIMIT 1""", scope.tenant_id, scope.project_id,
                    source.record_id, source.revision, self.identity)
                packets.append(packet)
            return packets

    def _decode_facts(self, payload):
        value = json.loads(bytes(payload))
        if not isinstance(value, dict) or set(value) != {"identity", "nodes", "edges"}:
            raise ValueError("Invalid graph fact codec")
        if not isinstance(value["nodes"], list) or not isinstance(value["edges"], list):
            raise ValueError("Invalid graph fact codec")
        bounded_int(len(value["nodes"]), 0, 1000)
        bounded_int(len(value["edges"]), 0, 3000)
        facts = Extraction(value["identity"], tuple(Node(**n) for n in value["nodes"]), tuple(Edge(**e) for e in value["edges"]))
        self._facts_valid(facts)
        return facts

    async def rebuild(self, subject, *, limit=1000, project=None):
        """C07 control hook: restore derived rows using immutable current facts."""
        bounded_int(limit, 1, 1000)
        scope, generation, sources, before = await self._select_sources(subject, list(KINDS), limit, False, project)
        packets = await self._rebuild_facts(subject, scope, generation, sources, project)
        processed, errors = 0, []
        for source, packet in zip(sources, packets):
            try:
                if packet is None:
                    raise GraphUnavailable("canonical_fact_unavailable")
                facts = self._decode_facts(packet["body"])
                await self._publish(subject, scope, generation, source, facts, project, existing_fact_id=packet["id"])
                processed += 1
            except PermissionError:
                raise
            except StaleGraph as exc:
                errors.append({"id": str(source.record_id), "error": exc.code})
            except Exception:
                errors.append({"id": str(source.record_id), "error": "canonical_fact_unavailable"})
        return {"project": scope.project_key, "selected": len(sources), "processed": processed,
                "errors": errors, "stats_before": before, "stats": await self.stats(subject, project=project)}

    async def extract(self, subject, *, source="all", limit=20, dry_run=True, reprocess=False,
                      backfill=False, use_llm=False, model=None, project=None):
        bounded_int(limit, 1, 1000)
        source = "all" if backfill else source
        aliases = {**{k: k for k in KINDS}, **{v: k for k, v in KINDS.items()}}
        if source != "all" and source not in aliases:
            raise ValueError("Unsupported graph source")
        kinds = list(KINDS) if source == "all" else [aliases[source]]
        if model is not None:
            text(model, 512)
        if not dry_run and (not callable(self.extractor) or not callable(self.fact_sink)):
            raise GraphUnavailable("extraction_ports_unavailable")
        scope, generation, sources, before = await self._select_sources(subject, kinds, limit, reprocess, project)
        processed, errors = 0, []
        if not dry_run:
            for item in sources:
                try:
                    async with asyncio.timeout(5):
                        facts = await self.extractor(item, {"use_llm": use_llm, "model": model})
                    await self._publish(subject, scope, generation, item, facts, project)
                    processed += 1
                except StaleGraph as exc:
                    errors.append({"source": KINDS[item.kind], "id": str(item.record_id), "error": exc.code})
                except GraphUnavailable:
                    errors.append({"source": KINDS[item.kind], "id": str(item.record_id), "error": "capability_unavailable"})
                except PermissionError:
                    raise
                except Exception:
                    errors.append({"source": KINDS[item.kind], "id": str(item.record_id), "error": "extraction_unavailable"})
        after = await self.stats(subject, project=project)
        return {"status": "dry-run" if dry_run else "processed", "project": scope.project_key,
                "source": source, "source_tables": [KINDS[k] for k in kinds], "limit": limit,
                "dry_run": dry_run, "reprocess": reprocess, "use_llm": use_llm,
                "selected": len(sources), "processed": processed, "errors": errors,
                "stats_before": before, "stats": after}

    @retry
    async def prune(self, subject, *, dry_run=True, keep_projects=(), project=None):
        if not isinstance(keep_projects, (list, tuple)) or len(keep_projects) > 100:
            raise ValueError("Invalid retained projects")
        async with self._tx(subject, "control", project) as (conn, scope):
            if any(p != scope.project_key for p in keep_projects):
                raise PermissionError("Prune is scoped to the authorized project")
            active = await self._state(conn, scope, lock=True)
            rows = await conn.fetch("SELECT id FROM retrieval.graph_generations WHERE tenant_id=$1 AND project_id=$2 AND id<>$3 ORDER BY id LIMIT 1001 FOR UPDATE", scope.tenant_id, scope.project_id, active)
            candidates = [r["id"] for r in rows[:1000]]
            if not dry_run:
                await conn.execute("DELETE FROM retrieval.graph_generations WHERE tenant_id=$1 AND project_id=$2 AND id=ANY($3::uuid[]) AND id<>$4", scope.tenant_id, scope.project_id, candidates, active)
            return {"project": scope.project_key, "storage": "postgres", "dry_run": dry_run,
                    "candidates": [str(x) for x in candidates], "pruned": [] if dry_run else [str(x) for x in candidates],
                    "candidate_count": len(candidates), "pruned_count": 0 if dry_run else len(candidates),
                    "truncated": len(rows) > 1000}

    def _build_options(self, request):
        if not isinstance(request, dict) or set(request)-{"repo", "full", "embed", "import_existing", "async_job", "sync"}:
            raise ValueError("Invalid graph build request")
        options = {"full": False, "embed": True, "import_existing": False, "async_job": False, "sync": False, **request}
        text(options.get("repo"), 4096)
        if any(type(options[k]) is not bool for k in ("full", "embed", "import_existing", "async_job", "sync")):
            raise ValueError("Boolean graph build flags required")
        if options["import_existing"] and (options["full"] or options["embed"]):
            raise ValueError("Invalid graph import mode")
        return options

    @retry
    async def _build_start(self, subject, options, project):
        async with self._tx(subject, "control", project) as (conn, scope):
            generation = await self._state(conn, scope)
            if options["repo"] not in {scope.project_key, scope.repo}:
                raise PermissionError("Repo selector does not match Core registration")
            if options["async_job"] or (options["full"] and not options["sync"]):
                if not callable(self.enqueue_job):
                    raise GraphUnavailable("durable_job_port_unavailable")
                try:
                    jid = await self.enqueue_job(conn, scope, {**options, "repo": scope.repo, "generation": str(generation), "extractor_identity": self.identity})
                except (PermissionError, asyncpg.SerializationError):
                    raise
                except Exception:
                    raise GraphUnavailable("durable_job_port_unavailable") from None
                if not isinstance(jid, UUID):
                    raise GraphUnavailable("invalid_job_receipt")
                return scope, generation, {"project": scope.project_key, "job_id": str(jid), "status": "queued",
                    "repo": scope.repo, "full": options["full"], "embed": options["embed"],
                    "status_url": f"/graph/build/jobs/{jid}", "message": "graph build accepted; poll status_url for progress"}
            return scope, generation, None

    @retry
    async def _build_finish(self, subject, scope, generation, options, receipt, project):
        async with self._tx(subject, "control", project) as (conn, current):
            if current != scope or await self._state(conn, current, lock=True) != generation:
                raise StaleGraph("stale_generation")
            stats = await self._stats(conn, current, generation)
            if stats["freshness"]["pending_records"]:
                raise GraphUnavailable("partial_build")
            if not isinstance(receipt, dict):
                raise GraphUnavailable("invalid_build_receipt")
            try:
                if options["import_existing"]:
                    if receipt.get("status") != "imported-existing-graph" or receipt.get("storage_key") != scope.project_key:
                        raise ValueError()
                    for field in ("repo", "source", "graph_db"):
                        text(receipt.get(field), 4096)
                    n, e = receipt.get("nodes"), receipt.get("edges")
                else:
                    if receipt.get("status") != "ok" or receipt.get("build_type") != ("full" if options["full"] else "incremental") or receipt.get("errors", []) != []:
                        raise ValueError()
                    text(receipt.get("summary"), 4096)
                    n, e = receipt.get("total_nodes"), receipt.get("total_edges")
                    if options["full"]:
                        bounded_int(receipt.get("files_parsed"), 0, 2**31-1)
                        if receipt.get("errors") != []:
                            raise ValueError()
                    else:
                        bounded_int(receipt.get("files_updated"), 0, 2**31-1)
                        for field in ("changed_files", "dependent_files"):
                            if not isinstance(receipt.get(field), list) or len(receipt[field]) > 1000:
                                raise ValueError()
                            for value in receipt[field]:
                                text(value, 4096)
                    if options["embed"]:
                        embeddings = receipt.get("embeddings", {})
                        if embeddings.get("status") != "ok" or embeddings.get("backend") != "openrouter":
                            raise ValueError()
                        text(embeddings.get("summary"), 4096)
                        for field in ("newly_embedded", "total_embeddings"):
                            bounded_int(embeddings.get(field), 0, 2**31-1)
                bounded_int(n, 0, 2**31-1)
                bounded_int(e, 0, 2**31-1)
                if (n, e) != (stats["entity_count"], stats["relationship_count"]):
                    raise ValueError()
            except (ValueError, AttributeError, TypeError):
                raise GraphUnavailable("invalid_build_receipt") from None
            return receipt

    async def build(self, subject, request, *, project=None):
        options = self._build_options(request)
        scope, generation, queued = await self._build_start(subject, options, project)
        if queued is not None:
            return queued
        if not callable(self.build_executor):
            raise GraphUnavailable("build_executor_unavailable")
        try:
            async with asyncio.timeout(30):
                receipt = await self.build_executor(subject, scope, {**options, "repo": scope.repo, "generation": str(generation), "extractor_identity": self.identity})
        except PermissionError:
            raise
        except Exception:
            raise GraphUnavailable("build_executor_unavailable") from None
        return await self._build_finish(subject, scope, generation, options, receipt, project)

    @retry
    async def job(self, subject, job_id, *, project=None):
        jid = UUID(job_id)
        if not callable(self.read_job):
            raise GraphUnavailable("durable_job_port_unavailable")
        async with self._tx(subject, project=project) as (conn, scope):
            try:
                receipt = await self.read_job(conn, scope, jid)
            except (PermissionError, asyncpg.SerializationError):
                raise
            except Exception:
                raise GraphUnavailable("durable_job_port_unavailable") from None
            if receipt is not None and (not isinstance(receipt, dict) or receipt.get("id") != str(jid)
                or receipt.get("project") != scope.project_key or receipt.get("status") not in
                {"queued", "running", "completed", "failed", "canceled", "unresolved"}):
                raise GraphUnavailable("invalid_job_receipt")
            return receipt
