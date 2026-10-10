# API2-EXACT-ID-INVALIDATION

Authority: Kai's accepted 2026-10-10 23:52 DO NOW and policy ruling: execute_search never returns an invalidated row on any path. The 23:54 amendment put PR93's pinned merge/proof first; that is complete, and this untouched source branch was advanced to the new main `c5cc937fbe535b69030d7fa73e87a25497a25fbe` before RED. Separate from PR93; v0.1.004 only; reviewer Vera. Any Vera finding on PR93 pre-empts this work.

1. Add one native test parametrized over decisions, lessons, handoffs and work_products. For each table, prove current rows remain visible by exact ID and prefix, invalidated rows are absent by both forms, and non-current work_products are absent. Include invalidation between the first ID query and the content query, so the two required predicates actually prevent a late invalidated row from being returned. Preserve this test unchanged after its deliberate RED and commit it before the source fix.
2. Add fixed table-specific predicates to both shared-loop queries in packages/api/main.py: invalidated_at IS NULL for the four tables, and status current additionally for work_products. Leave knowledge/messages behavior and project/RLS policy unchanged. If the second query returns no row, skip the candidate rather than appending an empty-content ghost row; this is necessary for the second predicate's effect under concurrent invalidation.
3. GREEN on the same four unchanged cases. Four literal mutants independently remove each table's predicate and must fail in the corresponding test body. Also challenge omission of the second-query filter, which must fire the late-invalidation oracle.
4. Use the same owned, uniquely labelled, native arm64 Podman PostgreSQL proof contract: <=1 GiB, <=2 CPUs verified from effective cgroups; >=35% host memory-pressure free percentage and VM MemAvailable/total before launch; inspected immutable upstream image; loopback-only unused non-live port; sanitized DSN guards before connection; no live volumes or credentials; container, anonymous volume and port cleanup in finally and reverified.
5. Fetch/merge current origin/main into the branch without rebase and rerun the final-tree controls. Commit/push only declared paths and open one separate PR for Vera; no candidate merge or deployment. Retain full existing SSD receipt paths and SHA-256 values, raw mutants, deliberate failure attribution and the final source snapshot. Consolidate documents under the main document root. Re-read DO NOW before source mutations and after return.

Declared paths: packages/api/main.py; packages/api/tests/test_exact_id_invalidation_native.py; this plan. The frozen PR54 audit gate is fixture reuse only and must have no diff. Receipts: `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/v004/exact-id-invalidation-20261010/`.

PR93's two accepted BM25 edits are now inherited from main and outside this ID-loop hunk. Any resource/ownership/DSN/oracle/cleanup failure keeps the proof RED; preserve the failed receipt and return the gap to Kai rather than weakening the gate.

## Proof receipts

The frozen audit file is unchanged; the new parametrized test SHA-256 remains `94a296b08102399d7d8d9da801dcafddcbc11595471efb163437ff340edef2e2` from RED through GREEN and every mutant. Full existing SSD receipts and SHA-256:

- RED: `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/v004/exact-id-invalidation-20261010/RED/receipt.json` — `a4af60628146375f9c8ffcf117fb59abfd43c246e7b415af11b20c095db6eafa`.
- GREEN: `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/v004/exact-id-invalidation-20261010/GREEN/receipt.json` — `af999371b8afa20047447f6fbf8c1cab570696d43429af99c3f3b00749068063`.
- mutant-decisions: `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/v004/exact-id-invalidation-20261010/mutant-decisions/receipt.json` — `2f477b1334d63c6f327355e5e41c796b2648cc47255f4ab712ffa5e943d1209d`.
- mutant-lessons: `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/v004/exact-id-invalidation-20261010/mutant-lessons/receipt.json` — `398e5cf9a2978d2f770c3a50dd6efd966eee52f064f58066366bf7b91af1cee0`.
- mutant-handoffs: `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/v004/exact-id-invalidation-20261010/mutant-handoffs/receipt.json` — `56d1ca86a1c4d380952d6de8e1be8200290eba2bca86199f15fafdd81021c93b`.
- mutant-work_products: `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/v004/exact-id-invalidation-20261010/mutant-work_products/receipt.json` — `c4f7263be2c8ebc7d0d11b8f51ef94beb3d9a29f34caf553311367b42fcdd18f`.
- mutant-content-read: `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/v004/exact-id-invalidation-20261010/mutant-content-read/receipt.json` — `a3e48474154f7826da2074e0feeb2d39a89c331ad12009f1021e1bd43d97a5d4`.

RED recorded four deliberate body AssertionErrors. GREEN recorded four passes, covering both exact ID and prefix, current rows retained, non-current work_products excluded, and invalidation between the two reads. The four table-specific literal mutants each fired the named invalidated-row oracle; the second-read mutant fired the late-invalidation oracle for all four tables. Every proof records 1 GiB/2 CPU effective cgroup limits, >=35% prelaunch host/VM headroom, eight DSN guards before connection, and owned container/volume/port cleanup. Raw mutated sources and transcripts are retained.
