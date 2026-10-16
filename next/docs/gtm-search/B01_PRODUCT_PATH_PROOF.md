# B01 product query fixture

This is source proof over synthetic, frozen float32 cosine inputs. It cannot
qualify a native baseline, an HTTP/API route, a real model/dataset, the complete
product stack, an engine choice or GTM. `product_path.receipt` keeps those fields
false and the engine decision `UNDECIDED`, including when PostgreSQL components
are exercised.

The input schema is `cortex.b01-product-input.v1`. The caller separately freezes
the SHA256 of sorted compact JSON (`allow_nan=False`) before querying. Required
fields are `identity`, `corpus_manifest_sha256`, `queries`, `splits`, `scope_map`,
`kind_map`, `record_map`, `source_revisions` and `source_sha256`. Identity includes
provider/model/version/dimensions/preprocessing. The corpus supplies its frozen
generation, metric and canonical stored float32 arrays. Queries use the existing
oracle fields plus ID, text, subject, cell, mode and limit. Tuning and held-out
IDs are disjoint, unique and exhaustive. Maps bind declared pseudonyms to actual
Core UUID scope pairs, product record IDs, kinds and source revisions; they are
not inferred from returned candidates.

Input admission checks every named corpus/dependency byte before product calls.
Synthetic corpus files are read once, SHA-checked against the admitted manifest
and staged privately through the existing corpus parser. Private arrays are
read-only. A source fixture is bounded to32MiB of array files. Each load client
admits and snapshots inputs before the measured phase; caller edits afterwards
cannot replace those validated bytes. Every request still invokes the supplied
current Core authorization port, the cache's own current authorization, actual
provider adapter on a cache miss and product search. Results must match the
frozen provider vector, embedding identity, scope/kind/source revision and full
independent oracle before receiving recall credit. Empty/under-k/tied results
retain the existing oracle's meaning.

Only scope/dense, cosine768, OpenRouter and the explicitly bound synthetic model
are supported. Coarse filters, other strata, sparse/hybrid, missing wire/model
contracts and incompatible inputs produce named unfulfilled results before
cache/provider/search effects. The load client turns any unfulfilled result into
a fixed exception so the existing offered-load runner charges an error/deadline
instead of favorable success latency. No direct SQL filter replaces search.

Run the unit adapter controls using the project's supported Python/dependency
pins and explicit source paths:

```sh
PYTHONPATH=next/src python next/tests/benchmarks/test_b01_product_path.py
PYTHONPATH=next/src python next/tests/benchmarks/mutate_b01_product_path.py <fresh-evidence-directory>
```

The unit cache/search ports are deliberately synthetic; the hosted adapter uses
real HTTPX parsing through `MockTransport` and a public synthetic resolver, with
no DNS/socket/paid request or credential discovery. The thirteen original unit
tests remain frozen. Literal faults run against temporary source copies with the
shared BODY classifier, effective import-path/SHA custody, zero skipped/errors,
and an unchanged full-suite restoration. Unit proof is not real-PG proof.

`native_suite()` defines separate real-component cases. It consumes the existing
Core fixture's `auth` context and accepts its Scope only after successful context
exit. Query-cache partitioning derives from the current complete Core
`cache_partition`; no `public.test_grants` or hardcoded permission counter is used.
Synthetic canonical records are fixture setup; projections are populated using
the actual PostgresSearch writer methods. Cache/search observations wrap and call
the actual methods, retaining real SQL and the hosted mocked-transport boundary.

The native command is deliberately fail-closed while its source/profile gates
are unmet:

```sh
PYTHONPATH=next/src python next/tests/benchmarks/mutate_b01_product_path.py <fresh-native-evidence-directory> --real-pg
```

It first requires the tracked, integrated
`next/tests/module_consumer/test_manifest.py`. The initial main903fd729 lacked
that file and refused before any resource access; Kai00:32 re-pins execution
to merged main9309ea77. The real-PG controller and nine component cases are
prepared, with the existing owned lifecycle, cached-image-only policy and fresh
host/engine35% admission repeated before each resource create and container start.
Only its already ownership-verified container name may be excluded from the
foreign inventory during that admission. The accepted existing fixture policy
uses host memory_pressure and VM /proc/meminfo MemAvailable/MemTotal, rather
than rejecting reclaimable cache through Podman memFree. The VM read is only
through the validated existing local rootless machine context, with raw public
memory observations retained; no remote host or new machine is selected.
The additional negative witnesses compare actual Core scope with an independently
frozen mapping, bind the real mocked provider/cache vector, reject an actual
projected kind/revision inconsistent with the frozen corpus, and charge warm Core-down
requests as offered-load errors. They do not replace Core grants or search results.
The bounded three-record fixture uses corpus integer kind0 mapped only to the
admitted `memory` string; it makes no mapping claim for the16-kind generator or
an unknown protected corpus. The C11 wire rule separately admits exact
`memory`/`session`, with omitted/all unrestricted; this fixture adds no wire parser.

