# C06 forward qualification: FM-001 / FM-002

The tested source commit is `e7cbf75461fb81e3ba113e61b7cc24df39dd5cf4`. The publication tip is a documentation-only successor: `forward-source-binding.json` names the tested commit and raw receipt, while `verify-forward-native.py` proves the product and controller bytes at the publication tip are identical to that tested source. Never relabel the receipt's tested SHA as the publication SHA.

Host Python performs stdlib source/AST/hash/JSON/orchestration only. Executable receipt discovery is the first product action inside the copied restricted network-none pod, after source copy and offline dependency preparation, before migrations/tests. Its reported count is compared with the declared 41. Actual `load_tests` executes only in the pod; a pure AST count is not used because the discovery hook can select or add tests dynamically.

`test_host_no_product_import.py` is a stdlib host boundary control. It temporarily places an uncommitted synthetic receipt module in the worktree. Module import and `load_tests` each write an owned marker. The preflight must refuse the uncommitted input without executing either callback. Both frozen named assertions fail at `0a2de16` with zero errors; the unchanged controls pass after isolation. `host-boundary-red-001.json`, the preserved earlier GREEN, and `host-boundary-green-001.json` retain exact test/controller hashes and marker observations. Each temporary fixture/marker is removed after the case.

The native recipe is `run-core-pg.py mutation-forward-fm-001-002`: 26 clean suites / 344 tests and eight primary mutation matrices with counts 43/32/30/11/3/1/26/8. Every primary fault must fail the declared test BODY with zero errors, survivors or inconclusive results. It retains raw per-fault results, copied inventory and owned cleanup, password discard and lock release. The measured result and source count live in the raw receipt and verifier output, not in a relabeled historical index.

To replay, create an isolated worktree at the tested source commit. Supply the eight hash-pinned offline wheels under `tmp/wheels` exactly as `offline-wheel-inputs.json` declares; do not install host dependencies. Run:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c06-source/run-core-pg.py mutation-forward-fm-001-002
```

This requires the already pinned native-arm64 Podman PG/Python images and the existing memory/lifecycle gate. It creates one pod at a time, total 2 CPU / 1 GiB, no network, published ports or binds. A publication checkout already contains the canonical raw output and intentionally refuses overwriting it; use the tested commit or a fresh phase name for a new replay, and keep its new receipt separate.

Verify the published packet at the publication checkout without executing product code on the host:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/c06-source/verify-forward-native.py .
```

The verifier checks every product/controller input against both tested and current Git bytes, all native clean/mutation/discovery/cleanup records, unchanged RED/GREEN controls, and every member of the new publication index. The index covers `next/` and `docs/next/evidence/c06-source/`, excluding itself. Prior r4 and older indices/receipts remain immutable historical evidence; they do not certify the forward tip. GitHub API PG CI is a separate gate and is not counted as the C06 copied NEXT matrix.
