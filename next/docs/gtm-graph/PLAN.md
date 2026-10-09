# H-D455 graph on Postgres execution plan

Nemo's fourth queued slice, Cortex worker artifact rule pre-approved. One stacked
PR after C08 PR33; no self-merge/release. Accepted revision5/C01 permits development
against ports before C04/C07/C11 integration. A separate graph engine is OUT.

## Observed caller contract

Fresh source reads: cortex/packages/api/main.py graph handlers and request models;
packages/cli/cortex-graph-{stats,search,build,prune}, cortex-extract-entities;
helix/local-cortex/console/app/{cortex_client.py,graph/api.py,graph/shape.py}.
C01 published route matrix identifies R023–R026 and R034–R037 as changing seams.
Preserve paths and successful fields for KOS/CLI: GET /cortex-graph/stats,
/cortex-graph/memory (the requested /memory means this suffix), /cortex-graph-search,
/graph/stats; POST /graph/build, /graph/prune, /cortex-graph-extract; GET
/graph/build/jobs/{job_id}. Preserve high_level/low_level/relationships and
nodes/edges/sources/source_edges, scoped count envelopes, queued status_url and job
polling. No invented SQLite file or remote-worker request.

## Source and proof order

1. Commit plan and RED integration/route checks before implementation.
2. next/schema/retrieval/003-pg-graph.sql: graph state/generation, per-record
applied source revisions and derived entity/relationship rows, Core project/record
revision FKs, forced tenant/project RLS and scoped indexes. Canonical Core records,
extraction facts and job tables remain Cox-owned. New graph reads join current Core
revision/tombstone directly, so stale projections cannot be served as current.
3. next/src/cortex_core/modules/graph/pg_graph.py: mandatory read/control/writer
authorization callbacks; bounded queries (2s), max1,000 returned nodes, max3,000
edges, depth<=3, full-transaction serialization retry; model/extractor generation
checks. Search uses parameterized terms, memory includes source provenance; truthful
backlog/current/lagging/unavailable states. Derived graph writes invoke a required
canonical-fact port in the same authorized PG transaction, with stale-result refusal.
4. graph routes/controller module: preserve the observed routes and envelopes,
require an injected authenticated-principal resolver, recheck Core grants per call;
project/repo selectors only match Core-resolved scope, never grant authority. Build
and extraction use an injected existing extractor/source port (no new provider),
bounded outside PG; reauthorize on publication. Async/full build queues through a
required durable Core-job port and exposes typed job polling; no volatile-only job
receipt. Sync/dry-run modes remain explicit. Missing ports return typed unavailable.
Prune is admin-authorized and deletes only obsolete scoped PG projection generations;
Core facts, active generations and other projects are protected. Document this PG
storage contract delta for C02, without claiming old filesystem-prune equivalence.
5. tests: real disposable PG with minimal named C03-shaped Core fixtures and a
durable synthetic Core-job/fact port; test existing caller envelopes, current grants,
cross-tenant/project filters/RLS/FKs, revision/tombstone/generation races, empty vs
unavailable, provenance, bounded traversal/input, extraction stale completion,
PG intent surviving adapter restart, scoped job polling, dry-run/admin prune and
Core outage. Full prior stack + graph checks; named assertion source/SQL mutants,
clean baseline/restoration, raw inconclusive errors separately.
6. Fetch/merge target tip, exact-head full rerun, push only graph branch, return
source/receipts to Kai for reviewer assignment. Fold docs/proofs into main helix
docs and remove parked worktree. Re-read inbox; do remaining standing work before wait.

## Binding gates and proof limits

C03 published head4d89ffebddef317c4ea3e916dd768bf2749fd4e6 supplies
core.projects/records/record_revisions/extraction_facts, coordination.jobs/results
and retrieval.generations. Nemo does not edit their migrations or shared manifest.
Core source hydration, canonical extraction-fact persistence, registry-backed repo
identity, durable job enqueue/status and the existing extraction execution adapter
are explicit injected ports; C04/C07/C11 bind them. C02 freezes exact wire statuses
and the typed availability/prune delta; acceptance is not implied by synthetic tests.
No source filesystem crawl, new LLM/provider, live API/DB, paid service or graph engine.
Only one disposable Podman PG <=1GiB/2CPU with nemo label, tmpfs and mandatory cleanup;
no SSD bind mount. The prior pinned native arm64 image is development evidence only.

