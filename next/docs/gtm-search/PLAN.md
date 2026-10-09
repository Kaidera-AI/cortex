# H-D455 Postgres search slice execution plan

Authority: Nemo DO NOW H-D455 (2026-10-09 21:06), H-D450, Kai accepted C01 draft (r426 15:29). Boot artifacts are pre-approved. This implements the first dispatched slice only; Mike reviews and Kai accepts. No self-merge or release.

## Scope and order

1. Commit this plan and pre-edit receipt, then a failing integration test.
2. Add `next/schema/retrieval/001-pg-search.sql`: stored embedding projection, HNSW cosine index, current source revisions, atomic model identity/state and forced tenant/project RLS. Embedding identity includes provider/model/version/dimensions/preprocessing. Fixed 768 dimensions match the current hosted default; other dimensions are explicitly refused pending a migration.
3. Add `next/src/cortex_core/embeddings/pg_search.py`: bounded asyncpg adapter, current authorization callback required on each operation, typed unavailable/Core outage, explicit freshness counts, tenant/project filters, stale embedding rejection. Core writers call revision notification in their transaction; projection rows return IDs for Core hydration, never stale text.
4. Add `next/tests/integration/test_pg_search.py` and `next/tests/integration/run_pg_search.py`: fresh isolated Podman Postgres, synthetic fixtures and non-bypass runtime role. Prove cross-tenant/project exclusion, current revocation, disabled/rebuilding vs empty, freshness, stale model/revision rejection, forced RLS, HNSW plan, invalid vectors and Core outage. Run targeted source and SQL mutants and retain receipts.
5. Fetch/merge current target tip into this branch, rerun, push this task branch and open one PR to main; return to Kai for Mike's review. Continue with P01 afterward.

## Contract seam

C01 draft SHA256 `4b9e58be0219926c760c62b799974c63991c5fbbe751b6833c8889eb5cfe8de4`. GET/POST `/search` remain Cox-owned routes; this slice is the adapter beneath them. No invented authentication or header authority. Cox C04 supplies a callable that rechecks Core grants within the request transaction and returns an immutable tenant/project scope. C11 binds the legacy success envelope; exact C02 wire compatibility remains a gate. The internal error code is `capability_unavailable`, capability `search`, with reason and freshness. Core outage is `core_unavailable` and never a successful empty result. The adapter deliberately has no unauthenticated/default authorization path.

## Risks and proof limits

HNSW is approximate; Mike owns Marlow recall/latency acceptance. Synthetic tests prove behavior and actual index use, not production scale or release acceptance. Revision notification must join Cox's record-write transaction; the hook alone does not prove C07 integration. Current permission checks happen before result publication; no authorization cache. Generation changes serialize against embedding writes and reads. No live credentials, live PG (:5499/:5500), live API (:8501), SSD bind mounts, other engines or legacy source modifications.

## Disposable container contract

Result: PASS. Name: kaidera-test-pg-search-1. Role: pg-search; project helix Cortex; environment test; singleton per worker session with a unique label. Canonical form: fleet; ordinal 1. Ephemeral storage, 2 CPUs/1 GiB memory, one container, loopback random port, no host bind mounts; cleanup in finally. Reuse the already cached upstream arm64 pgvector image by immutable digest; record its actual PG/extension versions during tests. Local experiment only: image rebuilding/multi-platform release is out of scope.

## Pre-edit evidence

Base: `04a26d0bf40c07ed3082328d3cc42c5a72ca8411` from freshly fetched origin/main. Worktree owned by nemo; `git status --porcelain` was empty, rc 0. Shared Cortex checkout had only untracked legacy docs/v2/cache, no next paths. All recent refs were compared for declared search paths; disconnected refs were independently checked with ls-tree and contained no next paths. Full refs and overlap receipt live under main docs lane-F/gtm-search-pre-edit. Existing worktrees belong to ren-cx/ren-kos/Kai and are not edited. No open next/search PR observed. Python/environment versions captured by the runner.

## Verification amendment (before acceptance)

The first actual-engine run found permission-denial misclassification (PermissionError is an OSError) and a one-row HNSW fixture that legitimately planned a cheaper B-tree. The source error is fixed against unchanged auth assertions. The HNSW fixture is strengthened to 2,001 stored vectors plus ANALYZE; its exact HNSW assertion is retained. The original failing output is retained in lane-F/gtm-search-evidence/partial-green.log. This is synthetic index eligibility proof; production planner/recall acceptance still belongs to Mike.

Verification tooling includes `next/tests/integration/mutate_pg_search.py` and a slice-local `search-requirements.txt` (asyncpg 0.31.0); Cox owns the shared next requirements file. Formatting is mechanical; no assertions or production acceptance thresholds are removed.

## Review round 2 plan — Kai 21:51, B01-PR29-1..4

Mike reviewed c42cd05a; Kai requires these fixes FIRST, then restack P01, then C08. Add RED-first checks in `test_pg_search_review.py` and `test_pg_search_tools.py`; retain Mike's original underflow probe. Fix `pg_search.py` by canonical float32 conversion, rejecting component overflow/underflow and unusable float32 cosine norm; retry each entire public authorized PG transaction at most three attempts, rechecking grants and snapshot each time, with typed concurrent_update refusal on exhaustion. In-transaction Core hooks remain inside Cox's caller transaction and do not retry in isolation. Official pgvector v0.8.2 vector.c uses float32 norm accumulators (VectorCosineSimilarity), so test norm-product overflow/underflow as well as value conversion.

Fix `run_pg_search.py`: any removal failure or remaining labelled resource is a failing run. Allow only the internal fixed mutation test-target seam; final acceptance still runs the full search suite. Fix `mutate_pg_search.py`: clean full baseline first, then named single-case semantic probes; require the expected behavioral assertion failure, reject setup/infrastructure/unrelated errors; final unchanged full suite confirms restoration. Record per-mutant targets and outputs. Add a monotonic-revision assertion probe so revision mutation gives an explicit assertion rather than an unexpected downstream error.

Scope: these source/tool files, two regression test files, and this plan only. New findings use same original PR/branch, no self-closure; Mike re-reviews exact new head. Fetch/merge target main, rerun RED/GREEN/mutations, push, restack PR31 and rerun its full suite. Check inbox before resuming paused C08 checkpoint 3aabb8e. H-D458 applies; explicit worker label, one <=1GiB/2CPU disposable PG per run, cleanup each time.
