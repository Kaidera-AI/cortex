# E0 selection and egress contract — RC0-E0-001 proposed

Status: E0 author freeze, **unratified and unimplemented**. Kai accepted E0–E2
scope in handoff 10eadb99-d485-4004-9b9d-505bd5c66f14. This unit adds only docs
and tests. Cox owns RC0/C11 ratification; existing R01/Core/native authority
remains UNBOUND. Tests use the RC2 non-authoritative original-member double.

## Five-line policy

1. The existing instance config has one optional `ai_mode`: omitted=`hosted`, or `off`; existing RC2 raw decode, schema, hashes and owner checks admit it.
2. Hosted means exactly the existing admitted OpenRouter embedding tuple and HTTPS origin `https://openrouter.ai:443`, POST `/api/v1/embeddings`; no redirect, proxy/environment override or provider/model fallback.
3. Rerank defaults OFF for GET and POST; Stage 1 has no rerank binding, and an explicit request cannot invent one or cause hosted calls.
4. Accepted OFF stops new provider/resolver, cache/vector, AI queue/retry and rerank work, cancels in-flight work, and drops old-revision output; OFF requires no provider handle or embedding identity.
5. OFF serves bounded, currently authorized canonical keyword reads plus the existing separate graph read; unsupported content and incomplete coverage remain typed, and one absolute RC0 request deadline caps all work at 10 seconds.

## Admission through the existing path

Extend `runtime.config.admit_config`, not the CLI/parser surface. Read original
R01 lock first, use `runtime.contracts.decode` with the existing size/depth and
duplicate/type guards, then the one fixed closed schema and exact contract pins.
Keep all cross-binding, target, role, private-address, path, resolver owner,
operation and native/effect checks. No request/environment can enable hosted
against OFF. Installation OFF dominates any narrower project/request selection.

Only `ai_mode` is added to configuration. It is an exact string enum, no boolean,
null, alias, `local`, provider URL or unknown keys. The default is semantic;
admission must not mutate the original bytes before hashing. `AdmissionPlan.ai`
is immutable descriptive data: `mode`, `revision` (SHA-256 of exact config bytes),
`identity` (the existing EmbeddingIdentity or None OFF), `origin` (fixed above),
and `rerank_enabled=False`. This data never grants execution authority, just as
the existing AdmissionPlan and RC3a FactoryInterfaces do not. Keep RC3a's
`describe_factory` data-only; no provider/resolver invocation during description,
startup health, identity checks or enrollment.

Hosted/omitted config retains the complete existing BOUND embedding declaration
(provider, model, model_version, dimensions=768, preprocessing, original receipt)
and opaque API provider reference. Admit one owner-qualified current tuple,
without a catalog, hardcoded unqualified public model or alternate dimensions.
Before resolver/cache/HTTP work, require exact tuple equality, current project
and permission scope, and the existing data/spend purpose authority. Schema BOUND
or a fixture UUID is not that authority. An absent original binding still refuses.

OFF permits both `embedding` and `resolver.handles.provider` to be absent: no
dummy identity or credential is required. It also permits retaining existing
well-shaped BOUND metadata/opaque references, so changing only `ai_mode` turns
hosted off. Retained fields are never resolved or selected; `ai.identity` is
None and the provider handle is excluded from the admitted process secret view.
If present, fields still obey strict shape; an embedding GAP is not a fake OFF
placeholder. The three API Core/probe/observer handles and real resolver-owner binding
remain required; other process secret views do not expand. The existing extractor
identity names the local graph generation and is not an embedding model/key. OFF
does not weaken Core/C04 authentication, SQL identities, R01, native, TLS/HBA,
private listener, enrollment or other owner gates. Missing graph material remains
graph-unavailable rather than successful empty. No fake identity is needed to
construct the OFF search adapter.

## One revision fence and one lifecycle

E1 adds one internal `runtime.selection.SelectionGate`, consuming admitted AI
data under the existing owner-controlled lifecycle. Its constructor or `apply`
cannot mint owner/C04/effect authority. Real calls are made only by the admitted
RC3 execution closure; tests substitute synthetic ports. No public policy setter,
reload CLI, second config path or new credential storage is introduced.

`gate.snapshot` is an immutable current selection; `snapshot.revision` is a
non-reusable acceptance token. It must fence the admitted byte identity with a
fresh boot/acceptance epoch, or refuse a previously used revision. A config hash
alone is not sufficient: ON→OFF→the identical ON bytes must never revalidate old
work. Old durable work cannot carry an authority token across restarts; E3 must
re-read current owner/source/permission policy before any later producer dispatch.

