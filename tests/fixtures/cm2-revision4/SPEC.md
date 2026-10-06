# R203 — CM-2 revision 4: native Cortex v2 prerequisite for KOS

**Contract amendments accepted by R203/H-D264; synthetic fixtures, no implementation or installed admission.** Authority: `/Users/amadmalik/DevVault/helix/docs/handoffs/2026-10-06_kai_rulings-r203.md`. Normative fields and fixture vectors live under `/Users/amadmalik/DevVault/helix/Program/Cortex/local-deployment/R197_CM2_NATIVE_PREREQUISITE_2026-10-06/revision4`. This replaces CM-1 for explicitly selected native v2 installations on Linux and Mac; legacy selection remains explicit.

```text
CTO-signed Cortex manifest + actual local payload/runtime
    + exact Console connection + private MemberKeyReader
    -> fresh local proof + DB-ready + principal + roster
    -> CM-2 READY -> KOS may write its runtime
owner provisioning -> private keys -> complete proof -> atomic descriptor
```

## Descriptor and trust

Same runtime user's `~/.cortex/prerequisite.json`: schema `cortex.prerequisite.v2`, at most 64 KiB, owned non-root uid, directory0700/file0600, regular single-link file, no linked/unsafe ancestors; atomic same-directory publication. Strict UTF-8 JSON rejects duplicates, extra fields and trailing data. Every path is absolute/physical; no traversal. Every failure precedes KOS product writes.

| Fields | Meaning |
|---|---|
| `owner_uid`, `host_os`, `target` | Same invoking/runtime uid; Linux x86_64 or Mac arm64. |
| `api_origin`, `installation_id` | Explicit same-user loopback origin; canonical database UUID. No remote selection in this contract. |
| `runtime_root`, `package_root` | Exact Cortex-owned private instance/package; bind its owned API container and published port. |
| `release_manifest`, `release_signature`, `release_manifest_sha256` | Retained external `cortex.release.v2`, detached Minisign signature and exact bytes. KOS verifies with its independently pinned CTO key; a descriptor cannot supply trust. |
| `connection_file`, `member_reader_archive_sha256` | Private `cortex.console-connection.v1` already understood by Console; independently pinned reader digest, never a token. |
| `podman` | Minimum6.0.2, signed policy digest, native lifecycle provider; Linux null machine/connection, Mac explicit running machine/connection. |

The signed external manifest binds opaque label, native lineage `cortex-v2-native`, positive assigned sequence, contract `cortex-kos-v02009.v2`, exact source/target, archive size/checksum, inner package-manifest checksum, binary/image archives/config/payload identities, migrations, RLS inventory, reader digest and Podman policy. Compatible signed sequence>=1 passes; never compare labels or restrict admission to one release. It is separate from the existing inner TEST manifest, avoiding an archive-signature checksum cycle. Exact allowed fields are in `/Users/amadmalik/DevVault/helix/Program/Cortex/local-deployment/R197_CM2_NATIVE_PREREQUISITE_2026-10-06/revision4/contract.json` and `/Users/amadmalik/DevVault/helix/Program/Cortex/local-deployment/R197_CM2_NATIVE_PREREQUISITE_2026-10-06/revision4/descriptor.schema.json`; examples use synthetic identities, never release claims.

Connection fields stay exactly `schema, origin, installation_id, project, member_name, project_root, principal_id, actor_id, scope_id`. The selected native Console is `console`, active service/member, with exactly one project grant read/write=true, publish=false. The existing project API grants those rights; prerequisite reads do not qualify every Console write route or writer policy. Project root must match the selected primary native root. Linux keys remain private KeyStore files; Mac keys remain the dedicated installation keychain. Backup/export excludes `.kaidera/secrets/`; restore requires reissue.

## Finite provisioning, explicit owner action

Proposed packaged `cortex provision-console`: explicit installed root/request file, owner credential on an attached fd, stable non-secret idempotency key; Mac passphrase only on a separate attached fd. No bearer in argv, environment, logs, response receipts or fixtures. Validate release/local engine and custody first; prove owner on existing `GET /v1/auth/privileged-actions?limit=1`, discard its body.

- **create-project:** explicit complete `CreateProjectRequest`, `source_project=null`, `with_console=true`, explicit lead/manager/physical roots. Existing `POST /v1/projects` atomically enrolls lead and Console. Persist both once-issued tokens directly into their selected KeyStores; retain only operation/recipient metadata in a private resumable journal. The lead has the existing API's read/write/publish rights and its first-project create right; this requires explicit creation, never implicit KOS installation.
- **adopt-enrolled:** validate the exact already-issued Console record/profile/roster; issue nothing. Missing Console setup refuses. Existing-project enrollment uses the existing owner enrollment/roster APIs as explicit manager setup; this helper never replaces a concurrent full roster automatically. Capture the selected roster revision/entry and revalidate before publication; a concurrent change refuses with `cortex_provisioning_conflict`, preserving existing keys and roster.
- Same-key replay returns no token. Continue only if both committed recipient records already exist and verify. Missing/partial/uncertain delivery yields `cortex_provisioning_reissue_required`; never report READY or invent plaintext. Explicit **reissue-project-keys** uses the original operation ID and existing `POST /v1/projects/{operation_id}:reissue-keys` with reason `recipient_store_failed`; warn that both lead and Console rotate. Serialize local operations; no automatic reissue or deletion of prior keys.

