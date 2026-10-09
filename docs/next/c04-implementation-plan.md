# C04 auth and project isolation — implementation plan

Authority: H-D455 small-GTM code go and cox DO NOW22:07, after PR30 correction and PR32 proof refresh. Existing accepted plan rowC04: auth/tenant roles,RLS,owner/private helper boundary; realPG read/write denial,cross-tenant tests,role cannot bypassRLS. Owner cox; Vera reviews; Kai adjudicates. Source-only branch stacked on PR32, no self-merge/deploy/live migration/v0.1.003 edits.

## Intent and full security grill

The API must obtain tenant only from verified principal credentials and known installation/project registry state; forged headers/context must not expand authority. Implement the narrow Core auth/RLS port now so C05 and engine owners can bind it. Source: design62§7.1/7.2,CXV2-006,PLAN C04/CX006,r426 OpenKai controls require owner/admin. The proposed composite v2 wire-key model requires Kai's ruling: this slice does not implement it. It uses C03's existing scoped principal UUID and credential verification records, with opaque credential bytes hashed in memory. No new key issuer or HTTP format/route mapping.

The riskiest assumption is that authoritative credentials/grants can be rechecked through RLS without trusting tenant GUCs or a stale cache. Falsify it with forged context, cross-tenant read/write, missing/revoked/expired/disabled key, wrong project/install, unauthorized control, RLS bypass and revocation races on realPG. Trusted boundary: API/server owns private DB connections; clients never receive DB credentials. Every auth port rejects privileged/owning runtime roles. No raw key/digest in public scope objects or errors.

Decisions already fixed by H-D455/CXV2-006: Postgres sole authority,forcedRLS,no cached auth authority,private least privilege,owner/admin control,no project wildcard or fallback. Alternatives: tenant-only GUC policy cannot verify identity; privileged API role bypasses RLS; cached grants admit stale revocation. Use two NOLOGIN/NOBYPASSRLS roles with no membership: request role has only scoped DML; verifier owns narrowly exposed SECURITY DEFINER auth functions and lookup policies, no superuser/bypass. Fixed secure search_path and revoked PUBLIC function/schema privileges. Row-share auth locks linearize accepted requests with revocation; expiry rechecked at return. Permissions-generation triggers partition caches on grant/credential/principal lifecycle changes. PostgreSQL official references: https://www.postgresql.org/docs/18/ddl-rowsecurity.html , https://www.postgresql.org/docs/18/sql-createfunction.html , https://www.postgresql.org/docs/18/explicit-locking.html .

Security grill score:38/50,capped39 until falsification/rollback receipts exist; author may build the authorized wedge,not claim production acceptance. Sources for all dimensions are the above scoped go,accepted C03 schema and proof design below; counter-evidence: API routes/engines are not yet integrated,role provisioning requires installer privileges,concurrent revocation semantics need execution proof,native Linuxx86_64 remains unqualified. Kai owns unresolved wire-key model/releasedC02 compatibility; each engine owner runs shared conformance,C10/C11 carry full API/engine acceptance. No cross-lane wait is created.

## Files and order

1. RED: `next/tests/auth/test_authorization.py`,using copied synthetic data on real disposable PG; full contract/source tests remain intact. Common negative cases in `next/contracts/auth-conformance.json` for downstream owners. Commit missing-port RED before implementation.
2. Add `next/schema/auth/002-isolation.sql`,append `auth-0002` to manifest with digest; never edit existing001 migrations. Create/validate two fixed logical runtime roles,forceRLS on canonical business/registry tables; schema ledger remains installer-only with no request privileges. Project tables use verified credential/install/project/action via auth functions,not tenant headers. Installation-wide feed/checkpoint/snapshot tables are denied to request role pending C06/C07 private service ports. Auth registry is not directly readable/writable by request role. No TRUNCATE/REFERENCES/DDL privileges.
3. Add `next/src/cortex_core/auth.py`:typed safe scope/errors,credential hashing,mandatory active transaction + safe-role check,verify/bind transaction-local context,current authoritative recheck before return,cache partition(installation,tenant,project,principal,generation,action). Data connection failure is typed core_unavailable; no bypass/fallback. API HTTP mapping/new wire-key model remain C02/C05 boundary.
4. Add `next/scripts/mutate_auth.py`:each implementation/schema/manifest source has semantic mutation with explicit expected-test-body assertion and full raw output using shared test_receipts. Error/setup/import/signal/malformed/fixture/unrelated failures remain inconclusive and fail proof.
5. Disposable nativearm64 PG18.4/Python3.12 pod,networknone/noports/noSSDmounts,2CPU/1GiB total,PG512MiBtmpfs,driver256MiB; offline pinned wheels. Run full auth/schema suites and mutation proof,sequential full contract suite. At most one own stack,remove aftereachrun,retain errors. Fresh verifier,integrate current target,hash/test-byte receipts,diff check,PR per slice,return and remove ownworktree.

## Naming gate

Result: PASS
Name: kaidera-runtime-core-request; kaidera-runtime-core-verifier
Reason: platform-owned logical PostgreSQL roles,shared runtime environment,Core request/credential-verification functions,singletons; no cloud or location dependency.
Canonical form: singleton
Required fix: none
Evidence needed before creation: H-D455/C04 source scope; roles instantiated only in disposable tests now,owner installer before real deployment. Infra skill's referenced SOP path is absent here; its explicit grammar governs these new names. Disposable stack names retain accepted H-D458 test naming.

## Proof and risks

Test a genuine nonowner/NOBYPASS request role; SQL reads with missing/forged tenant must see no other tenant/project,wrong INSERT/UPDATE refused. Positive authorized reads/writes/control must still work. Unknown/revoked/expired credentials and disabled principal refuse,read context never writes/control,control-only grant never elevates owner. Context vanishes on commit/rollback/pool reuse; function search_path/PUBLIC privileges and role ownership checked. Generation changes on grant/revoke/disable; cache partition has tenant/principal/generation/action. Concurrent revoke blocks behind accepted request,then subsequent request refuses; deadline expiry before response rolls back. Existing C03 tests verify fresh ledger/checksum/atomicity on new additive migration.

Rollback for current source: return to parked PR32 branch; disposable storage/roles are removed with the owned pod. Real forward migration is one-way and remains a separately authorized installer gate. No runtime/API/engine complete or release claim from source checks.

## Pre-implementation fixture refinement and latest queue

Kai23:20 orders C04 after available C03 rebase work. PR30 is merged; PR34 pure MOVE is published for Mike; PR32 is rebased without content changes and its merge waits Vera. Continue the already authorized C04 source build on that accepted C03 tree while retaining all independent merge gates. Current C04 target refresh produced only the known old-versus-corrected legacy-map add/add conflict; resolved to PR32's verified915-field map, no auth/source test bytes lost.

Disposable PG pre-implementation diagnostic at ef4f518 proves exactly28 canonical business tables (ledger excluded), so the original C04 test's minimum30 was a mistaken fixture assumption, not a product requirement. Replace it with the stricter exact28 for this frozen C03 boundary; every table must still assert ENABLE+FORCE RLS. This correction occurs before auth implementation, followed by a fresh missing-port RED with the final fixture bytes. The initial diagnostic also hypothesized that the PUBLIC privilege query was invalid; PostgreSQL18 accepted it, falsifying that hypothesis. Its runner assertion failure is retained; PUBLIC query/assertions stay unchanged. Follow-up confirmed diagnostic retains both valid PUBLIC query forms. No auth denial, ownership, scope or generation assertion is weakened. Original C03 tests remain unchanged.
