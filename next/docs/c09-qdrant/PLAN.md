# C09 Qdrant OSS module — PLAN ONLY

Author Nemo@helix. Kai accepts before BUILD. Authority: H-D472, Nemo DO NOW
2026-10-10 09:33 local and the matching r426 addendum. Qdrant is now IN, without
the previous Postgres-miss condition. **No product code, image pull, runtime,
private-data access, host creation or benchmark execution accompanies this plan;
the 09:49 amendment is published on a docs-only branch.**
**Kai ACCEPTED 09:49 with one policy amendment: GTM uses isolated networking and
the secret-store API key; private TLS returns only with a multi-host topology.**
BUILD remains held by C04's H-D471 security hold. Plan acceptance alone cannot
release that hold. C07 contract binding, C10 vector synchronization, native
installation, data custody and independent review retain their own gates.

## Intent and boundary

```text
authenticated Cortex API -> current Core authority -> vector-module query port
Core records + stored embeddings -> C06 feed -> C07 independent consumer -> Qdrant
Conductor -> bounded lifecycle control only
O01 consistent backup -> isolated restore -> physical Qdrant restore + Core replay
```

Postgres remains canonical. Qdrant is a replaceable, privately reachable derived
index in both editions. C09 pins and admits the engine, its connection/configuration
and module boundary. C10 owns model-versioned sync/query/filter/cutover; C07 owns
apply/checkpoint/rebuild semantics. There is no dual authoritative writer, client
engine access, engine-selected provider, automatic fallback or Conductor dependency
on the request/consumer path. Existing API envelopes and Postgres search stay intact
until an accepted binding selects a backend.

## Exact upstream identity and licence

