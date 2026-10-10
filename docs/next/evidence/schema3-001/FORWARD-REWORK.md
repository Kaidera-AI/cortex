# SCHEMA3-DELIVERY-001 — forward migration rework

Authority: Kai H-D455 2026-10-10 15:05; Vera's exact-head PR #61 REWORK at `2a03ba59e769d5a48430c81fe5a3cf3825c3d85d`. Next v1 cut only, never v0.1.003. Vera independently reviews the new head.

The July migration is already shipped and may be present in `cortex_schema_migrations` under its old checksum. Rewriting that ID makes the API and standalone runners refuse an upgrade before the repair can run. Keep its bytes identical to main and add a new dated forward ID.

## Acceptance and build order

1. Freeze a native **prior-ledger upgrade RED** against this PR head: create an owned scratch database with the old July checksum in its ledger, then call the real API migration planner/apply engine using a directory with the July source and planned successor. The current same-ID rewrite must produce `checksum_mismatch` / refusal. Also freeze a **fresh-install** control that must apply July then successor, once each, and rerun with zero new applications. Commit tests before changing migration source.
2. Restore `2026-07-29-01-archive-messages-shared-id-sequence.sql` exactly from `origin/main`. Move the already native-proven high-water/lock SQL into `2026-10-10-01-archive-messages-sequence-high-water.sql`. Preserve fixed order: hot/archive tables, then sequence. Point only the test file's top-level `MIGRATION` path at the new file; all nine PR #54 frozen test bodies and the added fence test body remain byte-unchanged.
3. Run prior-ledger upgrade and fresh-install controls GREEN on a disposable PostgreSQL admitted at >=35% free memory, using the real `schema_migration_plan` and `apply_schema_migrations` with a two-file test directory. Run the frozen and fence native cases GREEN. Mutate only the new forward file back to old SQL and require those cases RED; restore and rerun GREEN. Recheck cache-10/idempotence, old July hash, exact resource teardown, and source/test custody.
4. Merge current main tip and repeat focused checks. Push PR #61's new head, then return for Vera. Keep PR #59's early table-then-sequence lock fix as a **merge-order dependency**: PR #59 must be accepted/merged before PR #61 is applied on a running installation; otherwise a pre-lock API import can overlap this migration. Kai owns that release ordering. Neither PR is merged by Mike.
