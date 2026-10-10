# B02 both-arm benchmark tooling amendment — Nemo

PROPOSED for Kai acceptance before implementation. H-D472 / 10:33 rebalance owns this unit; Mike reviews Nemo-authored delta, Vera retains Mike-authored harness review. Parent: merged PR43, main 013c4aa5789105d7f578219e23232f6de524b288. Product C09 security hold remains; this unit is benchmark tooling.

```text
custodian geometry JSONL + byte-bound admission -> validated float32
 -> deterministic coverage split -> candidate corpus (NO tuning/heldout IDs)
 -> exhaustive independent oracle -> same offered-load scheduler
 -> PG or Qdrant diagnostic -> per-cell receipt -> Mike review
```

## Scope and input contract

Synthetic data ONLY for every author proof. Never open, decrypt, copy or query the actual Marlow export. Future real importer accepts a custodian-created geometry JSONL boundary: exactly id (64 lower-case hex pseudonym), project (pseudonym or null), type (bounded coarse string or null), month (YYYY-MM or null), vector (one 768-element numeric array). No text, auth, tenant, tombstone, provider, model or sparse features. This boundary is NOT a claim about the uninspected original dump format: Ren-TK retains original decoding/custody. Real mode refuses BEFORE opening input unless an explicit reviewed admission binds input SHA256, 141511 rows, dimension768, CTO decision and Ren-TK custody receipt. The source tool validates bindings; the native operator verifies the authority of those records.

Stored-value float32 geometry is preserved; reject duplicate/missing IDs, extra fields, matrices, invalid dimensions, nonfinite/overflow, zero or subnormal cosine norms. Unknown generating model/provider/deletion/tenant provenance stays explicitly unknown. Null project/type/month are distinct documented categories, never silently dropped. Record joint and marginal input/corpus/tuning/heldout coverage. Deterministic SHA256(seed, ID) order within each joint category: reserve one tuning and one heldout record for groups with at least three records; smaller groups remain candidates and are explicit missing query coverage, NOT_RUN. Remove ALL tuning and heldout IDs physically from candidate arrays. Stable ID order, query bytes and split manifest hashes bind input, rule, counts, identity and source. Freeze split before engine evaluation; no query selection after results.

Reuse six existing dense strata; queries are heldout document vectors, never synthetic semantic queries. Emit exact eligible counts using independent oracle. Fractions that cannot populate a stratum stay NOT_RUN; no pooling or fictitious PASS. Candidate project/type/month encoding is deterministic and documented as geometry metadata, not product tenant/auth identity. No semantic/hybrid/model/provider/cache/product-path acceptance from this data.

## Qdrant delivery and measurement

Qdrant OSS v1.19.2 Apache-2.0 pins from accepted C09 plan: linux/amd64 sha256:0e8273b9130ca3b0dd9dcfaa8f55342066711f8aaeb2aa072e53344c42ecf57f; linux/arm64 sha256:9eb80ba088703193c124db5e50a59f2028439c8bbc62fa1918625995116b50d0. Nonroot10001:10001, ALL caps dropped, no-new-privileges, read-only root, writable bounded tmpfs, exec Qdrant binary. Isolated internal Podman network, NO published port, gRPC/CORS/telemetry/cluster/snapshot-URL recovery disabled. API key generated per disposable lifecycle, Podman secret mount, config secret rather than env/argv/logs; discarded after cleanup. TLS is absent per Kai GTM simplification.

A separately owned nonroot pinned Python3.12 slim client container supplies stdlib HTTP on the internal network. Eight persistent HTTP clients accessed through bounded tracked Podman-exec proxy processes; no per-query container creation, no new library. Scheduled-arrival-to-complete proxy/HTTP response includes IPC overhead, explicitly diagnostic; never labelled native/product timing. Client image manifest and platform pins verified before first start and retained. Aggregate containers <=1GiB/2CPU (Qdrant768MiB/1.5CPU, client256MiB/.5CPU). A fresh >=35% memory-free and empty team test-stack inventory preflight plus existing B01 advisory lock precedes EVERY start. Labels worker=nemo, cortex.test and unique lifecycle. Pending external effects are tracked before acknowledgement; only positively owned resources removed. On incomplete cleanup retain lock and credential custody; retry cleanup, never release into overlap. Mike's separate B01-LOCK-001 patch is not modified.