Proposed engine: **Qdrant OSS v1.19.2**, released 2026-10-05. Upstream tag object
`721cf8793c0ee5953273b9e1d1bf4613adcd34bc` resolves to source commit
`016542aa5deb6c66380bb137badf73d54f742bde`; GitHub reports its tag signature verified.
This is upstream metadata, not a local signature-chain or reproducible-image proof.
[Release](https://github.com/qdrant/qdrant/releases/tag/v1.19.2).

The tagged server licence is **Apache-2.0**, SHA256
`210b508429e913d9de5301f90508bc2cbf5b2281de5b45e607c04d58f0f3bd8f`.
Ship the licence and required attribution/notices, identify distributed modifications,
and retain applicable dependency notices. The server licence does not establish the
licences of every Rust dependency, OS package, web UI or included binary. R01/R03
must bind complete per-platform SBOM/licence inventories before redistribution;
an unknown or disallowed component fails that gate. No Qdrant Cloud or proprietary
managed service is selected. [Pinned licence](https://github.com/qdrant/qdrant/blob/016542aa5deb6c66380bb137badf73d54f742bde/LICENSE).

Registry identity is `docker.io/qdrant/qdrant:v1.19.2`, index digest
`sha256:b7b0444c4c351c970b98e90a6f89c2ee4287c65b44e52b4cb503fa5b2aa927ad`.
Use the exact platform child manifest, with the readable version retained in the
aggregate release manifest:

| Edition / actual runtime | Immutable child manifest |
| --- | --- |
| Production: Linux x86_64 (`linux/amd64`) | `sha256:0e8273b9130ca3b0dd9dcfaa8f55342066711f8aaeb2aa072e53344c42ecf57f` |
| Standalone: Mac arm64, **Linux arm64 inside Podman VM** | `sha256:9eb80ba088703193c124db5e50a59f2028439c8bbc62fa1918625995116b50d0` |

Raw index/child/config bytes were fetched over verified TLS and rehashed against
the registry digests. Both image configs advertise v1.19.2 and `User=0:0`.
No layer was downloaded, image executed or embedded binary version/SBOM checked.
An index pin alone cannot substitute for verifying the selected child platform.
See [pins](upstream-pins.json), the `oci-*.raw.json` files and [source evidence](upstream-source.json).
Digest updates are proposed by the module/build owner after upstream fixes, with
new dual-platform tests/review and aggregate pins; a floating tag never updates a
deployment silently. The BUILD dispatch must confirm that update owner.

For completeness, GitHub advertises native asset digests: Linux GNU x86_64 tar
`sha256:34a788a09a4cb278b5c6d2af96d9a6db30c8c7e88ae3550455f7a4d425bb8d4b`,
Darwin arm64 tar
`sha256:d24dfd25f4584684b337dd30ee016472cd25f53fc2501f1cd058ab3d7ee0b019`.
These assets were not downloaded or executed. **Darwin-native Qdrant is not the
accepted Standalone runtime or Linux-to-Mac snapshot matrix.**

## Private configuration and runtime proposal

Use a separate rootless engine workload on the already approved private runtime
network, with **no host/public published port**, no host networking and no privileged
mode. Only the internal adapter and named backup/restore service may reach it.
Do not equate an image's EXPOSE metadata with port publication. The internal
network must have no public route; deny unsolicited engine egress, including
private peers it does not need. Network isolation is enforced and probed, not
inferred from Qdrant settings. Native adapters must name the actual enforcement
mechanism; an internal bridge alone does not prove every peer connection denied.

Proposed Qdrant settings (documentation blueprint, not an installed config):

```yaml
service:
  host: <assigned-private-interface-IP>
  http_port: 6333
  grpc_port: null
  enable_cors: false
  enable_tls: false
  enable_snapshot_url_recovery: false
cluster:
  enabled: false
telemetry_disabled: true
```

REST only initially; disable gRPC and clustering rather than leaving their base
defaults active. Bind the assigned private interface, not a host-wide wildcard.
For the GTM single-host topology, private HTTP relies on the proved network
isolation and Qdrant's API key supplied from the secret store. Require distinct
internal writer/administration and read-only credentials, neither available to
public callers. Missing identity or credential refuses startup/connection. TLS
and verified certificate trust return only with a multi-host topology. API keys do
not replace Core tenant/permission checks. No secret literal, environment dump, private vector or
dependency exception text appears in config evidence or logs.

Use local snapshot storage only; no cloud credential chain, remote snapshot URL,
automatic embedding/inference or public dashboard route. The application sends
already stored/query vectors. The pinned config supports the required controls;
native effective settings and deny tests are still NOT_RUN.
[Configuration](https://github.com/qdrant/qdrant/blob/016542aa5deb6c66380bb137badf73d54f742bde/config/config.yaml),
[upstream security guidance](https://qdrant.tech/documentation/security/).

Override the upstream root user with a dedicated mapped numeric non-root UID/GID
from native inventory. Read-only root, ALL capabilities dropped, no new privileges,
default seccomp and explicit owned storage/snapshot/temp mounts are required.
Use the verified binary as PID 1 with exec-form launch, or qualify the upstream
wrapper's signal/reaping behavior; do not assume its shell wrapper meets that
contract. Storage initialization, SIGTERM, hard kill, restart and OOM produce
truthful states. Engine liveness never depends on Core/Conductor availability and
never restarts Core. Readiness proves usable engine/configuration; module readiness
also requires the correct generation and consumer state.

Resource values are hypotheses until measured. After the BUILD gate, a small
synthetic two-container fixture proposes **aggregate** <=1 GiB/2 CPU (512 MiB/1 CPU
each for PG and Qdrant), owner labels, fresh tmpfs and no SSD bind mounts. Run
larger inherited PG suites separately, after removing that fixture. One v2 stack
team-wide; >=30% `memory_pressure` free before each local start. A failed budget
returns RED for resizing; it cannot silently increase the limit. Native B02 uses
its separate admitted hosts/envelope, not this shared Mac stack.

No actual resource/network/subnet name is assigned by this plan. The workspace
lacks the skill's referenced `docs/sops/infra-naming.md`; retain that lookup refusal
in [scope evidence](pre-plan-scope.json). Before provisioning, the native owner
must verify inventory, canonical naming source/approved project convention, UID,
private subnet, volume ownership and policy. No new VM, cloud primitive, Kubernetes
deployment or project-local naming exception is invented here.

## Known seam and proposed module interface

Source inputs are pinned to main `3401d1a1b4be7a3a115a440d5415ea937ff8e003` in
`current-source/` and [scope evidence](pre-plan-scope.json). Main contains C01's
event schema and provider/search seams; it does **not** publish C07's descriptor/
apply/checkpoint ABI. C06's accepted plan is a design input, not merged runtime
qualification. The C09 connection fixture can use the accepted draft without
waiting for another lane. Reconcile final C07/R01 names, versions and receipts
before binding a descriptor or admitting the integrated module READY; missing
bindings remain explicit and unavailable. The following names are proposed semantics.

| Port / owner | Required contract |
| --- | --- |
| Descriptor / C09 plus R01 | Engine/version/platform digest; licence/SBOM identity; supported module, API, schema and event versions; supported metric/vector dimensions; limits and compatible Core range. Incompatible/unknown combinations refuse. |
| Connect/probe/describe / C09 | Bounded isolated private HTTP connection and secret-store API key for GTM; TLS only for multi-host; no retry storm, environment proxy, redirect or URL-selected destination; exact engine/config identity and safe typed state. Unknown response/config refuses. |
| Apply / C07+C10 binding | Immutable event and payload digest, trusted installation/tenant/project, aggregate kind/ID/revision, operation/tombstone, source revision, embedding identity and generation. Durable effective apply precedes checkpoint. |
| Query / C10+API binding | Current server-authorized scope and atomic active generation/query-model pair. Mandatory tenant/project filter plus authoritative permission/source-revision validation; bounded results, existing distance/identity/freshness contract. |
| Snapshot/rebuild/restore / O01+C07+C10 binding | Consistent Core snapshot boundary, module manifest, per-generation data and replay accounting. Isolated shadow target, complete reconciliation, then explicit fenced cutover. |
| State / C09+C07 binding | Distinguish disabled, connecting, catching up, ready, unavailable, expired, quarantined and rebuilding. Failed/disabled/rebuilding search is typed capability-unavailable, never fabricated empty success. |

C01 event v1 already fixes event/installation/tenant/project/aggregate/payload UUIDs,
aggregate kind/revision, schema version, operation, tombstone, SHA256 and occurrence
time. Occurrence time/event IDs are not feed cursors. C06 proposes the separately
committed journal cursor; C07 must publish its exact delivery/checkpoint shape.
Unknown versions/payload mismatch are accounted for in Core quarantine, blocking
only the affected aggregate. Replay duplicates/reordering/deletes cannot overwrite
a newer effective revision or resurrect a tombstone. Expiry beyond the retained
feed (default seven days) requires snapshot rebuild, not checkpoint advancement.

Qdrant RPCs are external effects: **never call them inside `Supervisor.guarded()`**
or describe a PG rollback as undoing an engine write. C07 must freeze the apply/
ack/checkpoint and per-aggregate concurrency boundary. C10 must demonstrate a
destination-enforced stale-write strategy, including a delayed old RPC finishing
after a newer write/delete; a PG pre-check or process lease alone is insufficient.
No Qdrant compare-and-set facility or exactly-once transaction is assumed here.
If the pinned API cannot satisfy that strategy, return a scoped design consult
before code; do not weaken the invariant. Conductor death cannot stop an already
admitted independent consumer or a valid query.

## Embedding identity, generation and authority

CXV2-010 binds provider/model, immutable version, dimensions and the complete
preprocessing/chunking configuration hash; every vector also binds source revision.
Reuse the existing `EmbeddingIdentity.key` representation (provider, model, version,
dimensions, preprocessing) rather than introducing a divergent module hash. Current
search admits 768 dimensions; extending that is a separate contract decision.

Each collection generation represents exactly one embedding identity and vector
configuration. Point/payload identity includes trusted tenant/project, aggregate
kind/ID, source revision, identity and generation. No caller field selects a tenant,
model or alias. Define deterministic collision-checked point mapping in C10; it must
preserve kind and source identity, including identical record IDs in different scopes.
Use float32 Cosine as the initial unquantized comparison candidate, with exact
normalization/distance conversion bound to the frozen oracle; tuning/defaults are
selected from B02 evidence, not declared accepted here.

Core atomically selects query-model and active index generation for each request.
An engine alias alone cannot atomically switch the model/provider. Delayed provider
responses and old-generation events cannot become current. A model change builds
and reconciles a shadow generation; rollback requires the old generation to be
current or explicitly caught up. Same-identity rebuild uses vectors stored in PG
and makes **zero provider calls**; re-embedding with a new identity needs its own
accepted spend. Unknown model provenance stays unknown: a benchmark-only geometry
identity cannot become a production embedding identity.

## Backup, restore and Linux-to-Mac matrix

Qdrant snapshots are derived recovery artifacts, not substitutes for authoritative
PG records/vectors, immutable blobs, schema/event ledgers and O01's consistent
boundary. For a synthetic qualification fixture, freeze writes/consumer at an
admitted boundary, drain and record effective apply, then capture collection
snapshots plus an explicit inventory of collections, aliases, configurations,
payload indexes, IDs/revisions/tombstones, vectors/payloads and checkpoint/identity
mapping. Do not assume a collection snapshot includes every deployment-level alias
or consistent Core state. Hash/encrypt the artifact set through existing O01 custody.

The **physical matrix** is v1.19.2 `linux/amd64` -> v1.19.2 `linux/arm64` inside
the Mac Podman VM, single-node **collection snapshots**, exact child pins above.
Upstream documents version compatibility, but provides no cross-architecture
acceptance result for this matrix. That result remains NOT_RUN.
[Snapshot reference](https://qdrant.tech/documentation/snapshots/).

Restore local files/uploads into an empty isolated target; no remote fetch URL,
production address/credentials or automatic worker/provider dispatch. O02 requires
support mode, a new installation identity/provenance record and replaced secrets
before opening customer data. Bind every scope/installation remap explicitly.
Unredacted customer restore still needs its named CTO admission.

Before declaring readiness, reconcile complete collection counts/configuration,
IDs, payloads, stored-vector representation, tombstones/deletes, aliases and
embedding/generation mapping against the pre-backup manifest. Use exact ID/payload/
revision checksums; define float32 representation/tolerance before the run rather
than adjusting it after a mismatch. Query the frozen exact reference with tenant
filters and ties, not just a top-10 sample. Corrupt/missing/wrong-version/wrong-
identity inputs refuse without overwriting a current generation. Measure restore
time, peak RAM, disk/I/O and additional snapshot/shadow space; hard-coded capacity
headroom is not evidence. Keep source artifact hashes unchanged after transfer.

The **logical rebuild** is a separate timed proof: consistent PG snapshot and stored
vectors, replay from its commit-safe boundary, account for retention expiry and
poisoned aggregates, reconcile shadow data and publish a coherent model/generation
pair. Count provider calls as zero. A failed physical Linux-to-Mac restore reopens
the Qdrant choice under CXV2-009/H-D438; a successful logical fallback is not a waiver.
B03/B04 and O01/O02 retain full crash/support/native qualification; C09 cannot close
those gates with a source fixture or top-10 comparison.

## Fail-capable acceptance and execution order

Everything below is **NOT_RUN** unless explicitly listed as research evidence.
No green source or native result is claimed by this plan.

| Check / output that can fail | Required failure cases |
| --- | --- |
| Pin/licence/compatibility admission | Wrong digest/platform/version, incomplete SBOM/licence, unknown descriptor/API/event version; do not start incompatible bytes. |
| Effective private runtime inventory | Published/wildcard/host-network port, reachable outside approved peers, successful unsolicited egress, UID0, missing mount ownership/credentials, extra listener or secret in environment dumps/logs. Prove both authorized reachability and refused direct access. |
| Lifecycle and outage | Partial startup, timeout, malformed response, signal/cancellation/OOM/hard kill; bounded cleanup/reap with engine-unavailable state while Core remains available. |
| Scope/identity boundary | Missing/forged scope/filter, cross-tenant same-ID collision, stale revocation, mixed identity/dimension/source revision; no leak and no empty-success substitution. |
| C07/C10 effective apply | Duplicate/reordered/newer/delete events, crash before/after engine apply and PG checkpoint, delayed old RPC, expired consumer, poisoned payload; exact independent ledger and no stale overwrite/resurrection. |
| Physical transfer + restore | Full metadata/ID/vector/payload/delete/alias reconciliation, corrupt/missing input, exact version/platform, provider calls0, filtered exact-reference retrieval. A mandatory failure stays RED. |
| Logical rebuild | Consistent snapshot+boundary, writes/deletes during catch-up, retention expiry, shadow checksums, cutover/rollback and measured resource/time ledger. |
| Proof custody | Frozen genuine test-body RED before implementation; full GREEN/restored suites; named semantic faults per new source with shared classifier; runtime/fixture/cleanup errors INCONCLUSIVE; exact-head independent audit and CI. |

Proposed eventual source paths, all absent in the inspected 122 refs:

| Path | Bounded purpose |
| --- | --- |
| `next/docs/c09-qdrant/PLAN.md` | Fold the accepted byte-bound plan. |
| `next/docs/c09-qdrant/CONTRACT.md` | C09 semantics, compatibility and remaining gates. |
| `next/modules/qdrant/module.json` | Proposed descriptor, reconciled with published C07/R01 schema before edits. |
| `next/modules/qdrant/config.yaml` | Non-secret private configuration; concrete runtime addresses are injected. |
| `next/src/cortex_core/modules/vector/qdrant.py` | Bounded transport/config admission and engine metadata/connection surface; reuse existing httpx0.28.1 where admitted. |
| `next/tests/integration/test_qdrant_module.py` | Frozen synthetic pin/private connection/lifecycle controls. |
| `next/tests/integration/mutate_qdrant_module.py` | Literal named faults through existing bound runner/classifier. |

C09 does not edit Core/C04, shared schema/runners/classifier, public API, production
sync, native service units or another lane's files. If a new test finds such a defect,
retain RED and return an amended scope/plan before its fix. Final descriptor paths/
ABI/dependency custody must be accepted before their binding, not improvised while coding.

Order: (1) Kai accepts this document. (2) C04 security hold is explicitly released;
confirm dispatch/review owner and known draft compatibility limits. (3) Fresh
scope/main/ownership check; own worktree and frozen controls/real RED. (4) Fixture
connection implementation and bounded tests, preserving assertions. (5) Full clean/
mutant/restored bound proof, independent review, current-target merge and one PR.
(6) Bind the final C07/R01 contract when published; native/backup/C10/B02 acceptance
proceeds only through its existing gates. No new cross-lane wait is created for
the bounded C09 connection fixture.

Sizing: C09 connection/config source is one six-hour source day (1 contract/RED,
2 implementation, 2 verification, 1 custody/review return). Cross-architecture,
C10 and full rebuild/native proof are separate accepted units. Missing C07 ABI,
runtime policy or a new product defect returns an honest remainder/resize to Kai;
it is not hidden inside that day.

## What B02's Qdrant arm must prove

H-D472 approves **both Postgres and Qdrant**, both editions, **Mac warm-only** and
isolated Linux cold+warm, with a **$3 total cost cap**. It supersedes the older
Postgres-only/Qdrant-on-miss clauses in Mike's retained B02A plan. Execution still
needs Vera's PR43 ACCEPT, reviewed import, Ren-KOS's admitted host after attempt5
and Ren-TK's custody. This C09 document grants no data/host/spend/cache-reset action.

1. Freeze identical corpus/query IDs, source hashes, eligible predicates/selectivity,
   exact truth/tie handling, vector normalization, k, cache state and whole-query
   boundary for both arms. Exclude held-out query IDs from their searched corpus.
   Bind the Qdrant child/version, collection settings/index build state and actual
   platform; expose approximate versus exact search and any tuning separately.
2. The current Marlow set is **141,511 x 768**, geometry-only with unknown per-record
   model provenance. It is not 2.2M records, semantic queries, authoritative tenant
   grants or provider equivalence. Larger 2.2M/5M targets and absent product/scale
   coverage remain NOT_RUN without admitted measurements; no extrapolated PASS.
3. Use the accepted eight persistent clients and 40RPS scheduled arrivals, queue-
   inclusive complete-response latency, 10s absolute request deadlines, all failed/
   missed offers in accounting, successful throughput >=38RPS and zero errors.
   Keep nearest-rank worst-run p95, one-minute windows and independent exact-query
   recall/safety; do not pool away a failed stratum or count repeated queries as
   independent recall evidence. Retain recall>=.95 and Linux100ms/Mac200ms gates;
   a changed budget is a Kai/CTO ruling.
4. Report geometry/engine transport diagnostics separately from product acceptance.
   Full product latency includes current Core authorization/filtering, query-vector
   cache/provider and the public API when those bindings exist. A direct Qdrant
   timer or stale auth projection cannot close the end-to-end gate. Missing sparse,
   semantic, tenant/delete/model cells are NOT_RUN, even if dense recall is high.
5. Measure the whole 8GB Standalone stack and admitted Production minimum: resident
   memory, CPU, disk/I/O, connections, index/import duration, backlog/freshness,
   provider/cache observations and backup/shadow copies. No memory waiver from an
   engine-only number or warm page cache. Mac cold remains explicitly unqualified;
   do not evict/reset the shared Mac cache to manufacture a cold measurement.
6. Preserve per-offer/raw/native/resource/cleanup evidence and independent review.
   B02's selected default needs the complete admitted comparison, not an automatic
   switch or benchmark author's preference. Crash, long soak and physical restore
   belong to B03/B04 and are not claimed from B02 latency results.

Conditional arithmetic, preserving the current three5min-run/one1min-warmup
proposal: six populated cells, one dense mode, two engines, Linux cold+warm and
Mac warm-only => **108 measured intervals / 9 aggregate host-hours**, plus
**72 warmups / 1.2 hours**. Import/index/oracle/preflight/reset/cleanup add time.
This is a sizing check, not a frozen execution matrix, a price estimate or authority
to exceed $3. Mike must reconcile/rescale his envelope and return a cap breach
before execution; missing cells do not reduce the required gate. See
[arithmetic receipt](b02-envelope-arithmetic.json).

## Research evidence and accepted ruling

[Scope/source receipt](pre-plan-scope.json): 122 refs checked, no existing proposed
paths, exact source/document hashes and shared checkout state. [Upstream pins](upstream-pins.json)
and raw manifest/config bytes verify the two platform digests. Tagged licence,
configuration, Dockerfile and entrypoint plus retrieved snapshot/security pages
are retained with [source hashes](upstream-source.json). Initial default-Python CA
verification refused; it was retried with a trusted CA bundle and verification
enabled, never an insecure bypass. [Refusal receipt](research-refusals.json).

Kai accepted this bounded C09 plan and future file map at 09:49 with the single
GTM transport amendment above. BUILD/review/update owner follows the later named
dispatch; explicit C07/R01 limits remain until integration.
All runtime, licence inventory, benchmark, security, restore and native results
remain NOT_RUN. C04's hold stays in force. Nemo rereads DO NOW after this return;
X02 review findings pre-empt when Kai dispatches them.
