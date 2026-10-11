# C07 source proof and versioned recovery

C07 was merged forward from C06 PR #51 head
`cd540850ca61a8b154384c4e13d3c7daff08dde6` without rebase. The final
copied native receipt is `mutation-c07-final-005.json` at source
`a9447fa9e655a47e0240879549817c97fec77344`: 27 clean suites/347 tests;
three archived 28-table guards fail in their bodies before the amended
31-table guards pass; all 15 C07 faults are killed with 22 expected BODY
assertions, zero errors/inconclusive; all 439 copied NEXT files are restored
and all three owned resources removed. `verify-c07-r4.py` independently checks
the raw receipt, Git source, C06 ancestry, migration hash, wheel bytes,
classifier outcomes and cleanup. Earlier raw receipts are retained as the
RED/focused/repair history. The bounded sparse-outcome capacity and completed
outcome immutability controls were each committed RED before their fixes.

The C07 migration is additive to the eight prior migrations and creates
`consumer_project_checkpoints`, `consumer_event_outcomes` and
`consumer_aggregate_heads`. All three are durable recovery state. Once a real
consumer has registered, do not down-migrate, delete these rows, reset a cursor,
or reuse a generation. Before adoption, a disposable fixture can be discarded.

## Recovery version 1: expired consumer or failed target generation

1. Stop the affected `(installation, module, tenant, project)` runner. Preserve
   its checkpoint generations, outcome/head rows, unresolved quarantine, C06
   published journal and retained floor, module manifest, and target generation
   ledger. Keep the old target generation fenced from writes. A `scan_cursor`
   proves observation only; use `applied_cursor` to assess completeness.
2. Obtain a consistent Core snapshot at cursor `S` for that exact project and a
   new durable target generation built from the same snapshot. Require
   `retained_floor <= S <= published_head`. Verify the target's aggregate
   revisions, tombstones and exact payload bytes at `S`; a Core-only snapshot
   marker is insufficient. If the project inventory or snapshot cannot be
   proved complete, keep the consumer blocked.
3. With the scoped control credential, call
   `ModuleConsumer.begin_rebuild(snapshot_cursor=S, generation=G+1)` where `G`
   is the highest recorded generation. This inserts a new checkpoint generation
   and expires the prior one without rewriting it. The port refuses an old or
   out-of-range generation and refuses `S` below the previous scan cursor while
   that generation remains active. Do not bypass that refusal: use the later
   full shadow-rebuild procedure if a lower active-generation snapshot is
   required.
4. Replay bounded project pages after `S`. The sink must persist each target
   effect and revision/tombstone before Core records its outcome. Repair poison
   by event ID only after the exact event is available and the target fault is
   resolved. Watch `applied_cursor`, `scan_cursor`, retained `floor`, lag,
   oldest poison and pending outcome count separately. A sparse scan is not a
   complete checkpoint; capacity refusal preserves existing outcomes.
5. Keep the new generation out of production routing until its target and Core
   status agree at the chosen boundary. Full timed shadow rebuild, alias
   cutover and production service admission belong to X03/C11. C07 does not
   certify those steps and never promotes an installation rollup on the basis
   of one project's page.

If migration SQL has reached a non-disposable database, code rollback means
stopping the new consumer and restoring a compatible version that understands
the durable tables. Do not run an older binary that treats the installation
checkpoint as complete or drops the new tables. Keep a versioned forward
migration/rebuild available; no destructive reverse migration is part of C07.

## Evidence replay

`run-core-pg.py --preflight-only` proves all copied inputs, frozen table guards,
fault anchors, source closure and eight offline wheels before any resource is
created. The native controller uses one isolated arm64 PostgreSQL 18.4 /
Python 3.12 stack, two CPUs and 1 GiB, no ports or bind mounts, and removes its
exact owned resources. It runs the three original 28-table guards RED against
the additive ninth migration, restores the amended 31-table guards, runs every
inherited suite and the C07 module suite, then executes actual fault recipes
under the fixed active test-body classifier. Final raw receipt and verifier
are linked from the C07 return and PR.

## Integrated PR71 C07 finding repair (historical at7d0c458)

The earlier C07 receipts above remain historical. The repaired integrated source is `4cf4c8080c66d4a3186733e83d39591074029c92`, with current main `903fd729c56cf719bbb15e9f986a70b9160497c1` incorporated. Current proof is `mutation-c07-review-003.json`: 27 suites / 379 native tests, 35 consumer tests, 24/24 body-killed faults with 76 expected assertions, no errors/survivors/inconclusive results, 480 copied inputs restored, three resources removed. Three frozen old-table assertions remain intentional RED lineage, separate from the clean suites.

