# C07 source proof and versioned recovery

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