REST collection: metric/dimension match frozen corpus, explicit HNSW m16/ef_construct64 and query ef200, payload indexes for all filters, batched wait=true upload, exact count/ID mapping checks. Numeric point IDs map back to unchanged comparison IDs. Preserve tenant/project/deletion/generation/ordinal/kind/time filter parity on synthetic parent inputs. No exact-search substitution. Natural Qdrant telemetry/config/count recorded; no HNSW-use claim without observable evidence. Stable tie handling affects strict recall; existing tie-aware independent truth remains decisive.

## Files and implementation order

1. next/benchmarks/vector_baseline/B02-BOTH-ARM-PLAN.md (accepted amendment); README.md commands/qualification.
2. next/tests/benchmarks/test_b02_geometry.py and test_b02_qdrant.py: frozen RED body controls before source exists, asserting missing surfaces explicitly. Existing controls byte unchanged.
3. next/src/vector_baseline/geometry.py: importer, real admission, deterministic split/coverage/hash bindings and dense queries.
4. next/src/vector_baseline/qdrant.py: isolated owned lifecycle, pinned images, configuration, import/query/HTTP adapter and bounded cleanup.
5. next/src/vector_baseline/qdrant_proxy.py: stdlib-only persistent HTTP worker, sanitized protocol, secret from mount, no env dump.
6. next/src/vector_baseline/benchmark.py: engine selection, same scheduler/oracle/report; preserve default PG and existing admission tests. next/src/vector_baseline/postgres.py only if minimal filter/geometry compatibility is needed; do not touch Mike lifecycle fix. next/src/vector_baseline/corpus.py / oracle.py only if geometry loader boundary requires it; retain synthetic default and old assertions.
7. next/tests/benchmarks/mutate_b02_both_arm.py: literal behavioral mutations for every changed production concern, canonical test-body-only admission, exact source tree/raw bindings, clean and restored full affected suites.

## Fail-capable receipts

RED commits before each corresponding source implementation; fixed tests must remain byte identical. Hand-computed geometry: same candidate IDs/vectors independent of input order; deterministic disjoint tuning/heldout; null/category coverage and missing strata; truth from full eligible candidate scan; hashes reject drift; malformed row and real admission fail BEFORE source read. Qdrant: all pins/limits/nonroot/network/no-port controls; secret never in args/env/receipt; real/remote refusal before effects; matching full filters, point mapping and all responses; eight persistent clients; tracked partial starts/client processes; teardown error cannot be PASS/release lock; timeout/cancellation reap actual owned child. Fake deterministic probes plus one bounded synthetic actual arm smoke when memory>=35%. Preserve any failed attempt as FAIL/INCONCLUSIVE.

Run full benchmark and canonical shared receipt suites under Python3.12 with locked numpy2.3.4/psycopg3.2.9. Run actual named semantic mutants, admitting ONLY expected test-body AssertionErrors, zero setup/import/cleanup errors; retain raw streams, literal recipe, source snapshots, HEAD/tree, frozen test hashes, baseline/restored custody, independent receipt audit. Ruff + exact scope gate. Merge current main into owned branch and rerun affected checks before return unless Kai pins base. Push source PR for Mike, no merge/deploy/native/spend authority; remove own parked worktree after all bytes retained and pushed. Re-read inbox immediately.

## Open execution gates and risks

No real-data/native execution in this unit. H-D472 both engines, both editions, Mac warm-only / Linux cold+warm, $3 cap; Mike owns final sizing, Ren-KOS host admission, Ren-TK data custody, reviewed import and CTO named run authorization. No OS cache reset, live service, publication or production C09. Both native platforms, confidence windows, full product boundary and cross-arch snapshot restore remain NOT_RUN. Proxy IPC makes source smoke geometry/transport diagnostics only. Tiny categories cannot supply heldout coverage; report rather than fabricate. Original export decoding remains custody-owned until its exact schema is admitted.

## Acceptance and bounded compatibility detail

Kai ACCEPTED input SHA18820f1155cdb15ab9790c9e9016cf9785f45d275be0804c5d0e02bdb8aed873 at10:42, r426 addendum, after X02 fixes. This is the accepted implementation plan. Python3.12.15 slim verified immutable platform pins: amd64 sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe; arm64 sha256:16bf2b5c59a08523519d3c8589deca849285dee1d62a3653e124c3dba340f15e. Digest/manifest/config research is retained in the canonical pack. The existing next/tests/benchmarks/mutate_b02.py source collector must enumerate the three new modules so old source-custody checks remain complete, without changing its23 recipes or27 controls. No other proof-driver change.
