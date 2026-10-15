# RC3b inputs — assumptions for Cox ratification, never binding authority

RC3a is data/preflight/refusal source only. Its matching caller snapshots remain
UNBOUND and cannot grant a connection, credential or service. The RC0 lock binds
the operations member and owner-port identity; the nine role policies come from
that pinned operations member and SQL-name/installation bindings from the RC2
admitted config. No duplicate role rows or new config/parser are introduced.

Required inputs from the returned private-grant proposal, not yet ratification:

- **PG-A runtime-grants**: the exact admitted nine roles bootstrap, migrator,
  request_group, verifier_owner, api_request, probe, supervisor, observer,
  installer_enrollment; actual session_user/OID/installation bindings, role graph,
  phase/owner/ACL/default/RLS catalogue and real-PG grant/denial receipts.
- Existing SQL identities **kaidera-runtime-core-request** (NOLOGIN),
  **kaidera-runtime-core-verifier** (NOLOGIN, no members), and
  **kaidera-runtime-core-installer** (private enrollment only). All new physical
  login names are original installer-owned inputs, not choices made by RC3a.
- Sole API->request membership SET TRUE/INHERIT FALSE/ADMIN FALSE. No API owner,
  admin pool, direct projection-writer grants or bypass role. Finite bootstrap
  and migrator privileges exist only under their separate admitted owner phase,
  never API/supervisor/inspect secret views; all finite sessions/carriers close.
- Existing private C04 functions **auth.resolve_scope(text,uuid,uuid,text)**,
  **auth.bind_scope(text,uuid,uuid,text)** and existing credential/context checks;
  **auth.enrollment_readiness(uuid)** and
  **auth.first_identity(uuid,uuid,uuid,uuid,uuid,text,text)** keep exact one-use,
  replay/audit and installation checks. API has no direct first_identity grant.
- Proposed M20 private auth guards **auth.runtime_request_installation(uuid)**,
  **auth.runtime_tenant_installation(uuid)** and hidden
  **auth.runtime_bound_installation(text)**. Hidden helper has no service EXECUTE.
- **PG-B runtime-ports**: supervisor-only
  **coordination.runtime_lease_acquire(uuid,uuid,integer)**,
  **runtime_lease_renew(uuid,uuid,bigint,integer)**,
  **runtime_lease_release(uuid,uuid,bigint)**,
  **runtime_lease_assert_live(uuid,uuid,bigint)**;
  supervisor/observer **runtime_lease_health(uuid)** and observer-only
  **runtime_observe(uuid)** in schema coordination. No raw lease/map/tenant grants.
  Retain monotone fence, post-lock clock check, bounded aggregate/no fake zero.
- **PG-C reviewed runtime contract revision**: current RC0 is literally19/42.
  Proposed M20/M21 become21/44 only through the reviewed new manifest/SQL hashes,
  current-schema/contract/module/source/compatibility tuple and RC2 pin revision;
  none of the19 SQL files or their receipts is silently rewritten.
- **PG-D C04 connection adapters**: fixed SET LOCAL ROLE inside authorized()'s
  owned transaction before its role query, current session/credential/context
  recheck, reset/discard after success/cancel/rollback and pool reuse. asyncpg
  search/cache must bind the same current C04/session boundary; no GUC-only auth.

The six process secret views remain exactly RC0's. API/inspect role data may
describe all9, but connection ports offer only their admitted view; neither
request_group nor verifier is a login. Provider is a purpose/reference, not a
tenth database role. Original R01 unit3/owner resolver/TLS/HBA/namespace/operation
inputs and current real role/ACL evidence remain missing. A BOUND declaration,
matching snapshot, receipt string or construction of these dataclasses does not
replace any of them. No public bind toggle exists in RC3a.

## Existing code reused and why legacy startup is not imported

- Reuse RC2 admit_config/read_member/decode and fixed RC0 pins; no second verifier,
  config file, parser, signature/root store, DSN builder or credential reader.
- Reuse current C11 load_routes() for the full frozen route inventory and stable
  C01-R102 write, C11-D1 read, C01-R022 search and C01-R030 status identities.
  Enrollment is the private health input, not an invented HTTP route. Callbacks
  are checked but never invoked/retained; RC3a does not construct a live gateway.
- Reuse RC1 postgres_core_probe and EnrollmentHealth. The unbound probe port has
  its exact async connect deadline keywords; no alternate probe implementation.
  Static health composes the current Core-unavailable path and cannot report
  actual enrollment_required/ready without the real private readiness port.
- Legacy packages/api/main.py lifespan creates app/admin pools from environment
  DSNs, calls schema/recovery startup, and keeps an admin alias. acquire_scoped
  uses session cortex.project GUCs and a compatibility superuser fallback.
  Importing or copying that startup into v2 would violate this unit's no-effects,
  finite-owner and private C04/session requirements. Keep its reset-or-destroy
  lesson, not its authority/DSN/pool behavior; RC3b supplies the accepted adapter.

Real authorized writes/reads/search/status/enrollment, actual catalog grants,
current resolver/role login/TLS, native services and installed health are all
NOT_RUN. Callback mocks, declaration comparison and these stubs earn no such
credit. The exact ratified grant matrix supersedes these proposal assumptions;
incompatibility returns a typed gap and bounded rework, never a broad GRANT.