After the full component baseline, eight native literal faults reuse the unit
source needles in temporary source copies. Each fresh child consumes one named
unchanged assertion in the same admitted owned PG fixture, with effective import
path/SHA and canonical BODY attribution. Operational errors/timeouts/skips and
survivors cannot earn kills. Original source/test bytes, the temporary source
restoration and a full component restoration are checked before cleanup. These
recipes and cases remain unexecuted while the integrated manifest is absent.
Actual component execution, native causal faults and inherited exact-head
manifest/cleanup receipts remain unfinished until accepted integration is
available. No file copy, skip, old-head result
or substituted catalog count can close that gate. The prepared native cases are
not credited as executed. Use the existing admitted owned lifecycle and resource
policy; no new allocator, image/dependency installation or ambient DSN fallback.

Vera independently reviews this authored proof. Kai credits the unit only after
the implementation, source/cleanup, actual component and inherited-gate receipts
are complete. Source preparation or a unit-test title supplies no such credit.


## S4b accepted source accounting plan (2026-10-16, a342de79)

Build on Nemo S4a08c75f18 only. CLOSED v2 is the accepted protocol; this slice
changes only the existing benchmark/load, B02 offer/report tests and mutator,
and this protocol. No product adapter, oracle, report implementation, Core,
cache, provider, native controller, CI or admission manifest changes.

1. Freeze17 deciding tests RED on Python3.12 and3.14 and commit before source.
2. Add optional async load `before_measured(clients, warmup_records, config)`
   after warmup workers drain, while eight clients are open and idle, before
   measured origin. Default is no-op. Pass a private warmup snapshot; exceptions
   and cancellation use the existing cleanup and prevent measured offers.
3. Add `benchmark.attach_product_truth(run, prepared)` after cleanup. Compare
   each retained OK response in both phases through the existing prepared.compare
   seam. Bind row measurement to query/input/truth/serialized-response digests.
   A refusal removes stale measurement and charges ERROR on that same offer;
   retain response and all scheduling/dispatch/start/completion/queue times,
   retain actual response elapsed time and apply existing deadline latency
   penalty. Preserve earlier ERROR/TIMEOUT/MISSED rows byte for byte. Never
   repair, renumber, omit or retime an invalid offer inventory.
4. Add bounded async `benchmark.run_product(...)` library orchestration only:
   validate config/budget, freeze input and full independent truth once before
   any client, share it across eight actual C11 clients, call S4a preparation
   in the phase hook, compare only after cleanup, reuse report.summarize.
   Explicit owner read_port/scope_for are mandatory; no port/resource discovery
   or alternate API/SQL/cache/provider implementation. Source diagnostics may
   run; they cannot qualify public API, native or GTM performance.
5. Current S4a preparation is a digest-bound NOT_RUN/UNAVAILABLE refusal, not
   a state receipt. Check its current input/truth/config/warmup digest and
   warmup-error count. READY/boolean, changed or reused evidence cannot admit
   a native cell. Return all four OS warm/cold x query prefilled/empty cells
   NOT_RUN, and sustained_all_miss NOT_RUN. Do not change cap, TTL or leases.
6. Extend the existing mutation runner/classifier to new source faults, retain
   inherited faults, exact executed-source/test hashes and full restoration.
   Both Python suites, canonical receipt classifier controls and source scope
   must pass before pushing only mike/qdrant-s4b-20261016. No merge.

Native measured-t0 protocol stays CLOSED v2: warm/prefilled fills owned keys,
then60s warmup and proves fresh entries/zero actual provider HTTP on hits;
warm/empty does60s warmup then owned emptying and proves absence. Both cold
arms finish truth/setup/opening and owned prefill or emptying before native
cold proof; cold warmup is0 and no warming operations intervene before t0.
Proof is per arm/repetition, never a digest or inherited warmup substitute.
Record measured hits/misses and minute windows. An unproved state is NOT_RUN;
Mac cold claims need the admitted exception. No current source fixture supplies
those proofs. Sustained all-miss requires Kai's capacity-safe schedule and full
transport-attempt/retry/failure budget; until then it stays NOT_RUN. Adapter
embed calls are not HTTP or paid calls.512 scoped occupancy and1800s TTL are
unchanged; distinct-key arithmetic is not capacity or paid-call authorization.

Implementation detail: the returned run retains `preparation_warmup_records`
as the exact timed snapshot bound by S4a preparation. `warmup_records` then
receives offline annotations and any comparison refusal charges, so preparation
cannot hide a later warmup comparison failure. Verify preparation hashes against
the retained pre-comparison snapshot, not the annotated rows. Original responses
carry actual cache-hit/provider-embed observations; report minute windows remain
the existing offered schedule. No embed observation is promoted to HTTP/budget
or state proof. The old SQL/proxy CLI remains its existing diagnostic.

The existing mutation runner now discovers and verifies the full copied Python
namespace package (including product_path) rather than its stale nine-module
list. The frozen full baseline/restoration is60 tests:13 load,27 report,20 offer
accounting, including all17 S4b controls; inherited23 recipes are retained and
15 S4b recipes are added. Copies contain only source modules; product fixtures
reuse unchanged pinned Core/contracts resources and deny IP sockets.
