# R01 unit 3 aggregate and original-member contract — U3-A freeze

Authority: Kai handoff e1092a4f-89ed-4c31-aa35-5d9df102c235, 2026-10-14; parent accepted plan is Helix `docs/handoffs/2026-10-14_ren-kos_kai_w4-r01u3-plan.md`, SHA-256 f07288c170ea97bfabc052193b4547443be65f42114334f78c406327c4aca68b. Status: frozen proposal for Cox's contract acceptance and Vera/Hale review; **no verifier, native, signing or deployment acceptance**.

Base: RC2 a7c54a7869e784bf726ef2ba56e41d82c7970ad8, containing RC0 b06ba24f6e22099ee55bfff2b72727801b5b9325. No RC0/RC2 byte changes. The separate R02b.2 main commit 157600bdf2ab9752479a0484e7ceb4aaf9f88b5d is not silently merged into this named base. R01 input/receipt source remains the existing build admission; none is a signed-deployment verifier.

## Rulings and proposed facts

Kai names R02's build lane (Ren-KOS executor) as aggregate producer through R01 `assemble_aggregate`. The frozen future surface is `release/contracts.py:assemble_aggregate`, delegating to one R01-owned assembly body if split into `assembly.py`; no second assembler. That future export is absent and is not implemented/edited by A. CTO is signer at the signing sitting, with a separate named signing GO. One release key for KOS/Cortex is the existing Minisign ID **7BBB4B7E0CE8852E**; full public material is pinned from independently admitted owner policy, never from artifact data. U3-A reads/generates no key, public or private, and makes no signature. H-D546 stays restricted to KOS build-A TEST; it is not extended to Cortex. A root decision is not a signing GO, key path delivery, native grant or permission to publish.

Kai approves **hashed ED only**, including legacy Ed negative; existing KOS code remains unchanged. The new aggregate schema `cortex.release-aggregate.v1`, format1, canonical UTF-8/integer/NFC JSON with one trailing LF, and external content ID `sha256:<64 lowercase hex>` remain **proposed for Cox acceptance**. No aliases/bare digest alternative is accepted by default.

The existing KOS producer writes `kaidera-os.release.v1` and explicitly excludes Cortex images. Existing KOS signing coordinator signs exact `message/release.json` with `message/release.json.minisig`, rather than defining this aggregate. No existing KOS subject is renamed/reencoded as a Cortex subject. The full parent plan binds those source facts to exact paths/hashes.

## Physical subjects and schema closure

The owner-recorded transport directory contains original `release.json`, `release.json.minisig`, external `runtime.lock.json`, and a finite signed `members/` inventory. Original read roots/device/ownership/custody are independently owner-supplied. Candidate config/argv/environment cannot choose them. No fetch, search, extraction, image load or executable launch occurs to find a member.

`next/contracts/release/aggregate.schema.json` is the sole new manifest grammar. Every object closes keys; no additional field or future version defaults. Top-level fields are exactly `schema`, `format_version`, `purpose`, `release`, `source`, `sequence`, `not_before`, `expires_at`, `compatibility`, `members`. The release contains release_id, API contract, schema/event versions and separate Core/API module versions. Source contains exact Git commit/tree. Compatibility names original descriptor/compatibility/schema/event/migration/operations members and tuple, not substitute digests invented from matching declarations.

Members have closed kind unions. Image: kind/role/path/media_type/bytes/sha256/module_id/module_version/target/oci_manifest_sha256/build_input_sha256/build_receipt_sha256. Runtime lock: kind/role/path/media_type/bytes/sha256. Record: kind/role/path/media_type/bytes/sha256. Image SHA covers original raw uncompressed OCI archive bytes; oci_manifest_sha256 covers original platform manifest bytes. RC0 image.value.sha256 is the latter. Both must be independently measured and cannot be interchanged with config/index/tag hashes.

Four image roles are mandatory, exactly once per role/target:

| Role / target | Aggregate-relative path |
|---|---|
| core-image / linux/arm64 | members/core-linux-arm64.oci.tar |
| api-image / linux/arm64 | members/api-linux-arm64.oci.tar |
| core-image / linux/amd64 | members/core-linux-amd64.oci.tar |
| api-image / linux/amd64 | members/api-linux-amd64.oci.tar |

