# X3 slice 1 accepted build plan

Parent c4fd7358-62c3-45da-9839-98c989e84534, nemo@helix. Kai BUILD GO 2026-10-16; accepted design SHA256 8daa24af5494d9d533fff3f4ac8cfbfb785ebf1b024a10881292ae59c6f91cae. Source base origin/main 69e4dd762c729df9ce3fe9def576bf0ebbfc4837. Fixture-only; <=6 h. No DB, host, credentials, native IO, real drivers, network provider, rotation/recovery implementation, PR, protected-branch merge or deployment.

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


## Protected supplier contract (still UNBOUND)

Only `_installed_inputs()` selects the installed owner transport/custodian. The
fixture patches that private seam, never grants OS, SQL or recovery authority.
The current process tuple is `(host, boot, uid, namespace, code, lifetime)`;
actual peer/process/clock/custody observations and their authenticated originals
must come from X1/X2/X7, not from this data tuple. Grant expiry is the current
custodian expiry; the handle separately retains the smaller original owner E.
A shorter wait budget does not renew or extend either expiry.

- `observe(ref,purpose,installation,mode,E)` returns `_Grant`; `read(grant,E)`
  privately supplies transient material only under current owner authority.
- `reserve_carrier(grant,E)` returns an absent `_CarrierState` for the exact
  already-recorded P1 resource. It must not create/write material. `create_carrier`,
  `observe_carrier`, `close_carrier` receive that original state and E. Identity is
  `(mount,parent,inode)` of held resources, with independent same-UID reopen,
  tmpfs/private, mode, ownership, regular/link and consumer observations. Source
  policy cannot measure these. The descriptor is original reservation metadata
  (including S02 and reserved absence); it is not a live cleanup/restore receipt.
- `sql_policy(grant)` returns admitted `_SQLPolicy`; no caller DSN/kwargs. The
  private SSL context must already contain the exact admitted CA inventory.
- `reserve_connect(grant,policy,E)` returns `_ConnectAttempt(operation,attempt,
  resource,process)` from the original P1 attempt **before** driver effects.
  `connect(policy,kwargs,E,ticket)` must keep all child effects within that exact
  reservation, including lost returns. `abort_connect(ticket,original_E)` must
  independently establish no owned pending driver/session survives, or HOLD.
- `observe_session(connection,grant,policy,E)` supplies independent actual
  `_SessionState` (exact installation, DB, endpoint IP/SAN, OID/revision, TLS,
  HBA and IDLE). Echoed fixture booleans are not such a witness. Driver-specific
  session state normalization belongs to the protected implementation.
- `retire(connection,original_E)` returns exactly True only after independent
  finite absence. Unknown, timeout, cancellation or any survivor means HOLD.
  Local handle fencing and original cleanup do not resolve new credentials.

All protected async waits and synchronous SQL policy lookup reuse the existing
`private_driver_logs` scope. Fixed refusals contain no provider/driver error body;
no kwargs, value, verifier or secret-derived digest enters these descriptors.
Protected Python/driver/libpq buffers exist; guaranteed erasure is not claimed.

`ResolverConnection` is a physical-connect/current-check/retire boundary only.
It returns the protected supplier's connection; the later X5 pool/consumer owner
must call `check` before checkout/use and after C04 exit before publication, and
`retire` on close/failure. It is not a pool or a generic driver facade. RC1's
async connect signature is reused; live RC1 composition and driver-specific
execute/fetch/close adaptation remain UNBOUND. The existing DSN probe factory
is untouched and not selected by this source slice.

Cox's later DB rotation/recovery retains the existing five P1 fields; canonical
nonsecret descriptor bytes replace only the carrier field under a separately
admitted material adapter. This slice does not change role_grants.snapshot,
HBA, SQL, C04, hosted selection, recovery or installed factory wiring. Recovery
cannot infer password equality from pg_roles or descriptor equality.


Reservation race review added a current-handle and current-SQL-policy check
between reserved ticket and driver dispatch, plus current policy at connect
publication and both ends of session checks. This does not add an authority
channel; it reuses the same fixed supplier. The privacy oracle now verifies all
three log-producing activities by logger identity, rather than exactly three
messages, because mandatory current-policy rechecks legitimately add policy
observations. Leak and exception assertions remain unchanged; the original 20
case bodies remain identical to the first RED commit.


Current-main integration: incorporated origin/main 12b2c01a25047719f5842ffd4732976dcc047793
into this task branch only (d13825c4), preserving the accepted R4 changes and their
CI inventory pins. No protected branch changed. The X3 delta against that main
is exactly six declared files; CI change is one additional RUN test admission.
The privacy failure assertion now reports a boolean leak result and its marker,
so a newly detected public synthetic fixture value is not copied into diagnostics.


Carrier reservation review: `_CarrierState.phase` is an observed supplier field,
not a phase inferred from a path or invented by the descriptor. The initial
original must be S02, absent and have zero surviving consumers before any
material read/create. Invalid original reservations refuse before create or
cleanup; a bad descriptor is not permission to touch a target. After a valid
original begins creation, cancellation/uncertainty still reconciles only that
original and yields HOLD unless measured absence is established.
