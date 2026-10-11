# B01 Postgres baseline preparation

Mike authors; Vera reviews. H-D455 selects Postgres first; Qdrant is deferred until complete real-data product search evidence fails a gate. This subtree supplies reproducible inputs, independent truth and a disposable **synthetic SQL diagnostic**, not an HTTP service or a release decision.

```text
corpus.py → five-million-default dense/metadata/CSR arrays + hashed queries
oracle.py → full eligible canonical scans → strict/tie/full-fusion truth
postgres.py → owned pgvector HNSW + scoped filters + sparse dot/RRF
  → per-filter diagnostic receipts → UNDECIDED for the product engine
```

From the repository root, use pinned Python 3.12 dependencies:

```sh
uv sync --project next/benchmarks/vector_baseline --frozen --python 3.12
PYTHONPATH=next/src uv run --project next/benchmarks/vector_baseline --frozen --python 3.12 \
  python -m vector_baseline.corpus /absolute/new/synthetic-corpus \
  --dimension 768 --model synthetic-fixture-768
B01_PODMAN=1 PYTHONPATH=next/src uv run --project next/benchmarks/vector_baseline --frozen --python 3.12 \
  python -m unittest discover -s next/tests/benchmarks -v
PYTHONPATH=next/src uv run --project next/benchmarks/vector_baseline --frozen --python 3.12 \
  python next/tests/benchmarks/mutate_vector_baseline.py
```

The dimension/model are mandatory and labelled synthetic; 768 above is an example, not a production binding. Default count is exactly 5,000,000. Use `--count 24000 --dimension 8 --model synthetic-sql-fixture-8` for a quick local smoke corpus. Output must be a new directory: existing artifacts are not overwritten. Neither generation nor truth makes provider calls.

```sh
PYTHONPATH=next/src uv run --project next/benchmarks/vector_baseline --frozen --python 3.12 \
  python -m vector_baseline.postgres /absolute/synthetic-smoke-corpus \
  /absolute/new/sql-diagnostic.json --per-cell 1
```

`--per-cell` selects that many frozen held-out queries in each stratum/mode. Every omitted or unpopulatable query remains NOT_RUN. The CLI checks query/corpus hashes before opening its fresh database. Each query keeps returned public fixture IDs, natural EXPLAIN, a separately labelled forced-index witness, strict/tie recall against full fusion, finite-prefetch diagnostics, branch-boundary completeness and SQL elapsed time. Errors remain visible. This timing excludes auth/network/provider/cache/product paths and is not a latency gate.

The one local container uses the reviewed immutable upstream image, PostgreSQL/extension versions queried at startup, 1 GiB/2 CPU bounds, UID999/capabilities dropped/no-new-privileges, random loopback port and ephemeral password secret. A process lock prevents overlapping Mike B01 stacks. Create and start are separate so failed startup remains owned. Fresh named storage, container and secret are removed and inventoried on success/failure. No existing service URL, live database, host bind mount, OS-cache reset or paid provider is accepted. Allow Podman's default upstream seccomp profile; no privilege escalation or image build.

Synthetic geometry: deterministic PCG64 streams under pinned NumPy, seed447020; 70% clustered,20% diffuse,10% exact ties; 64 tenants with 50%/25%/25% skew across tenant0/1–7/8–63 and four projects each; delete/type/time/ordinal/sparse features. Float32 little-endian `.npy` arrays and CSR sparse arrays are hash-bound and memory mapped. Chunk size does not change row bytes. Both query splits contain1,200 rows (60 dense/120 hybrid/20 sparse per stratum), use disjoint IDs/streams and record full canonical eligible counts. Small corpora explicitly lack some selectivity cells. Initial synthetic filters use scoped ordinal ranges; kind/time predicates have independent controls. Broader C02 predicates and actual coarse-filter distributions require their owner bindings.