Publish connection then descriptor only after complete native proof. A crash between them leaves a typed unavailable result, never a usable partial descriptor. No new public HTTP route.

## Readiness and Podman

KOS verifies signed files, inner payload, reader pin and private metadata first. Its exact signed Cortex helper supplies a fresh nonce-bound **read-only** local proof: owned containers/network/loopback, actual five image IDs and payloads, database instance, complete migration checksums, required RLS enabled+forced, non-superuser/non-bypass app role, no app table ownership/migrator inheritance. The helper keeps database credentials private and makes no migration/data writes. A cached migration/install receipt or OCI source label alone cannot pass. Verify the helper bytes against signed `files[bin/cortex]` before execution and bind its actual digest in the proof. The read-only native `project_binding` gives the selected scope ID, primary alias and physical primary root; these must match the private connection and live member grant.

Then the actual KOS reader caller sends bounded member requests to existing `/health/ready`, `/v1/auth/principal`, and `/v1/scopes/{project}/roster`: 5 seconds/64 KiB each, 60-second overall deadline, no redirect/proxy inheritance. Headers are read once immediately before each request; changed credentials across admission refuse explicitly. Check exact installation/principal/actor/scope, one exact grant, active member roster. Health alone establishes only the DB connection. Native GREEN also proves the known ungranted fixture scope returns404 `scope_not_found` and zero writes.

Mac's creator-only keychain smoke is insufficient. Explicit manager-authorized preparation may stage verified release executables in a private owned area and configure their dedicated installation keychain ACL. It cannot publish a KOS runtime, write KOS state/carriers, register/start services or switch an installation. The actual verifier and actual packaged Console are separate headless readers, with exact executable bytes and execution identity; the KOS reader never unlocks or mutates the keychain. KOS admission remains read-only until every prerequisite passes. Identity-changing promotion requires fresh explicit manager qualification and a headless final read before service publication. Failure preserves the old runtime and unit; owned scratch cleanup and preparation effects are counted separately from product writes. Missing/locked/denied/unqualified access refuses without fallback. This is Mac-only and does not block Linux. Synthetic refusal vectors are not native ACL qualification.

Read the strict UTF-8 duplicate-free `podman version --format json` report, at most 64 KiB with a 5-second command deadline within the 60-second admission deadline. Linux executes `podman --remote=false version --format json`: observe `Client.Version`, represent `mode: local`, `server_version: null` and `connection_name: null`; an absent/null Server is normal, never invent it or start a service. Mac executes on the exact descriptor named connection, representing `mode: remote`, its exact `connection_name` and both observed numeric `Client.Version`/`Server.Version`. Each required stable numeric version must be >=6.0.2, with no ceiling; apply the union of signed Cortex/KOS dated denylists even for an unchanged valid descriptor. Record actual versions and Linuxbrew's stock bottle-checksum verification. A named known-bad version refuses visibly. Linux uses the local rootless engine and current Linuxbrew formula; no formula-commit pin. Rocky rehearsal requires SELinux Enforcing. Trouble yields a consult with the Rocky distro alternative, never permissive mode or relabeling. Mac retains its explicit connection and rootless Linux arm64 engine.

KOS exports only native mode/origin/connection/default-project values listed in `contract.json`, and drops inherited legacy/admin auth settings. It never provisions, unlocks, rotates or automatically retries a refused write.

## Refusals, fixtures and execution

Refusals retain the typed code, safe message, guide pointer, UTC and observed HTTP status; no auth dumps, token fingerprints/hashes or secrets. All 94 revision2 IDs, outcomes and phases are retained. Revision4 has **147** declarative vectors, with **53** additions covering the nine consumer consult gap groups, including successful adoption and concurrent-roster refusal. The coverage map and literal raw-report/connection/HTTP fixtures are shared producer/consumer inputs. All changed byte bindings are recomputed; revision2 and its freeze remain unchanged.

Every consumer refusal must be tested through the actual installer boundary, with zero runtime-tree/state/carrier/service/switch writes before admission, no fallback/provisioning calls and the prior runtime/unit intact. Use actual owned private file and parent replacement fixtures, separate caller identities, literal raw response bytes, bounded deadlines and per-request credential rotation seams; one stat/access Boolean is insufficient. Engine mutations remain zero. Producer vectors belong to Cortex; KOS proves install never invokes those operations. Explicit CM1 success is distinct from CM2 refusal with valid legacy inputs present.

This publication checks only contract byte/identity/coverage consistency. Product RED/GREEN, fresh author, Vera, actual signed native bytes, RLS/ungranted-scope404 and independent Mac headless ACL qualification are later gates. R203 authorizes producer create-project, adopt-enrolled, explicit reissue and Mac preparation RED-first against these exact revision3 bytes. No new public HTTP route, owner fallback, hidden roster replacement, real credentials, runtime or engine action is performed here.

## R216 protocol amendment

Exact release helper/retained paths/exit codes/bounds: /Users/amadmalik/DevVault/helix/Program/Cortex/local-deployment/R197_CM2_NATIVE_PREREQUISITE_2026-10-06/revision4/PROTOCOL_ADDENDUM.md. The helper is bin/cortex; the former TEST-only filename is not an alias for native admission. Revision3 and its freeze remain unchanged. All147 revision3 case records are identical in revision4; only helper naming/protocol metadata and transitive release/descriptor/proof bindings change. Linux fresh creation/readiness is first under R210; adopt/reissue/Mac implementation remains the next named slice.
