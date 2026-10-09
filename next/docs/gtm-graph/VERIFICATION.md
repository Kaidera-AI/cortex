# H-D455 PostgreSQL graph source and integration contract

```text
authenticated principal -> Core read / control / writer grants + registry scope
  -> forced tenant/project RLS -> active extractor generation
  -> join current Core revision/kind/tombstone -> bounded graph + provenance
Core SourceRef hydration -> existing extractor (outside PG)
  -> fresh writer grant + locked generation/source
  -> immutable Core fact UUID/payload verified -> same-transaction projection
lost projection -> current immutable facts + hydration -> rebuild (no new extraction)
build -> durable Core intent or injected existing executor -> verified completion
```

The source is an adapter/plugin beneath C03/C04/C07/C11, not a mounted release.
No live schema, API, containers, monitoring or credentials are changed. No graph
engine, SQLite store, new provider/local model or host filesystem crawl is added.

## Execution proof

Commit5b9d350 pins caller/seam tests before implementation; the first RED is a
missing-module import, not a mutation kill. Initial implementation2e49366 passes
103 integration checks. fb64308 pins pending-record starvation, whole-read timeout
and no-op fact-port regressions: starvation and no-op receipts fail assertions;
the original timeout RED is a behavioral error. Its subsequent named deadline
probe explicitly asserts on an over-budget operation. 5e12ec3 pins the missing
canonical-fact rebuild hook before implementation (AttributeError, not a kill).
d1d3077 pins multiword-query and changed-Core-kind assertion regressions. A later
own-source reprocess regression rejects a self-conflict before its correction;
the companion cross-source conflict control preserves unambiguous entity names. Raw
before/after logs are in helix docs/plans/cortex-v2-v0.1.020/lane-F/gtm-graph-*.log.

Final proof is run only after all source is committed, the current C08 target is
merged, and Cox's ONE merged next/tests/test_receipts.py is adopted through PR29.
The adopted helper is PR37's async-fixture fix from main d715d062, unchanged SHA256
d4e0fc8813bbf84c9e53405aa11d8d711d5552c6ac8297724998141fa95486fa.
PR29's runner forwards suite/named selection to that shared CLI; graph inherits
phase admission through PR33 edea171, without a copied emitter or classifier.
The parent disposable runner executes the prior search/provider/Conductor stack
plus graph integration cases. The graph mutation driver reuses the parent's bound
proof driver; it changes no parent mutation recipe and implements no classifier.
Every kill must be an expected test-body AssertionError from the shared structured
result. Fixtures, setup/teardown, import, runner, signal, missing metadata and source
drift are INCONCLUSIVE. Clean baseline/restored full suites and mandatory container
cleanup gate every set of 21 graph mutation recipes (source, route and SQL).

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r next/tests/integration/graph-requirements.txt
.venv/bin/python next/tests/integration/run_pg_graph.py
.venv/bin/python next/tests/integration/mutate_pg_graph.py /absolute/proof/directory
```

Adjacent *.source.json packets bind exact HEAD/Git tree, all tracked/nonignored
next/ file bytes before/after, raw output SHA256, and a canonical manifest digest.
mutations.json binds each literal recipe, original/mutant file digest and target.
The final handback names the published head and retained completed receipts.
The shared pinned PG18.4/pgvector0.8.2 native arm64 image uses one disposable stack
per Nemo worker, <=1GiB/2CPUs, tmpfs, random loopback port and mandatory cleanup.
macOS Python3.12 proof is not native Linux/release/performance qualification.

## Required Core ports

Read, control and writer authorization are separate required callbacks; each
rechecks Core grants in the transaction and returns GraphScope(tenant UUID,
project UUID, Core project key, Core-approved repo). No headers grant authority.
source_reader(conn, scope, tuple[SourceRef]) hydrates exactly the selected current
IDs/revisions/kinds in the same snapshot. Records are selected before the limit,
so completed rows cannot starve pending work. Source bounds: label180,
description420, content65536 characters; extraction outputs <=1000 nodes/3000
edges per record, with bounded names/descriptions and endpoint validation.

extractor(Source, options) is the existing Cortex extraction adapter, bounded to
five seconds outside PG; its complete registered identity must match the graph.
fact_sink(conn, scope, Source, Extraction) persists the immutable Core payload and
fact in that same transaction and returns its UUID. Payload JSON has exactly
identity/nodes/edges (Extraction dataclass shape), <=4MiB. Publication verifies
scope, source revision, identity and actual payload equality; a no-op, foreign or
mismatched receipt cannot authorize a projection. Rebuild reads current immutable
facts and validates that same codec/identity; it never invokes the extractor or
adds a new fact. All PG transactions have a whole two-second budget and bounded
whole-transaction serialization retries; callbacks must honor cancellation.

enqueue_job(conn, scope, options) and read_job(conn, scope, UUID) are C03/C07 durable
Core job ports, not in-memory tasks. Queued means persisted intent, not execution.
The optional existing build executor runs outside PG with a30-second budget;
completion is reauthorized and bound to the original generation, no backlog,
actual PG graph counts and the observed legacy completion fields. Partial errors
are refused. embed=true additionally requires its existing OpenRouter completion
receipt; this adapter does not invent embeddings. Missing ports are unavailable.
C07 owns executor registration, cancellation/recovery and current extractor/provider
binding. Synthetic fixtures prove this port contract, not their production wiring.

## Route and storage deltas for C02/C11

| Existing method/path | Preserved successful fields |
| --- | --- |
| GET /cortex-graph/stats | entity_count, relationship_count, source_counts, backlog |
| GET /cortex-graph/memory | project, nodes, edges, sources, source_edges |
| GET /cortex-graph-search | query, project, mode, expanded, high_level, low_level, relationships |
| GET /graph/stats | total_nodes, total_edges, repos[{name,nodes,edges,path}] |
| POST /cortex-graph-extract | status, project, source, source_tables, limit, flags, selected, processed, errors, stats_before, stats |
| POST /graph/build | legacy full/incremental/import completion; queued job_id/status_url with HTTP202 |
| GET /graph/build/jobs/{job_id} | scoped durable job receipt; missing/foreign job404 |
| POST /graph/prune | dry_run, candidates/pruned/counts/truncated; authorized scoped PG generations |

Search preserves current multiword token OR matching; score1 means a lexical
match, not vector similarity or a measured ranking claim. Expansion is <=3 hops,
<=1000 nodes/3000 relationships; provenance is <=1000 sources/3000 source edges.
Memory accepts the legacy limit up to2000 and clamps work to1000 with explicit
truncated when data exceeds the effective cap. Count/freshness metadata remains
truthful when output is clipped. /graph/stats path is a logical pg:// locator,
never an invented SQLite file; repo name is the Core project key. Exact storage
identity/ranking parity and proposed typed error envelopes await C02 fixtures.

Prune intentionally removes only obsolete generations of the authorized project;
dry-run writes nothing. Active generation, other projects, canonical records and
immutable facts are preserved. keep_projects accepts only this scoped project;
there is no filesystem-delete equivalence or global registry sweep. C02 must
approve this storage delta before integration. C04 supplies least-privilege roles
and registry/authorization ports; C03 registers migration003 without changing its
owned schemas/manifest here. C11 mounts the Router with an authenticated principal
resolver; the resolver has no default/header-authority fallback. Control-only
rights gate extraction/build/prune/rebuild; Graph never becomes a data-path relay.

**Door:** two-way for undeployed source; applying/dropping migrations has its own gate.
**Blast radius:** graph.
