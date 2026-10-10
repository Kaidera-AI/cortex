# B02A: GTM Postgres load and receipt harness

Owner Mike; independent reviewer Vera; adjudicator Kai. **ACCEPTED FOR BUILD** by Kai's02:05 DO NOW/addendum under H-D455, accepted input plan SHA0ec213c0. This requested execution amendment implements the narrowed scope. Initial proposal and216+18hour arithmetic remain immutable in the37-member preflight pack; they are historical B03/B04 inputs, not B02's active gate.

```text
synthetic frozen corpus/oracle ->8 persistent clients ->40RPS fixed arrivals
  ->queue/deadline/error/recall/resource observations ->Vera review
CTO-admitted141511×768 geometry +both editions ->GTM PG evidence ->Kai ruling
```

## Active boundary

Postgres ONLY. Tonight builds/proves the harness on synthetic data in ONE reviewed disposable local stack,<=1GiB/2CPUs, worker=mike, fresh lifecycle/storage/loopback port, removed after every run. Reuse B01's accepted pending-create/owned cleanup; never touch cortex-* or harness-*. No real-data read/transfer/decrypt, remote host, sudo, install, paid provider or cache reset. B01 source was merged at1f6f522d; new owned branch starts from fresh main67265f371a8829d12511c082b7792a1441ddcd77.100 recent refs checked;62 unrelated histories have no declared path in their full trees; no other active overlap, all raw evidence in the build pack.

GTM native dataset is the141511×768 Marlow export, **GEOMETRY-ONLY**, expressly accepted by Kai; held-out document vectors become queries, with query IDs excluded from searched corpus. Preserve coarse/project/null/type/month coverage and full exact eligible truth; missing strata remain NOT_RUN. No tenant/auth, semantic query, sparse, delete, generating model or provider equivalence is invented. Those product gates remain explicit. Both native editions are required. Qdrant stays deferred and runs only after an admitted measured PG miss is adjudicated by Kai. Full two-dataset freeze and24-hour soak move to B03/B04 before0.1.011 per the02:05 ruling.

Hosts/data wait for the CTO in the morning: Linux uses Ren-KOS's disposable Rocky x86_64 pattern, executed by Ren-KOS after attempt5; Mac is192.168.1.140 with a CTO-approved cache-reset/window; transfer/decrypt with Ren-TK needs its named go. These are execution gates, not prerequisites for tonight's synthetic authoring.

## Files and order

| Path | Change and purpose |
| --- | --- |
| `next/benchmarks/vector_baseline/B02-PLAN.md` | Copy the accepted byte-bound source plan and amendments into the source branch. |
| `next/src/vector_baseline/load.py` | New fixed open-loop scheduler with eight persistent transport instances, monotonic scheduled arrivals, queue-inclusive latency and bounded request lifetime. |
| `next/src/vector_baseline/report.py` | New per-cell/run accounting and prerequisite classification; full/tie/strict/safety results; nearest-rank p95 and one-minute windows. |
| `next/src/vector_baseline/benchmark.py` | New configuration/input admission and orchestration; injectable transport seam, explicit fixture/SQL diagnostics; reject product execution until exact auth/API/provider/native bindings are complete. |
| `next/tests/benchmarks/test_b02_load.py` | New frozen deterministic-clock controls for scheduling, stalls, deadlines, persistent clients and cancellation/cleanup. |
| `next/tests/benchmarks/test_b02_report.py` | New hand-computed boundaries, missing cells, failed safety, failed throughput and honest NOT_RUN/UNDECIDED controls. |
| `next/tests/benchmarks/mutate_b02.py` | Actual named behavioral mutations for each new production source file, admitted by the canonical `next/tests/test_receipts.py` helper, including its reviewed async fixture boundary; do not extend B01's synchronous vendored helper to async targets. |
| `next/benchmarks/vector_baseline/README.md` | Exact commands and qualification boundaries. |


B02A adds no API/server/schema, real-data importer or dependency. Keep numpy2.3.4/psycopg3.2.9 under Python3.12. An injectable client port supports an actual async PostgreSQL synthetic adapter now. Each client object is tracked BEFORE open; close runs after partial-open, request, cancellation or collection failures. Psycopg async connections are separate persistent sessions; one shared connection cannot represent eight clients. Concrete HTTP/product/native import bindings require later byte-bound amendments, not a fake success.

1. Commit this plan plus immutable tests; actual initial RED must fail at explicit missing-surface assertions, not fixture/import errors.
2. Implement load/report/admission with tests unchanged; run GREEN, named actual body mutants, full B01 and affected shared/contract consumer suites.
3. Run a short actual synthetic PG offered-load diagnostic with eight sessions, natural plan and source/query/oracle/runtime/owned-resource bindings; shortened windows remain diagnostic. Collect explicit PG-only versus absent whole-product observations. Preserve expected overload/deadline/safety failures in tests.
4. Integrate fresh main, rerun appropriate checks, freeze all raw/source maps and archive. Kodus author second opinion, disposition as proposals only. Push one owned task PR; Vera independently reviews. No author approval/merge.

## Workload and accounting

