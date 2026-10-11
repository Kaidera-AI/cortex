# Optional kind restriction on the PostgreSQL search port

Owner: Nemo. Accepted R5-PG-KIND-FILTER detail: ae7178e507934c7e66905ed13bd2fd034df3b9000f9bbb2f259286b41548d538; Mike PLAN_ONLY acceptance and Kai22:38 source/fixture go. This is an internal additive port; no HTTP type mapping or benchmark qualification is adopted here.

`PostgresSearch.search(subject, identity, vector, *, limit=20, kind=None)` retains the existing default behavior. `None` searches all eligible records in the authorized tenant/project. A supplied kind is an exact, printable, nonempty string of at most64 UTF-8 bytes without leading/trailing whitespace. Invalid types, empty/whitespace, control characters and oversized values raise ValueError before pool/database effects. Case and bytes are preserved; valid unmatched strings and an empty eligible source return successful empty hits. A string that resembles SQL remains a bound value, never query syntax.

The kind predicate applies in the same SQL WHERE as the existing tenant/project, configured embedding identity and current vector/source-revision predicates, before ordering and limit. A nearer wrong-kind row cannot consume the requested limit. Kind does not grant access or provide a client-controlled scope. Whole-authorized-scope freshness remains unchanged, so a restriction cannot hide pending sources. The current bounded HNSW strict-order iterative-scan settings are unchanged. Tests include an explicit real-index fixture; one forced-plan query proves that path only, not complete ANN recall under real data/load.

## Integration owners and mapping freeze

Cox owns the authenticated C11 parser and wire adoption. Before it adopts this seam, Cox publishes the actual approved legacy type-to-Core-kind table. Proposed `all`/omitted -> None and registered type -> exact canonical kind is not a guessed table or an API implementation. Unknown/unbound types and unsupported fields must produce the existing explicit unsupported/unavailable result before cache/provider/search effects. They must never become None as fallback. POST /memory-search needs the same explicit supported mapping if adopted. Nemo does not edit C11/auth.

Mike owns B01's product-path load/query/oracle fixture. Before adoption, its integer-kind -> admitted product string mapping is frozen independently of returned hits, along with scope/identity/source/generation/input/query/oracle hashes. Source scope+kind support does not imply time/ordinal/sparse/hybrid/room/hall support or fulfillment of all six benchmark strata. Unsupported rows receive no favorable latency/recall credit. Existing geometry's unknown model/tenant/deletion facts remain unknown.

## Verification and limits

Original a3f0a409 test bytes are unchanged. Offline controls cover the optional API, malformed-before-effects behavior, literal bound values, scope/limit positions and default/freshness compatibility. Actual PostgreSQL tests cover before-top-k, unmatched/SQL-like values, empty source, whole-scope pending counts, tenant/project/current-revision exclusion and default/None behavior. A separate companion captures EXPLAIN ANALYZE of the actual product query under an explicit forced-HNSW fixture. These use synthetic data/authority in a fresh admitted owned fixture, not C04 security acceptance or native platform/real-data/API/GTM qualification.

Run the repository tests through the existing product DisposablePostgres lifecycle under its accepted cached-image/mutex/pending-marker/35% Mac+VM/1GiB/2CPU/generated-credential/identity-cleanup policy; no replacement allocator or paid provider. Literal faults must fail the exact intended test assertion, with setup/import errors reported separately and exact source/test restoration.

The mandatory exact-head real-PostgreSQL module_consumer/test_manifest.py arrives with PR71. Its absence on main903fd729 is an explicit pending integration gate. Source review can proceed; unit credit requires PR71 merged, current-tip integration and the inherited exact-head rerun plus pytest-pg GREEN. No author merge/adoption or inferred release performance.
