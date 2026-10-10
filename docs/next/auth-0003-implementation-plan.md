# auth-0003 — one-day Core identity plan (NO CODE)

H-D470, Kai DO NOW02:35 on2026-10-10. **Owner Cox; Kai accepts; Vera reviews.** Proposed C04a, **Day5**, after the present C05 records/jobs proof. Kai ACCEPTED at02:42, allocating one of the two reserved integration days; the critical-path forecast does not grow. Separate identity PR; no implementation before plan acceptance, and no live issuer/adoption GO from that acceptance.

## Representation for acceptance

Append `next/schema/auth/003-identity.sql` / manifest entry `auth-0003`; keep001/002 and all40 C04 fixtures byte-identical. Add trailing `kind` (agent/human, defaultagent) and `identity_adopted` (defaultfalse) columns to `auth.principals`; add bounded, unique, normalized `roles text[]` (defaultempty) to `auth.project_grants`. A private view expands roles into **(tenant,project,principal,role)** memberships. This keeps the frozen28-business-table boundary; a new membership table would contradict that existing fixture. **Kai must accept this existing-grant representation.** Roles remain separate from permissions and grant no read/write/control authority themselves.

Existing principals remain unadopted and gain no role/human routing authority automatically. Ordinary C04 auth stays unchanged. Role/human consumers require current credential/grant, enabled adopted principal and fresh Core data. Missing/unadopted/disabled kind fails human checks; owner permission never implies human kind. Existing principal/grant generation triggers must invalidate kind/role changes.

Only fixed-search-path, PUBLIC-revoked verifier-owned functions perform private lookup/registration/adoption. Request role gains execution, never direct auth writes. Mutations require intact private binding plus fresh project owner/admin control. Separate `register_agent` and owner-attested `register_human` fix kind; caller headers and self-declared kind/role never establish identity. Core supplies C05's recipient role and human-return checks; no edge authority store.

## Issuer and adoption contract

`next/src/cortex_core/identity.py` owns the transaction and final authority check. Registration creates stable principal UUID, project roles and separately approved permissions. Role strings never grant owner/admin. The legacy `cortex-add-agent <name> <role>` maps to kindagent plus normalized project role under **owner/admin authority**, tightening its current project-writer registration. Model/display-name/writer-scope are descriptive; unsupported mappings stay explicit gaps. [Pinned planning inputs](evidence/c05-source/auth-0003-plan-inputs.json); exact released donor/client pins remain C02's gate.

Proposed issuer: opaque printable ASCII bearer from32 random bytes (`secrets.token_urlsafe(32)`), SHA256 of exact transmitted ASCII bytes, matching C04 without secondary decoding. Store digest/lifetime/revocation only. Deliver plaintext once to the private owner channel **after commit**, never to logs/shared artifacts/idempotency replay. Lost secret delivery requires explicit revoke/reissue; rotation explicitly revokes the old credential. Transport/bootstrap console enrollment stays C11/release-owned; no new live root credential is inferred.

Adoption requires an explicit owner-reviewed manifest: released donor SHA/hash, source project/identity, stable target UUID/name, kindagent, role tuple and separate permissions. Dry run returns exact mapping/counts without writes; apply binds that same manifest digest. Refuse duplicate/ambiguous IDs, conflicting scope, changed hashes and human conversion. Preserve immutable audit/mapping payloads without secret bytes. Migration/boot never adopts existing agents. Human registration is distinct and owner-attested. Missing released pin means adoption NOT_RUN.

## Files, order and six-hour budget

1. **1h:** freeze `next/tests/auth_identity/` and `next/contracts/identity-conformance.json`; actual real-PG missing-port/representation RED; unchanged C04 hashes.
2. **1.5h:**003 SQL/manifest, private functions/view, kind/roles constraints and generation behavior.
3. **1h:** identity port, fixed registration paths, once-only issuance and hash-bound dry-run/apply adoption.
4. **1h:** C05 integration with fresh Core role/kind checks; pre-fix spoof/self-review RED. Preserve its14 record/23 job tests; exact released HTTP mapping stays C02/C11.
5. **1h:** semantic faults per changed runtime/SQL/manifest/fixture/caller; unchanged30 schema/40 auth +14 records/23 jobs and new identity suites, restored bytes/cleanup. One copied nativearm64 disposable Podman stack, total2CPU/1GiB, no ports/binds, pinned offline wheels; no host product execution.
6. **0.5h:** independent verifier, current-main gates, one origin-bound PR/receipt for Kai/Vera, preserve ignored inputs/remove parked own worktree. Overrun returns remaining tasks for Kai to split.

## Fail-capable acceptance and gates

Prove real owner-only registration/adoption/issuance; cross-project denial; caller kind/role spoof refusal; no human inference from permissions; project role revocation and generation invalidation; PUBLIC/private-function/RLS denials and frozen28-table count; exact legacy argument mapping and explicit adoption; concurrent duplicate one outcome; rollback after actual principal/grant/credential/audit changes; expiry/revocation/rotation; lost-secret replay never returns plaintext. Mutants require expected BODY assertions, zero operational errors/survivors/inconclusive, and complete source restoration.

**Door:** one-way when migration/adoption is applied. **Blast radius:** identity. Source revert does not undo adopted data; repair applied data forward. Live003/issuer/adoption, install, nativex86/signing/release and customer publication remain held. Kai accepts representation/issuer/day before code; Vera reviews; release authority owns admission. No finding is closed by this plan.