Fixed40RPS, eight persistent clients,25ms round-robin arrivals. Scheduler is independent of completion. Timestamp latency from SCHEDULED arrival to complete response; include dispatch and client queue. Arrival lag beyond one25ms slot becomes an explicit missed offer; do not shift/catch up the schedule. Absolute10s deadline covers queued and executing work; every failure/missed/deadline observation stays in latency and throughput accounting. Cancellation propagates; cooperative ports required, no forcibly bounded indefinitely blocking callback claim. Offers scheduled inside the interval count and drain only to their deadlines afterward; measured duration remains fixed. Warmup is separate but uses the same sessions, with its failures visible outside the measured denominator. Cleanup errors fail the run.

Native confidence-window proposal for Vera/Kai: three independent5-minute measured runs per populated dense/filter cell/cache/edition, with separate1-minute warmup for warm runs. Each run supplies12000 offered observations and keeps worst-run nearest-rank p95, one-minute windows, scheduler lag and error counts. Repeated queries are not independent new recall evidence: compute exact held-out per-query recall once, retain strict/tie/full truth and safety, and report repeated observations separately. Report variability across the three runs; no IID confidence or soak claim from correlated arrivals. Numeric recall>=.95 and100ms Linux/200ms Mac timing budgets remain visible with **geometry/SQL** qualification; these do not close missing hybrid/API/provider gates.

For six populated filter strata, one dense geometry mode, two caches, three runs and two editions:72 measured intervals, **6 aggregate host-hours**, plus36 warmups = **0.6 aggregate host-hours**. Build/index/oracle/preflight/reset/cleanup time is extra. Missing filter/cold rows are NOT_RUN and reduce observed time, not required coverage. A short local proof (for example5s,200 offers) never qualifies that matrix. Full held-out dense query coverage and exact predicate/selectivity counts stay mandatory; native query-sampling detail is bound before data access. Changing windows after seeing a RED needs a reviewed amendment.

Use nearest-rank p95 on all offers; successes floor38RPS; independent zero-error requirement. An empty-only cell has no recall PASS. Mean tie-aware recall>=.95 per cell/run plus strict recall, all short/empty/forbidden/duplicate/deleted/wrong-generation/cardinality controls. Any safety failure fails despite a high mean; no pooling across cells/runs. Snapshot geometry lacks authoritative tenant/delete/model provenance and cannot be advertised as the full product contract.

## Fail-capable proof

Frozen tests catch completion-relative arrivals/coordinated omission, omitted failures/deadlines, queue-exclusive latency, incorrect p95 rank, throughput calculated over drain instead of scheduled interval, partial clients, warmup contamination, missing rows/empty-only recall, safety hidden by a mean, missing bindings promoted to acceptance, partial-open cleanup and secret-bearing error text. Actual mutants cover each new production source with named expected body AssertionErrors/zero fixture errors. Preserve every initial assertion AST.

Resource observations bind the owned synthetic PG container and exact lifecycle, queried version/platform/limits, sanitized stats and database size. Scope is PG-fixture-only; API/provider/cache/Conductor/backup/rebuild/whole8GB native stack observations remain missing. Missing collection is visible and prevents measurement qualification. No raw env/DSN/password/private vectors are written. The stack discards credential/releases lock after every failure; driver report is saved after final cleanup, with cleanup failure retained.

## Risks and review

Main risk is misleading measurement boundary/accounting, controlled by unchanged tests and exact per-offer evidence. Shared engine pressure is bounded by1GiB/2CPU, one Mike stack. Data/cache/remote native actions remain CTO-only; no source-only pass crosses them. Vera reviews harness and results, Kai owns engine/routing decisions; source author never closes those gates. Daily author slice is six focused hours; native confidence windows are separately scheduled, not a one-day soak claim.

## Amendment custody

2026-10-10 02:05: Kai accepts BUILD and supersedes original two-dataset/full-freeze B02 proposal with Postgres-only141511×768 geometry, both editions and confidence windows. Explicitly retains remote/data/sudo CTO gates. This source amendment follows that instruction; it does not claim new approval for host execution. Permanent historical preflight remains sealed and unchanged.

2026-10-10 verification amendment: initial19-test surface RED retained22 body assertions including4 subcases. One oracle fixture lacked status READY and raised KeyError in the first implementation run; add that field without changing any original assertion. Three additional frozen RED controls cover fast-error deadline accounting, blocking resource observation and warmup failure. Their pre-fix source bytes/raw RED3 are retained; these controls precede the respective source fixes. Scope stays within the accepted eight paths.

2026-10-10 advisory amendment: freeze four additional RED controls before fixes: actual stats subprocess timeout/reaping, actual collector cancellation/reaping, omitted expected held-out IDs remain NOT_RUN, and interrupts name the failure class in the saved FAIL receipt while propagating. This closes author preflight findings without claiming independent Vera acceptance. Same eight-path scope.

Mutation admission amendment: the first actual child produced its intended assertion, but custody correctly refused a noncanonical macOS /var versus /private/var alias. Preserve this as INCONCLUSIVE, not an admitted kill. Freeze an alias-and-foreign-path RED control before introducing canonical resolved-root custody; actual mutant temporary copies stay under the owned receipt destination. No assertion weakening or Git-context-negative claim.
