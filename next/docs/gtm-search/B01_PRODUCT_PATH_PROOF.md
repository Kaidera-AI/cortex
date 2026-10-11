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
`next/tests/module_consumer/test_manifest.py`. On the initial authorized main
base903fd729 that file is absent, so the result is BLOCKED, zero tests and zero
engine/resource access. The real-PG controller and nine component cases are
prepared, with the existing owned lifecycle, cached-image-only policy and fresh
host/engine35% admission. Those are the documented host memory fields in
[Podman info](https://docs.podman.io/en/latest/markdown/podman-info.1.html).
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