Oracle scoring is independent NumPy float64 over stored float32, with separate predicate and sparse implementations. It scans the complete corpus in blocks, applies model/scope/delete selection before ranking and retains global score/rank arrays for exact fusion. Memory is O(N) for scores/ranks plus O(block×dimension), not O(N×dimension) vector copies. Oracle computation is outside timed SQL. Canonical ASCII IDs break branch ties before top200; equal-weight zero-based RRF uses c2. Full-branch fusion is the mandatory hybrid target; finite-prefetch coverage cannot rescue a failure. Zero tolerance is frozen. Empty, short, duplicate, forbidden and missing-sparse cases remain distinct.

`validate_sanitized_row` is ren-tk's **typed Marlow slot**, not an exporter or importer with data-access authority. It accepts exact vectors, HMAC-hex identities, typed coarse fields/model/delete state and optional authorized sorted sparse features; unknown/private-text fields, invalid geometry and mixed models refuse. The local harness deliberately accepts only synthetic manifests. B02 must bind the protected 4 October snapshot/custody/disposal receipt and actual query/sparse features, C01/C02/C09/C10 product contracts, edition/resource/platform receipts, eight-client 40RPS/38RPS floor, warm and verified OS-cold runs. Missing bindings remain NOT_RUN and engine UNDECIDED. Core PITR/ACK/concurrency/SELinux/native release gates remain separate.

Evidence in `receipts/` records initial new-surface RED, actual forbidden-hit behavioral RED, verification-found cleanup/error/float32/resource controls before their fixes, full GREEN and mutation-specific raw assertion failures. The original resource expectation was tightened from2GiB to1GiB under H-D45521:33; original historical RED hashes remain, and new bytes are separately bound. Five-million generation/reproduction evidence uses **dimension8**, explicitly a synthetic fixture and not production-scale dimension/performance proof. Vera must review the code/oracle independently; no author acceptance, merge or release follows from these receipts.