`c07-review-red-002.json` records 34 tests / 55 body failures / zero errors before the four fixes; `c07-review-green-001.json` records their 34 clean tests. `c07-review-red-scan-001.json` adds the observed-range control: 35 tests, one body failure before the scan-boundary fix. `mutation-c07-review-001.json` remains FAILED (one INCONCLUSIVE legacy poison-fence abort); attempt 002 passes 23 faults but precedes the additional scan-boundary control. Neither is substituted for the final run. An unexpected ConsumerError from the specifically asserted normal cycle now fails that test body; setup/import failures remain INCONCLUSIVE and the shared classifier is unchanged.

The unchanged Mike AST controls are in `frozen/mike-repair-manifest.py`; `run-mike-repair-controls.py` supplies only constructor-owned module_id and observed scan cursor omitted by the original doubles. `reviewer-source-red.log` is three tests / five assertions / zero errors; `reviewer-source-green.log` is three clean tests at the repaired source. These are stdlib source controls, not native receipts. The proposed incomplete-receipt native control is preserved in `next/tests/module_consumer/test_review_incomplete_receipt.py`.

At the7d0c458 publication checkout, historical stdlib verification imports no product:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/verify-pr71-c07-repair.py .
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/run-mike-repair-controls.py .
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/test-native-memory-admission.py docs/next/evidence/c07-source/native-memory-admission.py
```

A fresh native replay requires the already installed pinned images and the eight hash-bound offline ARM wheels staged in `tmp/wheels`; no dependency or image install is authorized. Use a fresh basename phase whose output does not exist:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/run-review-pg.py mutation-c07-review-fresh-004
PYTHONDONTWRITEBYTECODE=1 python3.12 docs/next/evidence/o01a-source/run-native-forward.py native-pg-repair-fresh-002.json
```

Run these sequentially. Both controllers record actual Mac `memory_pressure -Q` and active Podman VM MemAvailable before creating any resource and refuse if either is below 35%. Admission script bytes are Git-bound in the final receipts. Synthetic admission controls prove low Mac and low VM refusal and admission at both floors, with zero resource operations. No historical ≥35%-free admission is claimed for the original forward runs: their controllers did not record or enforce it. The final fresh O01a receipt is `../o01a-source/native-pg-c07-repair-002.json` at `2063fbf4b65594335e164d4343e1599950e12eac`, a proof-controller-only successor with the exact same 480 NEXT input bytes; seven tests, 973 base files, four WAL segments, pg_verifybackup and synthetic age roundtrip, refusals and complete owned cleanup. Backup attempt 001 passed its native checks but omitted lifecycle commands from its raw results; independent verification rejected that omission. Attempt 002 records admission before the actual create command and is the qualified receipt. Backup product/test/mutator bytes stay identical to be52491; accepted seven-fault backup proof remains bound to these unchanged bytes.

## PR71 pagination continuation repair (first qualification at eb9bd04)

Kai's18:36 DO NOW retains C07-PR69-002 until long observed histories recover. Product change is only `consumer.py:repair`: capture quarantine-root eligibility once under the lock; search and continue monotonic pages of at most1000 events through the original scan fence; stop on conflict; apply only the requested aggregate through the ordinary fence. No completed/unseen event becomes a repair root. Schema/protocol/backup product bytes stay unchanged.

`c07-review-red-pagination-001.json`:37 native tests / two expected BODY failures / zero errors at80d2f7c before the loop. Cases exercise1001 observed events and an independent poison A1, requested poison B1001, held B2001, conflict stop, three-page replay and newly published B2002 held for normal cycle. `mutation-c07-pagination-001.json` is the final381-clean-test/27-fault run at43f9318, with79 expected BODY assertions and481 copied inputs restored. Three frozen table guards remain expected RED lineage. Raw cleanup and measured35%-free admission are required.

The unchanged reviewer `pagination-controls/test_held_continuation.py` fails one assertion before code and passes afterward. Its companion helper is archived byte-exact as `page-boundary-red-original.py`; the adjacent adapter drops only four top-level RED diagnostics that assert the old bug. Every model method/event/dataflow and the unchanged test's target/cursor assertions are preserved. This is AST source evidence, separate from native PostgreSQL controls. Logs, hashes and source commits are bound in `pagination-source-control-binding.json`.

The prior exact-source verifier `verify-pr71-c07-repair.py` remains historical at7d0c458, including closed001/003/004 and the first short-page fix. At this successor use:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/verify-pr71-pagination.py .
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/pagination-controls/test_held_continuation.py .
```

Fresh sequential replay uses non-existing output basenames with the same already available hash-bound offline wheels/images (no install):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/run-review-pg.py mutation-c07-pagination-fresh-002
PYTHONDONTWRITEBYTECODE=1 python3.12 docs/next/evidence/o01a-source/run-native-forward.py native-pg-pagination-fresh-002.json
```

