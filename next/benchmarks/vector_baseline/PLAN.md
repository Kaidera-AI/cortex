# B01 implementation plan — H-D455 CODE GO

Mike authors; Vera reviews. Target Kaidera-AI/cortex main, isolated SSD branch `mike/b01-postgres-baseline-20261009`; fresh `next/` only (helix `cortex/next/`).

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

H-D455 21:33 amendment: shared engine allows at most **1 GiB/2 CPUs**, label `worker=mike`. The original security assertion is tightened from 2 GiB to 1 GiB under this explicit steering; preserve original historical test hashes and bind the new test bytes. Verification-found startup cleanup/error classification controls are RED before fixes.

Kodus follow-up within the existing corpus-validation scope: add an unchanged RED control for missing/duplicate fixture IDs before any corpus files are created; require explicit unique IDs in write_corpus. Preserve the typed sanitized slot and loader validation. The accepted workload delta explicitly rejects zero cosine vectors; zero dot/Euclidean vectors remain valid stored-float32 inputs. Record the two Kodus findings and evidence dispositions separately.

Mutation phase follow-up: the shared search classifier accepted a fixture AssertionError as a named semantic kill. Add a B01 RED-first fixture/body admission check; replace raw-text admission with structured unittest phase and assertion ancestry. Reuse the exact reviewed C01 stdlib receipt helper at PR30 0f651b9 with a named-target loader seam; no product/runtime dependency. Existing nine recorded B01 failures remain actual test-body assertions.


## B01-CLEANUP-001 repair — Kai H-D455 2026-10-09 23:51

Accepted bounded repair direction: track pending creates under an exact fresh lifecycle identity, reconcile lost acknowledgement, preserve pre-existing/colliding resources. Vera's early finding is at helix `docs/handoffs/2026-10-09_vera_kai_b01-cleanup-001-finding.md`; full review continues. Mike authors, Vera re-reviews. Current main is incorporated before repair; the original published reviewed B01 head remains historical.

1. Add `next/tests/benchmarks/test_postgres_cleanup.py` before production edits. Model actual volume/secret/container creation then TimeoutExpired or acknowledgement error, empty owned inventories, credential discard, released POSIX lock, no body entry, collision preservation, safe no-side-effect failure, failed-removal retries and refused unverified identity. Commit expected RED. Keep these new controls unchanged through GREEN.
2. Amend only `next/src/vector_baseline/postgres.py` lifecycle code. One fresh 128-bit lifecycle label per stack instance is passed atomically to all three creates. Add pending ownership before invoking create, verify inspected identity before startup, and reconcile pending plus acknowledged resources on close. List exact candidate names, inspect their labels, remove only matching lifecycle identities (immutable container/secret IDs); named volumes use their verified exact unique name. Refuse reusing one lifecycle. Inspection or removal failure remains RED and retryable; always discard the in-memory password/release the lock. No ignore/replace, wildcard removal or live service access.
3. Existing two runner stubs must model new inventory/inspection calls; preserve every existing assertion. Adapt the existing volume mutation's textual target to the new pending-plus-owned branch, and add actual pending-omission and collision-identity semantic mutants. Retain original evidence as historical, add a separate repair receipt directory and exact byte bindings.
4. Verify unchanged RED controls, complete B01 tests, actual six sequential create-then-error lifecycles and real PostgreSQL fixture, current C01 checks after target incorporation, all old/new actual mutations and clean restored suites. At most one owned 1GiB/2CPU worker=mike stack; no live Cortex/harness resources touched.
5. Run Kodus on the bounded repair delta with the supported API-key alias, position/refute any findings using output, push the same PR35 feature branch for Vera. Incorporate fresh current main before handback and rerun if source changes; fold receipts, park/remove own worktree after push. No author acceptance, main merge or release.

Podman upstream supports labels on [secrets](https://docs.podman.io/en/latest/markdown/podman-secret-create.1.html) and [volumes](https://docs.podman.io/en/latest/markdown/podman-volume-inspect.1.html); local CLI help confirms those options. Secret list supports name/ID filters, so identity is checked through inspect rather than assuming a label filter. The existing immutable image pin, nonroot user, limits, loopback port and secret stdin remain. Volume name removal assumes no external actor deliberately replacing this fresh unpredictable identity between inspection and removal; no Podman atomic conditional-delete primitive is available. Container/secret removals use inspected immutable IDs.
