# Paired recipe SOURCE contract

`cortex.recipe-source-envelope.v1` is a descriptive source planning record.
The two 0644 JSON data files under `next/release/recipes/{core,api}/` identify
the Cortex source revision and tree. Each carries both `linux/arm64` and
`linux/amd64` obligations, each with A and B slots. These correspond to the
Linux ARM64 backend for Standalone macOS arm64 and the Linux x86_64 backend
for Production. They supply no build command, executable recipe or native
release authority.

All 21 runtime fields and all three material fields in every shipped target
are `UNBOUND`. The runtime names preserve Cox's missing runtime composition:
Core schema/migration invocation, shipped layout, service factory, real Core
probe, credentialed/PG ports, provider seam, enrollment, health/status/metrics,
supervisor, backup/restore, signed runtime lock, product launcher, consumer
descriptor, module version, entrypoint, argv, config, context inventory and
module descriptor. No runtime entrypoint, argv or config value is invented.

Every fact uses exactly `state`, `owner`, `value`, `evidence`. An `UNBOUND`
record has owner `cox@helix` and null value/evidence. The parser returns
`UnboundFact`; the planner refuses it before calling any reader. A later
owner contract can fill value/evidence and change state to `BOUND` while
preserving all envelope and field names. The current envelopes still refuse.

`BOUND` has a narrow SOURCE meaning: descriptive JSON matched to canonical
bytes of a regular, non-executable 0644 source observation, with exact path,
byte count and SHA-256. It does not assert that Cox approved the observation
or that a native base, toolchain, image or runtime exists. The owner string
is a source routing label. It cannot grant a capability. Public tests use
explicitly synthetic observations; no such fixture value appears in the
shipped envelopes. Placeholder strings and authority flags are rejected.

`runner.plan_source_recipes(raw_envelopes, requests, fact_reader, roots,
limits)` delegates to the source envelope module. It parses the complete
Core/API matrix and checks every runtime/material binding before reader
calls. It then accepts exactly four `SourceInput` records through the
existing sole R01 `build.inputs.admit` path, with fixture scope only. Native
scope, added caller grants, receipt-like dictionaries and malformed source
records refuse. Existing R01 schemas, reader, native inspector and native
emission refusals remain unchanged.

After R01 admission, the planner binds the literal entrypoint/argv,
descriptor/context inventory, module/version/platform, engine/toolchain
identity and descriptive ABI/image-member paths. It reobserves fact and
input bytes, kind, mode, link policy, sizes and hashes, including a final
four-target sweep after all selections. An interruption or changed source
cannot return a plan. A and B require separate absolute descriptive paths
for source, store, runroot, cache and output, without lexical aliases or
nesting. The arguments are held immutably before any reader callback.

Limits are a closed, finite integer declaration for CPU, memory, disk,
input/output, decode depth, processes, absolute time and six phase times,
plus a closed list of public environment names. No environment values or
secret-bearing names are accepted. The planner does not apply operating
system limits, create these roots or prove physical inode/host independence:
those are later admitted native producer obligations.

The only result is immutable `cortex.recipe-source-plan.v1` metadata with
four requests and held limits. Both plan and requests have
`launchable=false` and `native_qualified=false`. The entry has no process,
network, host, staging, image, execution or receipt-emission port. It never
calls the existing lifecycle executor. The failure/cleanup control verifies
preservation of that existing helper with recording callbacks only.

The twelve refusal controls are permanent test assertions. Omitting the
actual guard in an in-memory copy causes an assertion failure in its body;
restoring exact source passes. The positive path traces eleven new source
guards. The twelfth control traces the retained lifecycle failed-exit guard.
The initial API-absence RED is preserved separately and earns no semantic
causal credit. Additional shape controls cover malformed observations,
boolean ABI/member claims, callback substitution and late custody changes.

This source slice does not close the R02b material gaps or admit a native
input. Owner runtime composition, real offline materials and independent
native custody/inspection remain required. Kai routes Hale mechanics and
Vera trust after the source handback; this author cannot accept those reviews.