The native backup receipt is `../o01a-source/native-pg-pagination-001.json`; its source hashes bind unchanged backup product/test/controller inputs to this publication. All receipts retain distinct tested-source commits and publication head. No main merge or live adoption.

## Official reviewer control (Kai19:03 amendment, current)

Mike's corrected reusable wrapper is preserved byte-exact in `reviewer-reusable-control/test_held_continuation.py`. It pins the original probe SHA `9465118df9004ac0c8ee5f4bc5f931df34daf29d0071b8a9abd9f2a9706116d2` and loads that unchanged model while omitting only old-observation diagnostics. Both recovery assertions remain Mike's. The official wrapper is RED1/1 failure at7d0c458 and GREEN1/1 at repaired source; current binding is `pagination-official-control-binding.json`. The earlier private adapter and its logs/binding remain historical, not the current002 control.

The wrapper's pinned probe path is the canonical sealed reviewer artifact `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/pr71-c07-repair-mike-review-20261011/page-boundary.py`; its byte-exact Git copy is `reviewer-reusable-control/page-boundary.py`. It must exist at that path for the unchanged wrapper to run; missing or changed probe bytes refuse. This artifact dependency is explicit, not silently redirected by an adapter.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/reviewer-reusable-control/test_held_continuation.py .
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/verify-pr71-pagination.py .
```

The native controller now binds the official wrapper/probe in its input inventory. `mutation-c07-pagination-002.json` repeats the full381/27 matrix at committed7edf883 with unchanged481 NEXT bytes; the private wrapper is not the current control. All original native1001/multi-page assertions and product/test/mutator bytes remain unchanged from the preceding qualification. The fresh backup receipt for this controller-bound source is `../o01a-source/native-pg-pagination-002.json`. Publication changes only proof documents; every receipt retains its source commit and is re-bound to current Git inputs.

## Interrupted continuation (Kai20:12, current)

`cycle` now derives recovery from the durable applied/scan gap and sparse outcomes. It reads monotonic bounded pages through the original observed scan fence before fetching new history, skips unresolved poisoned aggregates without overwriting poison/held outcomes, and replays eligible observed work through ordinary fencing and idempotent target admission. New-work capacity limits never block existing observed recovery. No manual cursor reset, held-as-poison root admission, new table or SQL/protocol change. A new client/new PG request connection needs no repair-local continuation variables.

`c07-review-red-durable-001.json`:40 tests /3 expected BODY failures /zero errors atd582fe9 before code. Original native37 controls are unchanged. New cases cover root-checkpoint hook interruption plus temporary retry failure,held target commits then fails before Core ACK with exactly-once retry,and independent poisonA1/requestedB1001/heldB2001/full pending capacity after interruption. `mutation-c07-durable-001.json` at3f4d562:27 suites /384 clean tests /40 module tests /30 body-killed faults /82 expected assertions /zero errors,survivors or inconclusive results /482 copied inputs restored /owned cleanup.

Mike's desired `durable-reviewer-control/test_resumable_repair.py` remains byte-exact. Its review originally read a fixed JSON snapshot. The declared `run-probe.py` regenerates equivalent case fields from the exact current consumer AST and unchanged9465118d scaffold,with constructor-owned persistent state copied into fresh Harness objects;original diagnostic probe is archived. Fresh RED3 tests /2 BODY failures /0 errors;GREEN3/3. This source-flow model is not native proof. The official reusable uninterrupted wrapper remains byte-exact and GREEN1/1;original three controls stay GREEN.

Current stdlib verification/replay:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/verify-pr71-durable.py .
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/durable-reviewer-control/run-probe.py .
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/durable-reviewer-control/test_resumable_repair.py
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/reviewer-reusable-control/test_held_continuation.py .
```

The preceding pagination verifier/receipt bindings remain historical at86bc36c;current artifact is `pr71-durable-binding.json`. Fresh sequential native output names (same already installed images/hash-bound offline wheels,no install):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c07-source/run-review-pg.py mutation-c07-durable-fresh-002
PYTHONDONTWRITEBYTECODE=1 python3.12 docs/next/evidence/o01a-source/run-native-forward.py native-pg-durable-fresh-002.json
```

Renewed backup is `../o01a-source/native-pg-durable-001.json` at the same3f4d562 tested source;seven tests,973 base files,four16MiB WAL segments,pg_verifybackup,synthetic age roundtrip/refusals and exact owned cleanup. Admission requires actual Mac/active VM both≥35 before create. Publication successors modify only proof docs outside NEXT;typed source/publication commits stay distinct. No live migration,adoption or main merge.
