# API4-LOCK-001 — PR #59 lock-order rework

Authority: Kai H-D455 2026-10-10 15:02; Vera's exact-head REWORK at `6f8848c6459e678bf86fb0ea05cb4ae343c3dd89`. Target next v1 cut, never v0.1.003. Preserve the nine frozen PR #54 test bodies and all seven prior API4-004 controls; Vera reviews the new head.

The current import inserts requested rows before acquiring paired message-table locks in `reset_project_transfer_sequences`. Opposite partial imports can each hold one table's `ROW EXCLUSIVE` lock and then request `SHARE ROW EXCLUSIVE` on both, creating a cycle.

## Acceptance and order

1. On owned disposable PostgreSQL admitted only at >=35% free memory, add an exact-call-path two-import test. Patch only the reset boundary to pause after each import's INSERT and before the reset. Old source must permit both imports to reach that boundary, then fail one transaction with PostgreSQL `40P01`; capture raw named RED. The test itself expects both imports to complete and the next shared ID to exceed both table maxima and the prior value.
2. Commit that RED test before changing production code. Keep all prior frozen assertions byte-identical.
3. Extract one shared lock helper: fixed-order `LOCK TABLE public.messages, public.archive_messages IN SHARE ROW EXCLUSIVE MODE` for present tables, then same-cache `ALTER SEQUENCE public.messages_id_seq` to hold the sequence DDL lock through the transaction. Call it at the start of `import_project`'s outer transaction whenever either message table is requested, before its first INSERT. Reuse the helper in the direct reset path, so direct callers retain the earlier high-water guarantee. Match PR #61 migration's table-then-sequence order.
4. Run the new test GREEN and the seven prior API4-004 cases GREEN on the same bounded scratch server. Revert only the new early-lock call in a mutant and require the new test's body to fail while prior cases remain green; restore bytes and rerun. Merge current main tip, repeat focused checks, push new PR #59 head. Remove owned stack/worktree after recording receipts; return to Kai for Vera's independent review.