Upstream references: [pgvector](https://github.com/pgvector/pgvector) for HNSW operators/iterative filtering, [PostgreSQL ORDER BY](https://www.postgresql.org/docs/current/indexes-ordering.html). Exact selective-filter plans are permitted and labelled; an HNSW witness does not prove the natural filtered plan.

The PR35 rework for **B01-CLEANUP-001** records creation as pending before invoking Podman. Each single-use stack attaches a fresh 128-bit lifecycle label to its volume, secret and container. Cleanup reconciles that exact name and label after an ambiguous acknowledgement, retains failed removals for retry, and discards the password and releases the lock even when reconciliation fails. Container and secret deletion use verified immutable IDs. Volume deletion uses the verified name: deliberate external replacement between inspection and deletion is outside this fixture's qualified concurrency model. Pre-existing names with another lifecycle are preserved. The process lock bounds Mike's local stacks; it is not an engine-wide lock on other workers.

**B01-ROW-002** adds an exact `(dimension,)` shape requirement at the typed single-row boundary. Batched matrix validation elsewhere and accepted non-unit vector geometry remain covered.

`receipts/cleanup-rework/` preserves the reviewed-source RED (six lost acknowledgements, two lifecycle controls and nine nested-matrix subtest failures), the restored native suite, twelve actual mutation receipts, original assertion integrity and the refreshed-main checks. Its separate verifier keeps the initial `receipts/final-evidence.json` and five-million fixture receipts historical. The broad initial cleanup RED also contained one error; the focused six-case RED has six body assertion failures and zero errors. The collision fault-seed RED demonstrates test sensitivity, rather than claiming an additional bug in the reviewed source.

Reproduce the bounded repair from the root of an owned worktree with no competing Mike fixture. First verify the published packet in an unchanged checkout:

```sh
python3 next/benchmarks/vector_baseline/receipts/cleanup-rework/verify-evidence.py
```

For a fresh reproduction, run the following commands separately after that verification:

```sh
uv sync --project next/benchmarks/vector_baseline --frozen --python 3.12
B01_PODMAN=1 PYTHONPATH=next/src next/benchmarks/vector_baseline/.venv/bin/python \
  -m unittest discover -s next/tests/benchmarks -v
PYTHONPATH=next/src next/benchmarks/vector_baseline/.venv/bin/python \
  next/tests/benchmarks/mutate_vector_baseline.py
```

The mutation producer replaces raw outputs with fresh timings and temporary paths. Freeze those fresh receipts into a new manifest before checking them; the published manifest verifies the original packet and will reject overwritten outputs. Keep the published packet available in an unchanged checkout for comparison.

The mutation producer uses the vendored synchronous receipt helper for these synchronous `TestCase` targets; it does not qualify that copy for arbitrary async fixtures. C01/shared-helper integration checks additionally require the pinned `next/requirements-test.txt` packages in the same environment. Vera owns re-review and closure. Product-engine verdict remains UNDECIDED.

## B02A GTM offered-load harness

The [accepted B02 plan](B02-PLAN.md) is narrowed by Kai's02:05 ruling to Postgres first, the141,511×768 Marlow export as geometry-only, both editions and confidence windows. Native hosts, transfer/decryption and Mac OS-cache clearance require the CTO's morning gate. This implementation admits **local synthetic data only**, with zero provider calls. It does not import the export, install a product or select an engine.

```sh
PYTHONPATH=next/src next/benchmarks/vector_baseline/.venv/bin/python \
  -m vector_baseline.corpus next/benchmarks/vector_baseline/synthetic-b02 \
  --count 24000 --dimension 8 --model synthetic-b02-sql-fixture-8
PYTHONPATH=next/src next/benchmarks/vector_baseline/.venv/bin/python \
  -m vector_baseline.benchmark next/benchmarks/vector_baseline/synthetic-b02 \
  /absolute/new/b02-sql-diagnostic.json --duration 5 --warmup 0
PYTHONPATH=next/src next/benchmarks/vector_baseline/.venv/bin/python \
  next/tests/benchmarks/mutate_b02.py /absolute/new/b02-mutation-receipts
```

Eight separately opened persistent async PostgreSQL sessions serve a fixed40RPS round-robin schedule. The scheduler does not await completions; queue wait starts at scheduled arrival. Overdue arrival slots are explicit misses. Requests have a10-second absolute scheduled deadline, including time waiting in the client queue. Errors/timeouts/misses remain in throughput and nearest-rank p95; fast errors retain actual response elapsed time and separately use deadline-or-later latency accounting. Warmup uses the same sessions but stays outside the measured denominator, and its errors prevent diagnostic PASS. Offers scheduled inside the interval count; drain time never enlarges the throughput denominator. Cooperative cancellation is required.

Full exact B01 truth is calculated before offered load. Every returned response is checked against its own query's truth, including duplicates/forbidden/short/empty cases. Repeated cycles are not additional distinct recall queries. Per-query averages and failed observations are retained; no empty-only recall PASS. Natural EXPLAIN and complete per-offer timestamps/IDs/session identifiers are preserved. Geometry/dense SQL timing excludes API/auth/provider/cache/hybrid and remains diagnostic even if its recall/throughput/latency checks pass.

Owned resource observations run without blocking the arrival loop and retain lifecycle-bound sanitized PG stats/database size. Their scope is **PG fixture only**; whole first-release8GB/native resources are explicit missing gates. No environment/DSN/password is stored. Reuse the accepted pending-create cleanup; the report is finalized after owned resources are removed, the credential discarded and the lock released. Existing evidence destinations refuse overwrite. The local5-second/200-offer example is a shortened proof, never a native confidence run.

Defaults300s measured/60s warmup are the proposed GTM windows, with three independent runs per populated dense/filter/cache/edition. Six strata/two caches/two editions imply6 aggregate measured host-hours plus0.6 warmup hours, before setup/index/oracle/reset costs. Missing strata or OS-cold evidence remain NOT_RUN. The old full freeze/soak belongs to B03/B04 by Kai's ruling. No IID confidence or semantic/model/provider equivalence is inferred from repeated document-vector queries. Vera independently reviews the harness and results; Kai rules the engine gate.

Mutation execution uses the canonical `next/tests/test_receipts.py` async-aware phase classifier. Actual recipes run on temporary source copies, with imported module paths/hashes verified inside each child; this is not Git-context-negative qualification. Every kill requires exactly the named test body AssertionError(s), one method, zero errors, and clean/restored27-method B02 baselines. Original source remains unchanged. The initial missing-surface22-assertion RED, fixture-only READY correction and three later behavioral RED controls are retained separately in the author pack; all original assertions are preserved.

Async adapter reference: [Psycopg concurrent/async operations](https://www.psycopg.org/psycopg3/docs/advanced/async.html). The runtime pin remains3.2.9; actual cancellation/session behavior requires the recorded fixture run rather than documentation alone.

Author preflight adds four preserved RED controls: stats processes have a5-second communication deadline and are killed/reaped on timeout/cancellation; omitted expected held-out query IDs remain explicit NOT_RUN; interrupts propagate after saving a named FAIL receipt and cleanup flags. Actual mutation recipes include each repair. The original22 methods remain unchanged.

Mutation custody resolves both imported paths and expected roots, verifies all six package modules, rejects foreign or wrong-hash imports, and creates/removes mutant copies under the owned receipt destination. The macOS path-alias admission failure is retained as INCONCLUSIVE; the additional27th control was committed RED before this correction.


## H-D472 B02 both-arm tooling amendment

The [both-arm plan](B02-BOTH-ARM-PLAN.md), accepted by Kai10:42/11:00 on10October, adds a geometry importer and a Qdrant arm. This amendment supersedes the earlier Postgres-only benchmark scope. Product C09 is a separate held unit. Author execution remains synthetic only.

```text
custodian JSONL boundary → geometry.py → disjoint candidate/tuning/heldout + hashes
                                     → independent full canonical oracle
same heldout queries +40RPS/8 clients → PostgreSQL SQL arm
                                    → isolated Qdrant HTTP proxy arm
                                     → diagnostic report / engine UNDECIDED
```

Geometry JSONL is an explicit proposed custodian interchange, not a claim about an unread export. Every row has exactly `id`, `project`, `type`, `month`, `vector`: lowercase64-hex pseudonymous ID; project pseudonym or null; bounded coarse type or null; YYYY-MM or null; one finite canonical float32 vector with a normal nonzero cosine norm. Private text, matrices, unknown fields, duplicate IDs and wrong count/dimension refuse. Provider/model, tenant/auth, deletion, semantic queries and sparse features remain unknown. Stored vectors are preserved; no embedding provider is called.

The importer streams vectors to a temporary file, then emits memory-mapped candidate arrays. Within each joint project/type/month category containing at least3 rows, a deterministic seed/ID hash reserves one tuning and one heldout document vector; both are physically excluded from candidates. Smaller categories remain candidates with query coverage NOT_RUN. Null categories and all joint category counts are recorded, together with marginal project/type/month input/candidate/tuning/heldout totals and query-coverage status. Candidate ID order, stored float32 geometry and metadata bind the identity. Queries and the corpus manifest are separately hash-bound. Full oracle scoring remains outside timed execution. Missing selectivity cells remain NOT_RUN; document-vector queries cannot establish semantic search quality.

Use a new output path and a synthetic source of the declared shape:

```sh
PYTHONPATH=next/src next/benchmarks/vector_baseline/.venv/bin/python \
  -m vector_baseline.geometry /absolute/synthetic-geometry.jsonl /absolute/new/geometry-corpus \
  --dataset synthetic --dimension 768 --expected-count 256
PYTHONPATH=next/src next/benchmarks/vector_baseline/.venv/bin/python \
  -m vector_baseline.benchmark /absolute/geometry-corpus /absolute/new/pg-report.json \
  --engine postgres --cell scope/dense --duration 5 --warmup 0
PYTHONPATH=next/src next/benchmarks/vector_baseline/.venv/bin/python \
  -m vector_baseline.benchmark /absolute/geometry-corpus /absolute/new/qdrant-report.json \
  --engine qdrant --cell scope/dense --duration 5 --warmup 0
PYTHONPATH=next/src next/benchmarks/vector_baseline/.venv/bin/python \
  next/tests/benchmarks/mutate_b02_both_arm.py /absolute/new/both-arm-mutations
```

Qdrant OSS1.19.2 and the stdlib Python client image are pinned by Linuxamd64/arm64 child manifests. Two owned nonroot containers share an internal network with no published port. The API key is a0400 mounted secret, never an argument/environment dump/report. Root filesystems are read-only; capabilities are dropped and privilege escalation refused. Qdrant uses768MiB/1.5CPU and the proxy256MiB/0.5CPU: aggregate1GiB/2CPU. Fresh memory>=35% and an empty team test slot precede creation; each start repeats admission allowing only verified own names. Pending creates are reconciled after lost acknowledgements. Cleanup attempts every owned resource and verifies absence before releasing Qdrant credential custody and its serialization lock. Mike's inherited PG cleanup repair B01-LOCK-001 remains separately owned; this delta applies Nemo labels and resource admission without claiming that repair.

Eight persistent proxy processes inside the isolated client container speak bounded sequence-framed HTTP operations; request cancellation and partial readiness close/reap owned CLI processes. Errors disclose class names only. Both container observations are retained. Qdrant preserves scope/generation/delete/ordinal/type/month predicates and maps numeric point IDs back to stable comparison IDs. Imports verify runtime version and exact count; writes are never retried after a partial import. HNSW configuration is recorded, while index use remains unqualified for tiny fixtures.

Postgres timing is scheduled arrival to SQL response. Qdrant timing is scheduled arrival to proxy HTTP response **including local IPC**. Neither is native product HTTP/auth/provider/cache latency, and the different adapters do not establish engine equivalence or a winning engine. Defaults and report acceptance remain unchanged; short author runs do not prove confidence windows. Whole first-release8GB stack sizing remains separate. The1GiB author fixture does not prove capacity for141511×768 native data.

Real preparation/reading is disabled by default. Optional `--dataset marlow-geometry --admission RECORD` requires a reviewed record before source access: schema `cortex-b02-data-admission-v1`, exact dataset/count141511/dimension768, input SHA256, import review SHA, named CTO decision and ren-tk custody receipt. Authority must be independently verified by the operator; a JSON shape check cannot grant it. Real benchmark execution additionally requires native host/resize receipts, edition/cache mode and finite positive cost cap<=3USD; Mac cold refuses. Linux cold/warm and Mac warm, real data custody, native capacity, B01-LOCK-001 and CTO run authorization remain open. No real data is part of author verification.

The both-arm mutation producer runs the full benchmark suite clean/restored and literal changed-source probes against frozen controls. It binds every tracked/nonignored `next/` file to the Git head/tree and verifies all9 executed package modules inside each child. Only the exact named test body AssertionError inventory, with zero errors/skips, is a kill. The inherited parent producer keeps its23 recipes and27 controls unchanged and expands its imported-source collector to the same9-module package. Default inherited live tests remain explicit skips unless separately enabled with fresh resource admission.


### PR50 input-lineage and import-parity gates

Geometry preparation now emits `frozen-input.json` and returns/prints its SHA256 before any engine run. An import reviewer/operator retains this value independently of the artifact. Pass that retained value as `--frozen-input-sha256 "$REVIEWED_GEOMETRY_SHA256"` for synthetic geometry benchmark commands; native admission additionally carries `frozen_input_sha256`. Never compute the trusted expected digest from the current artifact at benchmark time. The frozen record binds source input/identity, candidate file bytes, both query files, split/count/order, and source ID/canonical float32 vector digests. Rehashing mutable manifests cannot replace that anchor. Existing artifacts need a fresh independently reviewed preparation/freeze; no legacy implicit acceptance.

An unpopulated valid cell emits an input-bound NOT_RUN row with query IDs and reason, without an engine start. Qdrant imports require exact count plus deterministic first/middle/last point readback (at most3), complete ID/predicate/generation payloads and collection vector/HNSW/index schemas. Cosine readback compares normalized stored-float32 geometry at frozen rtol1e-5/atol1e-6; dot/Euclidean readback is exact. Parity mismatch emits NOT_RUN before timing; failed cleanup remains FAIL. Tiny readback samples are diagnostics, not proof of full native dataset parity or HNSW use.
