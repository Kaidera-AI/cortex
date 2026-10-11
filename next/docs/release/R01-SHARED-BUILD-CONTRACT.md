# R01/R02 shared pre-build and build-receipt contract

Authority: Kai21:25 / R01-R02-PLAN-001. Owner: R01/ren-cx@helix.
Status: proposed paired-plan amendment; no implementation/native/signing go.
R02/ren-kos consumes these names/bytes unchanged, with no aliases or translations.

## One acyclic sequence and separate decisions

```
accepted source/recipe/dependencies/builder requirements/policy
  -> R01 admit_build_input -> VerifiedBuildInput
  -> R02 independent A/B builds and raw artifact/evidence/cleanup receipt
  -> R01 verify_build_receipt -> VerifiedBuildReceipt
  -> R01 assemble unsigned aggregate from verified module receipts
  -> CTO signs original aggregate bytes
  -> R01 qualify_deployment -> VerifiedDeployment
  -> product installer (recheck actual state, artifact/trust custody, then effects)
```

Build admission does not require a newly built output, production aggregate or
production signature. Deployment admission cannot replace build admission or
authorize a builder. None of the three typed decisions allocates a host, signs,
migrates, installs or grants those owner gates. A fixture decision cannot become
a native build/deployment receipt by relabeling its result.

## Exact names, APIs and repository ownership

| R01-owned item | Fixed name/path |
|---|---|
| Pre-build schema ID/version | `cortex.build-input.v1`, `format_version:1` |
| Pre-build JSON Schema | `next/contracts/release/build-input.schema.json` |
| Build-receipt schema ID/version | `cortex.build-receipt.v1`, `format_version:1` |
| Build-receipt JSON Schema | `next/contracts/release/build-receipt.schema.json` |
| Input parser/admission | `next/src/cortex_core/release/inputs.py`: `parse_build_input(raw_bytes)`; `admit_build_input(input, input_reader, accepted_policy) -> VerifiedBuildInput | BuildRefusal` |
| Receipt parser/verification | `next/src/cortex_core/release/receipts.py`: `parse_build_receipt(raw_bytes)`; `verify_build_receipt(receipt, verified_input, output_reader, evidence_reader, qualification_policy) -> VerifiedBuildReceipt | ReceiptRefusal` |
| Shared domain/assembly | `next/src/cortex_core/release/contracts.py`: immutable decision types, supported combinations and `assemble_aggregate(verified_receipts, compatibility, release_identity)` |
| Post-sign qualification | `next/src/cortex_core/release/qualification.py`: `qualify_deployment(original_aggregate, signature, trust_policy, artifact_reader, actual_installed_state, edition) -> VerifiedDeployment | DeploymentRefusal` |
| Plumbing | `next/src/cortex_core/release/trust.py`, `artifacts.py`; one R01-owned package `__init__.py` |
| Product CLI | `next/src/cortex_core/cli/release.py`: `admit-build`, `verify-build-receipt`, `assemble`, `qualify-deployment` |

This package layout supersedes the original plan's flat `release_*.py` proposal
and aligns with R02's `cortex_core.release.build` subtree. R02 owns its build
subtree/engine/policy/audit/runner adapters. Its `build/inputs.py` and
`build/receipts.py`, if needed, delegate to these R01 parsers/verification and do
not define wire schemas or alternate compatibility policy. The earlier guessed
`reproducibility.schema.json` name is not an additional schema authority.

CLI boundaries take explicit input record/digest, read roots, accepted policy
record/digest and output path. Their outputs are structured decisions/refusals.
R02's CLI accepts the original `cortex.build-input.v1` and obtains
VerifiedBuildInput through R01; it never accepts a caller's PASS header.
R01 verify-build-receipt remeasures actual artifacts/evidence before assembly.

## Common encoding, bounds and inventory types