One lock row is mandatory at `runtime.lock.json`. It covers both targets and is separate from both images. No final image embeds that lock or physically aliases it. The actual installed absolute path is RC0 `/run/cortex/runtime.lock.json`; transport names are not arbitrary installed read paths. Original install mapping comes from owner admission.

Required record classes: lock-schema, config-schema, operations, descriptor, compatibility, schema, events, migrations, build-input, build-receipt, evidence, sbom, provenance. These classes do not imply one generic record qualifies all four image builds. Each selected image requires its complete original input/receipt/output/evidence closure, module descriptors and current compatibility under independent accepted policy. Cox freezes the exact full member list before U3-C. Missing physical originals/owner dependencies remain required_binding_gap.

Paths are lowercase ASCII canonical relative components, start alphanumeric per component and contain only alphanumeric/underscore/dot/hyphen; only `runtime.lock.json` is permitted outside members/. No empty, dot/dotdot, slash alias, backslash, drive/UNC, percent encoding, case/Unicode alias, links or device escape. Paths must be sorted and unique **by path**, not merely unique whole JSON objects. Whole duplicates are also forbidden by schema uniqueItems. Distinct records repeating a path can validate structurally; this is an explicit semantic verifier refusal, never a schema PASS promoted to admission. Installed executable/contract paths retain RC0's own grammar; this transport rule does not rewrite them.

Manifest bounds are at most1MiB/depth32; independently admitted production-policy bytes≤8MiB/depth32; signature4096B; lock262144B; record8MiB; image4GiB per original archive; aggregate16GiB; members10000; path4096B; bounded reads120s. Owner policy may tighten any cap. Canonical parser independently measures raw/depth/count/total/time limits; declared lengths do not enforce physical caps. All wire integers reject booleans/floats/nonfinite values. JSON Schema alone can treat whole-valued1.0 as an integer; the original raw R01 decoder rejects every float spelling before schema validation. This is an explicit parser obligation, never a structural-schema proof. Sequence1..2^63-1; UTC seconds0..253402300799; source hashes40 lowercase hex and SHA25664 lowercase hex. Neither manifest nor lock includes the manifest's own digest, avoiding a cycle.

The manifest self digest is externally supplied and covers **original whole canonical bytes**. The verifier first authenticates those original bytes and then checks schema/canonicality; it never verifies repaired/reencoded bytes. SHA256SUMS, labels, BOUND/state flags, schema validation or parsed objects provide no root/original/native authority.

## Trusted-comment purpose grammar and key-use separation

The exact ASCII comment grammar is:

`cortex.release-aggregate.v1|purpose=<purpose>|release=<release_id>|sequence=<sequence>|sha256=<manifest_sha256>|source=<source_commit>`

Order, labels, case and separators are literal. No whitespace, percent encoding, missing/duplicate/additional field, Unicode alias, prefix match or reordering. `purpose` is exactly `cortex/core-api/test` or `cortex/core-api/production`, matched to independent owner purpose and root allowed_purposes/key_use_permission. Release ID follows manifest grammar, cannot contain pipe/equal; sequence has canonical decimal spelling with no sign/leading zeros; digest/source are exact lowercase full hashes. Comment≤512 UTF-8 bytes. This is a **new Cortex signer contract**, not a change to the KOS coordinator's256-byte limit or its arbitrary owner comment.

