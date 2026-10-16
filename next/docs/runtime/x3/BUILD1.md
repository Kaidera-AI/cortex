# X3 slice 1 accepted build plan

Parent c4fd7358-62c3-45da-9839-98c989e84534, nemo@helix. Kai BUILD GO 2026-10-16; accepted design SHA256 8daa24af5494d9d533fff3f4ac8cfbfb785ebf1b024a10881292ae59c6f91cae. Source base origin/main 69e4dd762c729df9ce3fe9def576bf0ebbfc4837. Fixture-only; <=6 h. No DB, host, credentials, native IO, real drivers, network provider, rotation/recovery implementation, PR or merge.

Depth tree (solo):
1. Resolver policy/opaque handle: new runtime/secret_resolver.py, reuse signed_member opaque registry/refusal pattern and original six-view inventory. Fixed installed input supplier refuses by default; tests replace only this private seam. Current owner grant checked before/after waits, per-use and publication; reference/secret revision/process lifetime/purpose/install/mode/epoch/deadline; no bearer/serialization/export.
2. Private carrier lifecycle: same module, protected material IO protocol only; 0600/0700/held identity/private tmpfs/source process checks delegated to independently observed supplier; original nonsecret descriptor retained, no values/history; cleanup observed even on refusal/cancel, HOLD on uncertain absence. All actual IO remains UNBOUND. This slice implements policy over fixture ports, not OS custody.
3. Shared SQL boundary: connections.py fixed resolver-backed adapter preserving RC1 connect protocol; per-physical-connect resolution, explicit Psycopg or asyncpg kwargs and current before/after connect checks; exact session/TLS/HBA witness port and destroy/absence on failure. No driver imported/selected by caller. Reuse postgres_core_probe unchanged; legacy DSN factory remains legacy and is not used. Retain config, role_grants, auth and hosted provider behavior unchanged. Cox consult owns DB/recovery interface concurrence; no DB rotation implemented here.
4. Evidence/review: tests next/tests/test_x3_secret_resolver.py; assertions fail in body on missing feature, never count import/fixture failure. Commit RED tests before implementation, keep them unchanged. CI-pinned Pythons 3.13.7/3.14.0 (3.11/3.12 runs are supplemental), scoped regression, meaningful one-fault mutant/restore controls, exact own branch push/readback. Report per-X3 partial-source cases vs full native NOT_RUN.

Risks: fixture DATA cannot authorize installed custody, unknown post-connect/carrier closure must HOLD, private driver auth buffers allowed by Kai but never logged, no guessing clock/process/platform/custodian originals. No authority may be promoted by callback/BOUND receipt. No current production path becomes usable.

Pre-edit: fresh worktree clean; worktree/ref inventories in this packet (119 trees, 505 recent refs). No existing changes in declared new paths. Disconnected legacy roots checked directly and absent. Existing adapter path is isolated here; no edits to shared checkout. New task branch nemo/x3-build1-20261016.

# Acceptance gates — X3 source slice only

- [ ] G1: each source-level X3 case first fails at its expected test-body assertion on the baseline; test file committed before implementation.
  CHECK: run authored unittest fixture suite on RED commit; classify raw failures by expected test/body; reject import/setup/runner failures.
  EXPECT: expected body assertions only, no ERROR
- [ ] G2: resolver identity/current-state/no-bearer/refusal/expiry/close cases succeed on both supported Pythons.
  CHECK: authored fixture suite on final source.
  EXPECT: exit 0 and exact independently counted cases; no skip
- [ ] G3: carrier policy proves private finite lifecycle and explicit HOLD on uncertainty using fixture observations, never OS qualification.
  CHECK: authored fixture carrier cases in same suite.
  EXPECT: correct order/closure and zero value in retained descriptor/result
- [ ] G4: shared connect protocol refuses stale/current/TLS/session faults, closes unknown partial connections, keeps exact timeout and private driver kwargs.
  CHECK: authored fixture SQL cases and existing effect-free RC1/RC2/RC3a regression.
  EXPECT: exit 0; driver/network calls zero
- [ ] G5: selected meaningful guard removals are killed by expected test-body assertions, clean source restored.
  CHECK: authored one-fault source-control runner + restoration suite.
  EXPECT: expected AssertionError per declared control; no setup/import counts
- [ ] G6: DB interface consultation returned and amendments addressed; later DB rotation/recovery/native remain UNBOUND.
  MANUAL: Cox receipt + exact interface/code comparison, report limitation.
- [ ] G7: own branch pushed and origin exact head/source/test bytes read back; report top3/30 and same-row return receipts.
  CHECK: git ls-remote and origin blob/hash checks + report audit + cortex-return readback.
  EXPECT: exact equal head/digests; returned true, no PR

Verification correction: CI matrix is 3.13.7/3.14.0. Admit only the new fixture test in the existing offline RUN inventory, without changing exclusions or runner policy. Original 20 test bodies remain frozen; extended fixtures use original connect reservation tickets.