Both documents are UTF-8 JSON without BOM; every string must be valid Unicode
and NFC, with no surrogate or forbidden control character. Duplicate keys,
nonfinite/floating numbers, unknown fields, unsupported versions and wrong
primitive types refuse. Canonical document encoding is sorted object keys,
no insignificant spaces, Unicode emitted as UTF-8, and exactly one trailing LF.
The parser rejects a noncanonical representation; it never repairs accepted
bytes. All integers are JSON integers, never booleans. Canonical encoding is
product code shared by both parsers, not R02's private reserialization.

Limits: input document <=1MiB; receipt <=8MiB; module IDs/versions/role names <=128
bytes; path <=4096bytes; an argv element <=4096bytes/argv<=256elements; source
inventory<=50,000 entries; dependency inventory<=20,000 entries; evidence
inventory<=10,000 entries. Size/count must be nonnegative, explicit and bounded
by the accepted module policy. All SHA256 values are64 lowercase hexadecimal;
Git revisions/trees40 lowercase hexadecimal. No partial/prefix digest matches.

The SHA256 identity of either document covers its **exact canonical whole bytes**
and is supplied externally as a file record `{path, bytes, sha256}`. Neither
document contains its own digest. Inventories are sorted unique relative paths,
without `..`, absolute names, escaping/aliasing links or duplicate conflicting
members. Each `InputFile` is `{path, kind, bytes, sha256, mode}` with `kind` one
of `regular`, `symlink`; mode is a reviewed integer permission value. Symlinks
also require a signed `link_target` and are limited to an explicitly allowed
in-root target; no file can select its own trust hash. Actual observed physical
roots are supplied to readers and remain outside the canonical build key.

`ArtifactFile` is `{role, path, media_type, bytes, sha256}`. Paths are bounded
logical relative inventory paths; media types are an explicit schema enum,
not guessed from extension: `application/x-tar` for uncompressed raw OCI-layout
archive, `application/vnd.oci.image.index.v1+json`,
`application/vnd.oci.image.manifest.v1+json`,
`application/vnd.oci.image.config.v1+json`, `application/octet-stream` blobs,
and `application/json` SBOM/provenance/semantic reports. The archive's exact
uncompressed raw bytes/serialization are the R02 signed-output candidate;
transport envelopes are separate custody, never equality authority. R01 checks
the declared media shape and complete referenced OCI blobs/config/platform.

## Closed pre-build document fields

One input document selects **one module × one OCI platform**. R02's first matrix
is Core/API × amd64/arm64 × independent A/B from those four admitted inputs.
R03 adds Conductor using the same schema, not a renamed recipe format.

| Required field | Type and exact content |
|---|---|
| `schema`, `format_version` | literal `cortex.build-input.v1`; integer1 |
| `target` | object `{module_id, module_version, oci_platform}`; platform enum `linux/amd64`, `linux/arm64` |
| `source` | `{repository, revision, tree, context_root, inventory}`; immutable permitted repo ID,40hex pins, relative context root and complete InputFile list |
| `descriptor` | InputFile for R01 module/capability/interface descriptor; independently accepted ID/version must match target |
| `recipe` | `{recipe_id, version, entrypoint, inventory, argv_template}`; version integer1; recipe entrypoint in exact inventory; no shell-evaluated argument text |
| `dependencies` | `{locks, assets, base_images}`; lock/asset InputFile lists; base images `{role, reference, platform, manifest_digest, config_digest, provenance}` with exact input digests, no floating tag authority |
| `builder_requirements` | `{execution_kind, os, architecture, rootless, uid_min, toolchain, engine, storage_policy}`; execution_kind literal `native`; arch matches OCI target; rootless booleantrue; uid_min integer1; toolchain/engine exact versions and input executable/image/dependency digests; fresh independent mutable stores required |
| `determinism` | `{policy_id, version, epoch, clock_channel, python, file_metadata, account_log_cache, serialization, environment}`; version1; epoch integer0..253402300799; ONE reviewed clock channel; checked-hash/final-path/compiler/optimization/seed policy or explicit non-Python inapplicability; complete allowlisted file/account/log/cache/xattr/ACL rules; exact JSON/tar/compression policy; public build-env allowlist only |
| `resources` | `{cpu_millicores, memory_bytes, disk_bytes, phase_timeouts_sec, absolute_timeout_sec}`; positive integers; phase map has exact required stages and deadlines<=absolute budget; accepted policy owns allowed maxima |
| `compatibility_inputs` | `{compatibility_contract, schema_manifest, api_descriptor, event_index, event_schemas, dependency_interfaces}`; all existing accepted InputFile/digest records; producer/consumer version identities checked against their bytes |
| `admission_policy` | `{policy_id, version, policy_sha256, qualification_scope}`; version1; scope `fixture` or `native`; independently supplied accepted policy must match; document cannot approve itself |

