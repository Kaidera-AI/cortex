# API4-004: preserve the shared messages sequence on project import

Authority: Kai H-D455 2026-10-10 14:33, NEXT v1 cut only. Mike authors; Vera independently reviews. The frozen, merged PR #54 native regression is the accepted RED oracle. This PR changes only `packages/api/main.py` and this plan; any added test must be a separate unchanged-oracle control in the same slice. No v0.1.003 change or merge is authorized.

## Intent and invariant

After importing either `public.messages` or `public.archive_messages`, the shared `public.messages_id_seq` must not be set below the maximum hot ID, maximum archive ID, or its value before the reset. A later `nextval` must exceed that high-water mark. All other transfer sequences retain their current reset behavior.

## Build order

1. Pin main and run `test_api4_004_transfer_reset_keeps_shared_sequence_above_archive` against an owned disposable PostgreSQL; retain its expected RED before editing production source. The existing test file stays frozen.
2. Change `reset_project_transfer_sequences` so the shared sequence is handled once for an import containing either table, rather than once per owned column. Lock the sequence for the high-water read/reset, compare both table maxima with its prior value, and set it only to that maximum. Do not let the generic per-column path first rewind the shared sequence.
3. Run the frozen native test GREEN and a small control matrix for hot-leading, archive-leading and prior-sequence-leading cases. Reintroduce the old reset behavior in a temporary source copy and require the frozen test to fail at its named assertion. Check source restoration, syntax/lint and focused surrounding tests.
4. Commit and push one small PR. Vera reviews its exact head; author does not approve or merge. Remove the owned worktree and database stack after receipt capture.

## Boundaries and danger

Door: one-way at runtime because a duplicate message ID can corrupt data, but this unmerged source PR is reversible. Blast radius: import. The proof stack is one labelled `mike` PostgreSQL 18.4 container, at most 1 GiB/2 CPU, ephemeral loopback port, admitted only at >=35% memory free; the test fixture creates/drops its own database. Never target live `:5500` or `:8501`, protected data, a remote host, or v0.1.003.

Dependency: PR #50 or C06 re-reviews pre-empt this work if Kai assigns them before return. Any change to this plan after evidence must be recorded before the corresponding code change.
