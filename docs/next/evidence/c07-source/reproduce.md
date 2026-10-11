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

## Integrated PR71 C07 finding repair (current)

The earlier C07 receipts above remain historical. The repaired integrated source is `4cf4c8080c66d4a3186733e83d39591074029c92`, with current main `903fd729c56cf719bbb15e9f986a70b9160497c1` incorporated. Current proof is `mutation-c07-review-003.json`: 27 suites / 379 native tests, 35 consumer tests, 24/24 body-killed faults with 76 expected assertions, no errors/survivors/inconclusive results, 480 copied inputs restored, three resources removed. Three frozen old-table assertions remain intentional RED lineage, separate from the clean suites.

`c07-review-red-002.json` records 34 tests / 55 body failures / zero errors before the four fixes; `c07-review-green-001.json` records their 34 clean tests. `c07-review-red-scan-001.json` adds the observed-range control: 35 tests, one body failure before the scan-boundary fix. `mutation-c07-review-001.json` remains FAILED (one INCONCLUSIVE legacy poison-fence abort); attempt 002 passes 23 faults but precedes the additional scan-boundary control. Neither is substituted for the final run. An unexpected ConsumerError from the specifically asserted normal cycle now fails that test body; setup/import failures remain INCONCLUSIVE and the shared classifier is unchanged.

The unchanged Mike AST controls are in `frozen/mike-repair-manifest.py`; `run-mike-repair-controls.py` supplies only constructor-owned module_id and observed scan cursor omitted by the original doubles. `reviewer-source-red.log` is three tests / five assertions / zero errors; `reviewer-source-green.log` is three clean tests at the repaired source. These are stdlib source controls, not native receipts. The proposed incomplete-receipt native control is preserved in `next/tests/module_consumer/test_review_incomplete_receipt.py`.

From a publication checkout, stdlib verification imports no product:

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