Nested maps also have closed schemas and exact module-policy-owned key sets.
Secrets, private keys, DSNs/customer data, actual host/VM/container/store/PID/
output identities and future output, build-receipt or aggregate digests are
forbidden. A base image/dependency digest is an existing **input**, not a future
module output. Paths/hashes of known source/schema/recipe inputs are admitted
before builds; they do not depend on the aggregate later produced.

`VerifiedBuildInput` contains exact input digest plus target, complete measured
input inventory and matched policy/requirements. Build admission checks every
actual input byte/mode/path and accepted identity before invoking a builder.
Changing recipe/platform/source/inventory/dependency/toolchain/policy changes
the input key and invalidates prior evidence. It performs no native allocation.

## Closed shared build-receipt fields

| Required field | Type and verification rule |
|---|---|
| `schema`, `format_version` | literal `cortex.build-receipt.v1`; integer1 |
| `input_sha256`, `target` | exact admitted input digest and byte-identical target object; no translated field names |
| `qualification_scope`, `result`, `reason` | scope `fixture`/`native`; result enum PASS/RED/BLOCKED/ENVIRONMENT_UNQUALIFIED; bounded stable reason enum; only complete native PASS can supply production aggregate artifacts |
| `builds` | object with exact keys `A`,`B`; distinct independently observed build identities and complete records below |
| `comparison` | `{raw_archives_equal, raw_archive_sha256, raw_archive_bytes, oci_closure_equal, semantic_audit_equal}`; booleans are checked against actual bytes/reports, never trusted labels |
| `audit` | `{policy_id, policy_sha256, required_classes, reports, findings}`; complete semantic/cache/account/log/label/xattr/ACL/input/runtime smoke classes or explicit policy-bound inapplicability; report ArtifactFile map and bounded typed findings |
| `evidence` | `{inventory, index}`; complete ArtifactFile inventory, separately generated index FileRecord covering raw argv/exits/measurements/audit/custody/cleanup; index never includes itself recursively |
| `custody` | `{complete, partial, missing_roles, receipt}`; complete boolean compared to required indexed artifact/evidence closure; partial/missing is not PASS |
| `cleanup` | `{complete, receipt, remaining_owned_resources}`; remeasured cleanup receipt/observed results; unknown, missing or remaining resources cannot produce PASS |

Each A/B record has exact fields `build_id`, `observed_builder`, `source_sha256`,
`recipe_sha256`, `dependency_sha256`, `started_at_unix_ns`, `finished_at_unix_ns`,
`operations`, `outputs`, `oci`, `provenance`, `custody`, `cleanup`.
Observed builder includes native OS/arch/kernel, UID/rootless mode, actual engine/
compiler versions and hashes, build/execution/store/runroot identities and
fresh-store admission evidence. R01 compares those observations with the input's
requirements; it never copies observed identities into the pre-build input.
Times are nonnegative integers, ordered and within accepted budgets.

Operations bind actual full argv, public environment-name/policy attestations,
exit/status, phase timestamps and indexed raw evidence. A/B stores/runroots/
containers/source materializations/build IDs must be distinct; immutable verified
downloads may be shared only when policy permits. Both builds must actually
execute; two exports of one build or adoption of A's image by B refuse.

