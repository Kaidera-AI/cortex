# H-D455 P01 execution plan

Nemo's dispatched second slice, artifact pre-approved by Cortex boot rule; Mike reviews, Kai accepts. Base is the first slice task branch (PR29), so this is a stacked PR with only provider/cache changes. No self-merge. Current source and v0.1.003 manual candidate declare OpenRouter as hosted default, current default model `nvidia/llama-nemotron-embed-vl-1b-v2:free`, 768 dimensions. Source authority is configuration plus the existing provider-key resolver, not a new provider or new credential store. Exact released/runtime settings remain Cox C02 binding, not inferred from defaults.

## Files, order and proof

1. Commit plan; commit RED integration/hosted-transport tests.
2. `next/src/cortex_core/modules/providers/hosted.py`: bounded OpenRouter adapter; caller supplies immutable embedding identity and current credential resolver; response shape/dimensions/finiteness validated; errors scrubbed and typed; no new provider dependency or real key in tests.
3. `next/schema/retrieval/002-query-cache.sql` and `next/src/cortex_core/embeddings/query_cache.py`: tenant/project forced RLS; cache key includes current permission generation, complete identity and SHA256 of the exact processed query. TTL (30 minutes default), bounded rows per tenant/project, cross-process claim with lease plus monotonic fence, bounded waiting; no Core connection held during provider HTTP. Authorize before lookup and before publishing/returning a provider response; revocation/mixed generations refuse. No query text, credentials or failure payloads stored.
4. `next/tests/integration/test_query_cache.py`, `run_query_cache.py`, `mutate_query_cache.py`, provider-requirements.txt: synthetic transport plus disposable PG. Prove warm reuse across cache instances/restarts, cold concurrency coalescing, TTL/permission/model misses, tenant collisions, revocation mid-flight, late lease response, errors not cached and finite vector validation. Run one meaningful semantic mutation for each application source and SQL migration; preserve literal receipts.
5. Fetch/merge target tip, rerun full search + P01 suite; push only Nemo P01 branch; one PR, one return; remove parked worktree. Then C08.

## Interfaces and boundaries

Cox authorizes per operation and supplies tenant/project plus nonempty permission generation. Cache authorization is never trusted as grants. Configuration identity and provider request use the same snapshot. Preprocessing is explicit: exact input text bounded to 16 KiB UTF-8; no hidden truncation or cross-query normalization. The cache has no API route or credential fallback; C11 connects it to query embedding. HTTP compatibility remains C02/C11's gate. Model dimensions other than 768 require a separate schema migration and are refused now.

## Risk and pre-edit evidence

Postgres is authoritative but query cache is disposable; loss causes provider recomputation, not record loss. Provider calls may still repeat after a failed/expired lease; fence prevents late writes, no exactly-once claim. HTTP timeout < lease; caller wait bounded; cancellation releases claim. No external HTTP call performed in verification. No live DB/API, no SSD bind mounts. Reuse the prior immutable disposable PG runner (one stack, 2 CPUs/1 GiB, fresh tmpfs, non-root, loopback random port and cleanup). Core query/write routes remain independent from cache and Conductor.

Worktree `/Volumes/WD-B-4TB/DevVault/helix/.worktrees/nemo-gtm-provider-20261009`, clean at creation except this new plan. Shared checkout unchanged. Existing refs/worktrees inspected; no `query_cache.py`, hosted provider module or 002-query-cache migration present on another task ref. Cox's in-flight C01 owns contracts.py/contracts/requirements-test; those paths are excluded. Search branch PR29 is the only intentional dependency.

## Verification findings folded into plan

The unchanged cross-process singleflight test exposed REPEATABLE READ claim serialization; bounded retry uses a fresh grant snapshot and no provider call on a lost claim. A RED-first test (`8910589`) exposed unbounded credential lookup; one 8-second total hosted timeout now covers resolver, HTTP and streamed JSON. Response parsing stops above 256 KiB. The test runner gains a pattern argument so P01 runs the complete search + provider/cache suite. No search application code or search assertions changed.

A second RED-first check (`b577247`) proves concurrent distinct cache keys can overflow capacity under stale REPEATABLE READ snapshots. The existing authorized PG context in `next/src/cortex_core/embeddings/pg_search.py` gains a private isolation argument (search default remains repeatable-read). Cache lookup/admission uses read-committed so the scoped count is read after its advisory lock; publishing still rechecks current authorization. Its semantic mutation forces the old isolation and must fail this unchanged check. Scope amendment includes only that shared context parameter, not search semantics or assertions.
# Review re-run — Kai 21:58

Restack on published PR29 ab84b7e, preserving P01's read_committed admission path.
Add RED-first tool checks for a clean unmodified baseline and rejecting setup errors.
Reuse PR29's named-assertion classifier: every mutation selects its expected test,
keeps raw output, reports inconclusive failures separately, and verifies full suite
restoration. Run full search/review/provider/cache checks, then the eight P01 mutants;
push the same PR31 branch, return receipts to Kai for Mike, remove the parked worktree.
No provider/cache behavior change is planned beyond inherited PR29 fixes.

Tool RED commit f26be4e reproduces both unsafe mutation admissions; raw output is
lane-F/pr31-mutation-rule-red.log. After the tool change, the clean baseline and
restored full suites run 50 checks; eight P01 mutants each fail their named expected
assertion. Raw target/status receipts live in lane-F/pr31-restack-mutations/.
The cache admission isolation remains read_committed; search remains repeatable_read.

