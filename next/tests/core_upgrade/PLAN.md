# Core upgrade source unit: accepted scope and gates

Authority: Kai's 2026-10-10 21:07 DO NOW and the accepted C11 upgrade plan.
Stack on PR #80 exact `f8293572`; RED first, one branch/PR, no host or live
installation. This is Cox's Core source/PG unit; Ren-TK owns signed package
execution and the production trust root.

Release evidence checked before code: the released `v0.1.002` GitHub release
has one SHA-256-pinned launcher tarball (`1829fb6d...`), no install payload
or aggregate signed deployment manifest; `git tag -v v0.1.002` reports no
signature. Its published `cortex install` refuses. Therefore the actual
old→new deployment pair is **unadmitted** until Ren-TK supplies an approved
signed old/new pair. The source checker will fail closed for it. Ephemeral
minisign-signed synthetic manifests exercise the source boundary without
claiming they are released artifacts.

1. Add RED tests for wrong signature, pinned digest drift, unknown schema or
   event, downgrade, mixed platform, incompatible module set and new writer
   on an old schema. Every rejection must precede DB change.
2. Add a pure signed-manifest verifier and exact compatibility table in
   `src/cortex_core/upgrade.py`; require caller-pinned trust key and two
   manifest digests. No implicit production table or synthetic promotion.
3. Extend `migrations.apply_migrations` with a validated `through` target to
   call the explicit port one forward-only step at a time. Add a Core upgrade
   coordinator using injected durable backup/fence/journal/restore ports. An
   interrupted step is re-enterable from the migration ledger; acceptance is
   durable and prevents rollback.
4. In disposable PostgreSQL 18+pgvector, seed records, blobs, permission
   generations, outbox/consumer cursors and model identity. Apply two
   synthetic expand-only migrations one step at a time, interrupt after each,
   re-enter without duplicate rows/events, and compare bytes/digests/scope/
   cursors/model identity. Check pre-acceptance restore/fence and post-
   acceptance fix-forward refusal. Retain raw tests, mutations and cleanup.

No SQL contract/drop, actual signed artifact execution, native Mac/Linux
update or v0.1.002 stack install is claimed. A production pair remains a
named external gate; never synthesize a signature for the released launcher.