Outputs contain the full raw archive ArtifactFile plus complete OCI index,
manifest/config/layer/blob inventory. Provenance binds admitted source/recipe/
dependencies/builder/policy to each independently observed output; SBOM binds
the actual selected output. Semantic audit is exhaustive and cannot waive
whole-byte inequality or repair exported bytes. R01 rehashes and inspects the
full closure using the same product parsers/readers, proves equality and cleanup,
then returns `VerifiedBuildReceipt` with receipt/input/output/evidence digests.
It is **not** an aggregate signature, deployment decision or native runtime gate.

## Upgrade: installed state versus expanded target

The installed manifest and actual applied ledger are independently admitted
current state. The **target** signed aggregate may append reviewed migration
IDs/bytes after the installed prefix. Positive: installed `[m1:h1,m2:h2]`, target
`[m1:h1,m2:h2,m3:h3]`, supported old/new module/API/event tuple and unchanged
prior hashes -> expand transition admitted. Missing/reordered/changed prior
entry, unknown installed ID, state generation change, unsupported producer/
consumer ABI or unbound migration -> refusal before SQL/writer activation.

Target additions do not mean the installed ledger already includes them.
Cox's migration port owns execution/expand/cutover/contract and immutable
checksums. Installer state CAS/fence is rechecked before effects. Rollback is
allowed only under the signed pre-acceptance transition; post-acceptance image
rollback cannot reverse schema/data and is refused. Irreversible steps require
fresh backup and named CTO gate, outside R01 build/deployment values.

## Shared round-trip and deciding mutations

Add R01-owned repo tests `next/tests/release/test_build_contract_roundtrip.py`
and `test_build_receipt.py`; R02 imports the same fixtures and parsers. One
positive round-trip: exact CLI admitted input -> R02 fixture A/B producer ->
canonical shared receipt -> R01 API/CLI verification -> unsigned aggregate
assembly -> isolated TEST signature/trust fixture -> VerifiedDeployment -> inert
installer spy once. Fixture/native/signing fields stay labeled; this proves
format/producer/consumer/effect wiring, not real native A/B or CTO signing.

The same input/receipt bytes must parse via R01 API and CLI without translation;
R02 cannot supply a differently named receipt or bypass signature downstream.
Before output exists, admit-build can succeed from complete accepted inputs;
deployment qualification must refuse missing artifacts/signature. This is the
deciding no-cycle test, with each stage's distinct typed decision asserted.

| Mutation/refusal | Original deciding assertion |
|---|---|
| Change/remove/add input inventory member, source/recipe/dependency/builder policy or actual input bytes while reusing original key | build admission refusal, builder spy0 |
| Replace platform/clock channel or insert future output/aggregate digest into input | strict parser/admission refusal, builder spy0 |
| Receipt input digest/module/platform/source/recipe mismatch | R01 verification refusal; no aggregate assembly/installer call |
| Reuse A identity/store/image as B or omit a build/exit | independent-build refusal, no verified receipt |
| Alter raw archive bytes/size/media or OCI manifest/config/layer platform/closure | whole artifact/evidence refusal; normalized-equality label cannot rescue |
| Omit semantic/evidence/custody/index/cleanup or mark complete over missing bytes/resources | verification refusal, no assembly/install; early partial receipt retained |
| Tamper verified receipt after assembly or trust TEST/old production aggregate | downstream aggregate/deployment refusal; build admission unchanged |
| Ignore installed prefix/CAS or deny valid appended target migration | missing/changed prefix and stale state refuse; valid append still admits |

Guard-removal mutants must die at these original assertion IDs with zero
operational/setup errors; survivors/inconclusive keep the paired gate RED.
R02 native parser/eight actual builds and R03 remaining-module matrix are later
real-target receipts using this same schema; no archived v1 pass or fixture can
qualify them. CTO signing, native deployment and release remain visibly pending.