`await gate.run(callback, revision=token, deadline=absolute_monotonic)` checks the
current mode/token/deadline before entering callback, owns cancellation, passes
the shortened absolute deadline to `await callback(deadline)`, and checks again
before returning a value. OFF refuses with existing `CapabilityUnavailable`,
`reason='hosted_disabled'`. Stale/expired calls refuse before any effect. Do not
create an alternate authority hierarchy or a second policy controller in a
provider, cache or worker: those consume the same current selection fence.

`await gate.apply(admitted_ai)` represents an already authorized accepted control
change. OFF becomes the dispatch barrier before acknowledgement. Cancel active
requests and fence their late results even if a subordinate suppresses
cancellation. Do not wait indefinitely for that subordinate to drain. Recheck
the same current token immediately before each cache publication, vector path,
provider dispatch, enqueue/retry and response; no late vector, cache entry or
success may escape under an earlier token. A warm semantic cache is bypassed
entirely OFF. Existing pre-OFF text cannot be recalled from a provider and may
still be processed there. Existing stored vectors/facts are retained; deletion
requires its own authority. Re-enable repeats full current admission and never
causes surprise bulk catch-up or model switching.

The existing HostedProvider gets the current selection through its `selection`
callback (e.g. `lambda: gate.snapshot`), matches the full identity before key use,
and keeps `embed(text, identity)` as the embedding port. Resolver use is through
the existing admitted opaque handle/purpose closure, never a raw env key, SDK or
new key store. The one API-lifetime httpx client must disable redirects and
`trust_env`; an injected permissive client must not let the adapter redirect.
Send only the admitted canonical text, model and dimensions. No record IDs,
paths, metadata, credentials, raw text or provider error bodies enter status/logs.
Keep existing response byte/vector/float32 checks and error scrubbing.

## Absolute request deadline

The RC3 HTTP lifecycle sets private scope `_cortex_deadline` once at earliest
request admission, using monotonic time and `runtime-operations.budgets.request_ms`
(10000). Incoming trusted deadlines can only shorten it; no caller can extend it.
Carry that SAME absolute value through auth, pool waits, D2 projection waits,
keyword scans, resolver, query cache/lease waits, transport and final checks.
`QueryEmbeddingCache.get(..., deadline=absolute)` must not reset it to the
standalone cache's 12-second default. The general cache default need not change;
the runtime call must clamp its remaining budget. Hosted's current 8-second
budget and connect/read/pool limits may only shorten remaining time. Cleanup and
cancel must not produce a successful response after expiration. Expired calls
do not start provider/cache/Core read work. Fixture/setup errors are not evidence
of a timeout assertion. No wall-clock sleeps or real providers are needed here.

## One canonical text projection and bounded keyword reads

E2's single codec is `embeddings.text.project_text(kind, payload: bytes) -> str`.
Use the existing strict raw JSON decoder, not a second general parser. Recognize
the released `memory` and `session` kinds only. Payloads are already current,
authorized Core canonical record bytes; the codec does not authenticate them.

* Memory: require string `section` and `content`; output `section + '\n' + content`.
  Exclude category, source and all metadata/IDs/paths.
* Session: preserve the current canonical message order; require canonical roles
  human/agent/system and string content; join `role + ': ' + content` with newline.
  Exclude agent/task/source/provider/path/timestamps and message/root metadata.
* Bound input bytes at 8 MiB and decoding depth at the existing 32-level limit;
  oversized bytes are `text_too_large`, malformed/deep input is `invalid_payload`.
  Preserve Unicode and line breaks, no implicit truncation or JSON stringification
  of numbers/binary. Nonempty projected text must fit the existing hosted query
  bound, 16 KiB UTF-8 including separators. This E0 choice makes oversized records
  explicitly unsupported; later chunking requires a separately versioned codec.
* Raise `UnsupportedText(code='unsupported_text', reason=...)`: `unknown_kind`
  for unrecognized/binary kinds; `invalid_payload` for invalid UTF-8/JSON, duplicate
  keys, bad shape/types/roles, empty text; `text_too_large` for overflow. Never
  silently skip, replace, return empty success or embed only a prefix.

Keyword and embedding consumers share this exact codec/preprocessing identity;
future rerank must use it too. E2 adds `embeddings.keyword.KeywordSearch` over
existing C04/Core **current read** ports, no new table/index, SQL role or writer:

