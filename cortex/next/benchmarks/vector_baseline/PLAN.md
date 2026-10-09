# B01 implementation plan — H-D455 CODE GO

Mike authors; Vera reviews. Target Kaidera-AI/cortex main, isolated SSD branch `mike/b01-postgres-baseline-20261009`; fresh `cortex/next/` only.

```text
corpus.py: deterministic 5M-default streaming generator + typed sanitized slot
oracle.py: exhaustive independent float64 truth + ties/full fusion + cell verdicts
postgres.py: one disposable digest-pinned pgvector HNSW stack + diagnostics
tests: immutable RED → GREEN → killed per-source behavioral mutations
```

Accepted draft: `79725eb52879e4c9bc637908e2536c0599a05730a4422a0eedd4d44e567ac2d6`. Additive workload delta: `b486136f4488d33431f4f64436c1225fe0da1ba907ea254512e3e29e52dfbf67`, at helix `docs/plans/cortex-v2-v0.1.020/quality/2026-10-09_mike_h-d455-b01-postgres-delta.md`. H-D455 explicitly supersedes H-D452 code hold.

1. Commit executable behavior tests before implementation; retain initial missing-surface RED and exact test hashes.
2. Pin NumPy/psycopg environment, write bounded-memory vector/metadata arrays and manifest, separate tuning/held-out seeds; default corpus count exactly 5,000,000, explicit dimension/model/metric.
3. Independently scan full eligible corpus, not engine candidates; preserve sparse positive-overlap semantics, canonical branch ties, finite/full fusion truth, empty/short/forbidden failures and per-filter verdicts.
4. Stream fixtures into owned fresh Podman storage. Nonroot UID/GID 999, no capabilities, no-new-privileges, CPU/memory bounds, random loopback-only port, ephemeral password secret; remove container, volume and secret on success/failure. No live service, SSD bind, cloud, paid model or real dataset access.
5. Run unchanged tests, actual pgvector fixture and killed mutation per production source file. Preserve exact commands/results/byte bindings. Merge current target main and rerun; publish one PR for Vera, never self-approve or merge.

Naming gate: PASS; `kaidera-dev-vector-baseline-<numeric-run-id>`; project helix/Cortex, dev diagnostic, fleet member, no region; role is vector baseline. Canonical form `kaidera-{env}-{role}-{n}` from helix `.agents/skills/infra-naming-gate/SKILL.md`; legacy SOP path currently absent. The suffix identifies one owned lifecycle, not a live platform resource.

Local SQL/precomputed-vector timings are diagnostic. Product auth/provider/cache path, 5M/native full benchmark, real snapshot/query/sparse custody, cold host proof, crash/ACK, 16-worker concurrency, PITR, portability and 24h SELinux stay separate gates. The harness cannot decide Qdrant from synthetic or partial data; it emits UNDECIDED for those conditions. Complete per-cell real-data product evidence is required to retain Postgres or reopen Qdrant.