Minisign sidecar envelope is bounded four lines: untrusted comment, base64 signature packet, literal trusted comment line, base64 global signature. Require ED; Ed/unknown algorithms refuse. Envelope base64 is strict: signature packet74 decoded bytes (algorithm2 +key ID8 +signature64), global signature64 bytes. Public key decodes to42 bytes (public algorithm Ed2 +ID8 +key32); that Ed public encoding is distinct from the ED signed-message algorithm. Pin the whole original owner public file bytes/length before decoding, allow only its accepted single-key format, compare decoded ID with policy, and refuse malformed root as aggregate_root_mismatch. Sidecar uses literal `untrusted comment:` / `trusted comment:` labels, LF line endings and one final LF, with no additional/blank/trailing lines; unsupported sidecar structure refuses aggregate_signature_invalid. No alternate grammar or automatic repair. First Ed25519 signature authenticates BLAKE2b-512(original manifest bytes); second authenticates first signature bytes concatenated with exact UTF-8 trusted comment. Key ID and full independently pinned public-key bytes/hash both matter. Untrusted comment never authorizes scope. R01 must strictly parse the actual Minisign line syntax, not use the schematic placeholder as an alternate wire. [Upstream Minisign specification](https://jedisct1.github.io/minisign/).

A valid KOS manifest signature does not admit Cortex: exact comment purpose/key-use context plus Cortex manifest schema/domain reject it. A Cortex manifest cannot admit KOS via the existing KOS package verifier because its KOS schema check rejects the Cortex schema; raw verify_minisign only proves crypto and never product admission. This unit makes no claim that KOS's primitive verifier independently enforces the new Cortex purpose grammar. Sharing a key cannot share artifact/deployment authority. Cross-purpose positives/negatives are future test-root cases, with original independent root/context, not actual signing here.

## Independent production policy

`next/contracts/release/aggregate-policy.schema.json`, schema `cortex.aggregate-policy.v1`, format1, closes root, release, source, sequence_floor, expected_aggregate_sha256, expected_members, installed_baseline, validity, limits and owner_binding; purpose is independently chosen. Exact policy digest, current acceptance/provenance and physical roots are **external**. An artifact cannot choose its own policy/root hash by shipping a schema-valid record. No accepted/trusted flag, test mode, optional fallback or artifact root field exists.

Root fields are fixed key_id, full key_material_sha256, literal owner-public root reference `owner-release-public-key`, actual public_key_bytes, signature_algorithm ED, allowed_purposes and key_use_permission `cortex-core-api-release`. The logical key reference resolves only inside the independently admitted installed owner store; it is not a caller filename. Actual key bytes/length/path/provider/ownership remain undelivered in this row. Existing KOS public policy's material hash `cc789827d21f6db4b3d74c39709b78d2ab6a636b28489d58d3b7ec442b66c8ef` is metadata, not a key file inspected here or an admitted Cortex store. No OpenPGP/subkey scheme is invented for Minisign.

Owner bindings carry original owner/root-store/transport/verified-image/code-closure receipt SHA256, installation/operation UUID and policy generation. Installed baseline carries release ID, original receipt SHA, fence and generation. These are required current subjects to verify with independent owner providers; declaring correct-looking UUID/hash values does not mint them. Trust-code/root custody lives on MAIN or the named private native owner root, never the noowners SSD as private authority. Current revocation/root acceptance is included in independently verified owner/root-store receipt and generation; absence/unknown/revocation cannot default valid. Rotation to another key needs a new named release-authority contract, not artifact-directed root discovery.

Replay defaults: target selection permits only the independently expected current manifest digest/release/source/tuple, sequence≥sequence_floor, exact signed/owner purpose and current policy/installed generation/fence. Current owner time is authoritative; manifest.not_before≤now≤expires_at and owner validity must hold, maximum release age≤604800s and tighter owner cap. The verifier must prove time/state available and current after long audit. A correct signature/key from an older release with its own correct content ID refuses aggregate_replay before current-digest comparison. Same/higher sequence with another current digest refuses aggregate_identity_mismatch; sequence alone does not accept replacements. New signing makes a new original subject and requires policy acceptance. Replay/freshness policy applies even if a requested subject exists in an owner transport map.

Historical baseline reading is distinct from target selection; rollback/counter-replay exception needs a separate narrow current owner purpose/grant and actual state check. Default has **no rollback exception** and cannot lower sequence/fence. Cross-field tuple/ref correspondence, time ordering, current digest, sorted paths, role/ABI/original receipt identity, physical custody, aggregate sum and unknown-member closure are mandatory verifier semantics beyond JSON Schema. The policy's expected_members must exactly equal the verified accepted signed set and all physical originals.

## Backend and existing reader identity contract

Public RC2 calls remain request_member(aggregate_id, member_name) and read_member(member, *, aggregate_id, member_name). Private delivery remains aggregate_id/signing_root_id/member_name/member_sha256/read_original(limit), with raw snapshot≤262144 for the external lock. Proposed sole R01 backend surface is `release/member_admission.py:request_original_member(aggregate_id, member_name)`, used only by fixed installed owner composition; missing production dependencies retain the exact `_unbound` behavior/unit string. No public binder, root/reader callback setter or environment/argv test-root mode. Images use a future separate bounded streamed original-artifact port, not an enlarged OriginalMember.

**Both backend and sealed reader must require type(arg) is str and exact original string equality**. No str(), coercion, normalization, subclass/custom __eq__/__ne__, digest-prefix or filename alias. In a bound authority the backend rejects crafted requests as member_identity_mismatch before any root/member transport; unbound production default remains required_binding_gap before argument/input handling. Backend delivery compares exact requested aggregate/member with independently admitted identities. Reader compares arguments to its registered sealed member before returning bytes; a passed Python object or matching hash alone is no proof.

Vera RC2-IDENTITY-001 is carried as a separate Nemo reader-fix gate, not repaired by U3-A. `r01u3_support/oracle_red.py` freezes six **BODY** controls: equality-object, wrong-value equality string subclass and same-value string subclass, each in aggregate_id/member_name. Existing request port's six exact-request controls pass using a clearly non-authoritative in-memory delivery double; those passes prove its request guard only, not the missing R01 backend. The frozen reader assertions require member_identity_mismatch without invoking crafted comparisons. Future U3-B/D test factory must repeat all six against the **actual bound R01 backend**, not a callback/port surrogate, with transport/root/config/effect spies zero. The unavailable backend/factory is explicitly SURFACE_ABSENCE today, not a claimed six-body verifier result.

Original immutable snapshot is tied to exact manifest/root/member digest under current admitted policy and held descriptor/ancestor/device/byte custody. Return no raw dependency/key/path text. Before any later effect, the independently admitted owner adapter rechecks original actual image/code/private owner authority, scope/state/fence/freshness; OriginalMember or AdmissionPlan is read-only and never an effect ticket. Cached snapshot authenticity does not make a current effect grant.

## Exact refusal vocabulary and precedence

| Reason | Meaning |
|---|---|
| required_binding_gap | missing production root/policy/composition/current original owner/image dependency; finite named unit |
| member_authority_unavailable | unavailable/unexpected backend/transport/provider error, sanitized |
| raw_limit | measured raw/stream/time/count/depth budget exceeded |
| aggregate_unsigned | missing detached signature |
| aggregate_signature_invalid | malformed/truncated signature or failed original-byte/comment signature |
| aggregate_root_mismatch | independently pinned full root material/hash/ID mismatch |
| aggregate_algorithm_unsupported | Ed or unknown algorithm under ED-only policy |
| aggregate_shape | malformed/noncanonical/unknown/version/type/inventory manifest |
| aggregate_identity_mismatch | raw/current digest or independent source/release subject differs |
| aggregate_scope_mismatch | purpose/edition/root key-use/trusted-comment context differs |
| aggregate_replay | old/expired/revoked/stale/time-unavailable policy/subject/state |
| aggregate_pair_mismatch | wrong/swapped/missing Core/API/target/original module/ABI/compatibility tuple |
| member_not_in_aggregate | requested logical member absent from signed accepted inventory |
| member_identity_mismatch | crafted/nonexact/conflicting requested/delivered/read identities |
| member_digest_mismatch | original member length/hash does not match signed row |
| member_path_alias | lexical/physical/link/root alias or embedded-image lock cycle |
| member_custody_changed | original named/held/ancestor/device/bytes changed |
| unsealed_member | forged/unregistered OriginalMember |

Precedence: absent production authority; exact bound request arguments; transport path/raw bounds; requested raw aggregate identity; signature envelope/root/algorithm/crypto; manifest closed/canonical shape; independent scope/current identity/freshness (older sequence before current-digest comparison); paired original closure; requested member/custody; reader registry/exact identity. Existing RC2 wrapper/refusal names are preserved; unexpected backend exceptions map member_authority_unavailable without raw text. Public unit gap text is exactly `R01 unit 3: signed Core/API aggregate and member admission` until separately named owner dependencies become the precise gap. No partially admitted value after refusal.

## Test-only boundary, oracle and Vera gate

No real or test key generation/access/signing is authorized by U3-A. `aggregate.canonical.json` and `production-policy.shape.json` are **inert schema-shape fixtures**, not signed artifacts, OCI archives, real lock or actual owner/root/native receipts. Declared member lengths/hashes and public-key length are deliberately fictional; the key ID/hash are public policy metadata only. Physical members do not exist. The fixture is schema-valid and still completely UNBOUND. SUBJECTS.json records no signature/material/authority. Future valid crypto/OCI positives must use a separate reviewed test corpus; never promote this shape fixture.

Under a future explicit test-material GO only, test support outside installed source may generate ephemeral unrelated Ed25519 material on private MAIN/in memory and create a test-only verifier factory, expected `r01u3_support.factory.test_authority`. Production packaging/import closure cannot reach it; no installed test mode/binder/env override. Full test root pin differs from production; no real release key or native outputs. Wrong root, changed member, swapped Core/API, old replay, unsigned, truncated and path alias cases use actual test-crypto/actual original bytes under that isolated factory. ORACLE_CASES.json freezes those desired reasons and execution state NOT_RUN_UNBOUND. Correct-looking signature text or mocking crypto does not count as a signature positive.

Permanent tests/expected bytes are frozen before schemas; later implementation must make desired bodies pass without rewriting deciding tests. Keep initial/final RED and restoration hashes; absence/import/setup does not earn body credit. New schema guard omissions must fail frozen original BODY assertions, with literal logs and exact restoration; source oracle's six crafted-reader failures remain pending separate Nemo fix. Signature/member/native causal controls remain future units, not U3-A credit.

Vera must select/accept the boundary beyond Nemo's API seal before U3-E: explicit trusted installed interpreter with independent effect owner, or an OS-enforced independent verifier/effect-admission process with private authenticated IPC, independently held code/root/UID/custody/current policy and replay-resistant operation context. Same-UID subprocess or readonly Python object alone does not defend trusted Python rewriting globals. U3-A chooses neither. Native peer/kernel/installed code/root evidence remains NOT_RUN; source simulations cannot close it. Kai routes Vera/Hale/Cox; no new handoff or external judgment API is sent here.

## Cox's questions and proposed defaults — no wait in this freeze

| Question | Default proposed for acceptance | Refusal/gate until actual delivery |
|---|---|---|
| Manifest/content ID and purpose | new schema above; sha256:<original whole manifest SHA>; exact two Cortex purposes/comment grammar | Cox accepts grammar; signing still separate CTO GO |
| Exact signed members | four named image targets +one external lock +complete original per-image input/receipt/evidence and RC0/descriptor/schema/event/compatibility inventory; no generic record replaces four closures | exact owner inventory/digests/lengths/original refs required before U3-C real assembly |
| Core/API module descriptors/ABI | role and target are fixed; exact module IDs/versions and OCI platform-manifest/ABI/closure facts come from current original Cox/R01 receipts, no guessed IDs | aggregate_pair_mismatch or required_binding_gap; fixture-* never production identity |
| Original compatibility/schema/event/operations | R01 current original descriptor/schema/event/migration-prefix subjects; three accepted RC0 hashes byte-exact; compare actual members, not equal declared strings | preserve RC0 originals; full accepted compatibility/installed-state provider needed |
| Replay/sequence | current exact aggregate digest +floor/current owner time/generation/fence; lower sequence rejected; no default rollback; named exception only | aggregate_replay/current identity refusal; no caller downgrade |
| Root/purpose/key-use | one 7BBB root with full owner byte pin/current scope; TEST distinct from production; H-D546 not extended | actual public material/provider/owner custody/signing grants UNBOUND |

U3-B6h +U3-C6h +U3-D6h +U3-E6h +U3-F4h remain **28 author hours**, plus7h remaining independent review after U3-A's1h. Full source remains32author+8review=40h; conditional N1/N2 verifier proof16h; incremental total56h. Extra production-policy schema and explicit six identity controls fit U3-A's4h; no reprice currently proposed. A Cox amendment or Vera boundary requiring further slices triggers updated ≤6h units/costs before dispatch. No future source/native/signing/acquisition/merge grant is implied.