```
KeywordSearch(read_current=async_read, authorize=async_auth, max_records=100)
await search.search(subject, query, limit=top_k, deadline=absolute)
await async_auth(subject)  # before read and again before returning
await async_read(subject, limit=max_records+1, deadline=absolute)
```

The read returns current authorized records with record_id, kind,
source_revision and payload. Casefold literal substring matching is sufficient
for this first bounded baseline; no regex, stemming, fuzzy service or external
index. Equal scores sort by record ID; top-k truncation alone does not imply an
incomplete scan. More than max_records makes `complete=False, reason='scan_limit'`.
Unsupported records produce typed `(record_id, reason)` entries and incomplete
coverage alongside supported hits, never a fabricated complete ready-empty.
Read authorization failure, deadline expiry or final permission revocation
suppresses results. These synthetic checks do not prove real C04/PG behavior.

## Existing read adapter and response gate

E1/E2 extend `C11b2ReadPort(..., selection=gate, keyword=keyword_port)`, preserving
its current required Core/project/fact-generation inputs. With OFF, identity and
provider may be None. Search chooses keyword **before** `_cache` or vector ports.
Hosted keeps the current semantic path under the same fence/deadline. Defaults
for both POST and GET rerank become false. `_current` remains mandatory before
return; no synthesized auth/policy dict can become a real connection grant.

OFF `/search` preserves existing C11 fields: `reranked:false`, typed
`degraded:['hosted_disabled']`, actual hits and truthful complete/state/freshness.
Do not invent distance/model-identity values for keyword hits. E0's synthetic
join freezes only IDs/degradation/no-cache behavior; **Cox must ratify** the exact
keyword score/result and unsupported/partial response mapping before E2 changes
the closed released C11 ABI. That is a named owner gate, not an implicit schema
exception. Explicit requested rerank remains typed unavailable; an intentionally
omitted/OFF reranker adds no warning. Keep separate `graph_search` and its actual
freshness/current-auth checks; no unified graph fusion route is claimed or added.

## Exact RC0 diff and remaining gates

`RC0-E0-001.patch` is the exact unapplied diff against base
482495bb52271c3030a8521b4118dd024dcdafca. It changes only the existing config
schema, operations declaration, and RC0 CONTRACT amendment. Lock schema, budgets,
native contracts, roles and endpoints other than the one embedding policy stay
unchanged. Scratch application/shape checks use the existing strict decoder and
Draft202012Validator; they are author tooling, never an alternate admission path.

The declaration changes require Cox ratification and a coordinated existing
operations.expected/paired fixture digest and original-member re-freeze plus
RC2's exact two byte pins. Those accepted files/pins are untouched in E0. The
patch alone is deliberately insufficient to make E1 GREEN; old pins must refuse
new bytes until the explicit ratified refresh. Do not admit multiple revisions,
relax hashing or repin from a candidate config at runtime. E1 then implements the
conditional handle view and data selection inside existing strict admission.

Tests-first commits precede this contract. The oracle map is the test classes:
AdmissionOracle (6), ExistingReadOracle (5), SelectionOracle (5), ProviderOracle
(3), CanonicalTextOracle (8), KeywordOracle (6), OffReadIntegrationOracle (1),
OffJoinOracle (3), OneSwitchOracle (1), CachePublicationOracle (1): 39 tests.
RED counts and source hashes live in the evidence
packet; an absent surface reports only that absence, not downstream behavior.
Imports/setup/fixture/effect failures are INCONCLUSIVE via shared test_receipts.

E1 still depends on RC3's admitted resolver/HTTP lifetime, current owner data/spend
scope and PG-A/B/C/D gates. E2 needs current C04/Core reads and Cox's C11 mapping.
E3 document production, E4 real-PG qualification, model switches, projection
mutation, job catch-up and **DEP-W** are excluded. DEP-W remains Cox's separate
purpose/credential/source-revision-bound projection writer work (sized 3 h);
no E0/E1/E2 keyword read requires a new writer identity or raw projection DML.

LEAN: reused strict RC2/R01 decode/admission, existing config and provider/httpx,
query cache, C04/Core reads, graph port and shared test_receipts; did NOT add a
config file/parser/CLI, SDK, model service, credential store or production source;
could merge provider/cache deadline plumbing into the one admitted RC3 lifetime
during E1 rather than building a second service factory.
