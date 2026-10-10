# API2-001 BM25 invalidation class repair

Accepted by Kai's 2026-10-10 23:38 DO NOW, extending the 23:29 plan to decisions and lessons. Initial source base: `db0989d25930cfb70d662f9cffade79673ce446e`. Cortex v0.1.004 only; independent reviewer Vera.

1. Preserve PR54's existing decisions regression unchanged. Add a companion native-PostgreSQL lessons regression with the same stale/current-row shape, reusing the frozen API and scratch-connection fixtures. Show both RED at their stale-row assertions, then commit the new regression before the product repair.
2. Change only the two BM25 tuple predicates in `packages/api/main.py` to `project = $2 AND invalidated_at IS NULL`. Show both unchanged tests GREEN.
3. Make two separate literal source mutants, reverting only decisions or only lessons. Run the corresponding unedited regression and require its deliberate stale-row assertion to kill each mutant. Retain each raw mutated main.py and transcript.
4. Inventory every other execute_search stage reading decisions, lessons, handoffs or work_products, including exact-ID, work-product lexical, trigram bridges and vector. Record the effective invalidation filters and exact source lines; name leaks as follow-ups, without repairing them in this PR.
5. All database proofs use one owned, uniquely labelled Podman PostgreSQL instance with effective memory <=1 GiB and CPU <=2. Host memory-pressure free percentage and VM MemAvailable/MemTotal must each be >=35% before launch. Use the inspected local immutable upstream PostgreSQL image, native arm64, a randomly reserved loopback-only non-live port, trust authentication only on disposable data, no live volumes or credentials. Capture effective cgroup settings and mapping before connection; remove the instance and its disposable volume in finally and prove their absence and port closure.
6. Fetch and merge current origin/main into the branch without rebase before return; rerun gates if its tip changed. Push only declared paths and open one PR for Vera. Do not merge, deploy or change release identities. Stop on a conflict or changed test oracle.

Declared paths: packages/api/main.py; packages/api/tests/test_bm25_lessons_invalidation_native.py; this plan; docs/maintenance/api2-bm25-class-20261010/SEARCH_STAGE_INVENTORY.md. Frozen test file must have no diff. Receipts and raw mutants live on the SSD under `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/v004/api2-bm25-class-20261010/`, cited by full existing path and SHA-256.

The prior uncapped native-PG diagnostic is withdrawn as acceptance evidence and will not be reused. Pre-edit ref/worktree overlap evidence and graph-helper failure are recorded in the SSD receipts. Active health/graph changes do not alter execute_search; prior frozen release branches are excluded from this main-line repair.

If memory, disposable ownership, cgroup limits, DSN guards, the deliberate RED or cleanup cannot be proved, return the failure to Kai rather than weakening the gate. Any new DO NOW is checked before each source mutation and after return.