## Pre-edit custody

Clean new nemo-gtm-graph-pg-20261009 worktree based on published C08 5eda8f3.
Shared checkout's pre-existing untracked docs/v2/DS_Store paths remain untouched.
Existing C03/C01 next paths inspected by origin refs; graph module/migration paths
are absent. Ren-CX's legacy graph build/repro branches are not edited or treated as
v2 graph ownership. This plan records the source/route seam before any graph code.

## Port/proof refinement after first execution

Fresh C03 source f30e1d82 confirms the same Core keys; this fixture is an explicitly
minimal subset, not an executed C03/C04 release stack. Core hydration receives exact
SourceRef(record UUID, revision, kind) tuples selected before the limit, so already
applied records cannot starve pending records. All graph PG transactions have a
whole two-second budget. Extraction/build awaits run outside these transactions.
The fact port returns a persisted Core fact UUID: validate scope, source revision,
extractor identity and immutable payload against the exact Extraction shape before
publishing; graph_applied keeps its scoped fact FK. Fact payload codec is identity,
nodes and edges (the Extraction dataclass JSON shape); C07 binds this named port.
Retain RED-first selection, timeout and no-op fact-port checks. Add a canonical-fact
rebuild hook and prove projection loss can be repaired without an extractor call or
new canonical fact. Existing full-build execution remains an injected C07 adapter,
not a fabricated receipt. User/CTO/Kai keep C02/C04/C07/C11 integration gates.
Kai23:08: shared test-body classifier is owned by Cox's standalone PR; continue
graph while it is reviewed, then adopt the merged helper before final mutation proof.

## Final structured-proof gate — Kai 2026-10-10 00:24 GO

PR37's async fixture fix merged on main d715d062. Adopt its ONE unchanged
next/tests/test_receipts.py through PR29 914ee9e, PR31 c36318f and PR33 edea171,
restacking in order before the graph proof. Graph source, thirty integration cases
and twenty-one recipes are unchanged from draft2083f1c; the full baseline now
includes the parent's two retained fixture-attribution cases as well.
Commit this source/document tree before running baseline, named mutations and
restoration. Every receipt binds that exact HEAD, all next/ bytes, actual mutant
digests and raw output; independently reconcile literal recipes and use the shared
classifier for admission. Keep PR36 draft until the completed receipts exist.

## Source-kind rework — Kai 2026-10-10 01:16 GO, shape A

GRAPH-PG-002: one new ordinary-extraction control changes decision/revision1 to
lesson/revision1, then consumes the pending record without reprocess or rotation.
Both pending SQL and the hydrated-source applied check bind record/revision/kind.

GRAPH-PG-001: one kind-dependent extractor control changes kind at revision1,
rotates generation and refuses old-kind reuse; after ordinary extraction, matching
kind facts must rebuild even when a newer other-kind fact exists. Core tables and
their invariants remain unchanged. The graph-owned encode_graph_fact(Source,
Extraction) codec adds source_kind as the first compact JSON field; the existing
Core fact columns bind record/revision/extractor. Reuse filters the parameterized
canonical kind-byte prefix before newest/LIMIT, without casting untrusted bytea
to JSON. Decode and publication verify the kind and exact canonical payload bytes.
Old unbound payloads are unavailable and require ordinary extraction; C07 owns the
production sink binding to this codec. No new provider or Core migration.

GRAPH-PG-003 is local: preserve only the three allowlisted publication causes
entity_type_conflict, invalid_canonical_fact_receipt and
canonical_fact_port_unavailable in the existing per-record error field. Unknown
dependency text stays scrubbed. One RED control covers extraction and rebuild;
the broader C02/C07 error contract remains gated.

Commit this amendment, then the three controls before fixing source. Preserve
existing test bodies; update the existing payload mutant only for the new codec,
add kind-selection/reuse and safe-cause recipes, and rerun full clean/restored
source-bound proof. Merge current main, push PR36, merge it into PR39 without
rebasing or changing health-owned bytes, and rerun PR39 proof before handback.
