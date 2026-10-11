# schema_3-001 — shared message sequence migration repair

Authority: Kai H-D455 (2026-10-10 14:33); one small Cortex PR for the next v1 cut, never v0.1.003. Vera independently reviews. Base: `origin/main` at branch creation. Do not merge.

## Intent and acceptance

The archive-message migration currently reads the sequence's `last_value` and the archive maximum, then calls `setval`. A concurrent `nextval` between its read and reset can receive an ID which the migration reissues. The migration must preserve `max(public.messages.id, public.archive_messages.id, prior sequence last_value)` and serialize concurrent `nextval` through the high-water reset. It must remain idempotent and keep using the one shared sequence.

## Build order

1. Run the frozen PR #54 `test_schema_3_001_migration_does_not_rewind_concurrent_message_ids` on a Mike-owned disposable PostgreSQL with at least 35% free memory. Retain its expected test-body RED before editing migration source.
2. Add a focused native test: run the migration inside an explicit transaction against hot/archive/prior IDs, attempt `nextval` on a second connection before commit, require it to remain blocked until commit and then return above all three maxima. Run this test RED against old source. No live database or protected data.
3. Repair the migration inside its transaction: lock both present message tables against writes; acquire a transaction-held sequence DDL lock without changing its cache setting; read the prior sequence value and both table maxima under these locks; transactionally `RESTART WITH high_water + 1`. Preserve the archive default and source's idempotent rerun behavior. PostgreSQL does not permit `LOCK TABLE` on a sequence, so use same-cache `ALTER SEQUENCE` for its lock.
4. Run frozen and new native tests GREEN. Restore the old migration in a controlled mutant and require the frozen/new tests to fail in their named assertions; restore and rerun GREEN. Check syntax/static quality and the source bytes.
5. Merge current `origin/main` into the branch, rerun checks, push one PR, and return exact head/evidence. Vera reviews; Mike does not approve or merge. Remove owned resources/worktree after receipt capture.
