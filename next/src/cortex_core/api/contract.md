# Bound Core contract export

`runtime.services.bind_core_services(resources)` enriches the actual
ConsumerGateway dispatch rows from the existing `load_routes` census. Each row
owns its OpenAPI operation alongside the dispatch identity, method, path and
effect. There is one listener, one schema descriptor set and one existing HTTP
JSON codec. `BoundRegistry` refuses an unknown callback, missing referenced
schema, missing required response/input or a later registry/handler/schema drift.
It does not add or implement the proposed A daily routes.

After actual readiness, the host may call
`services.export_bound_openapi(source_commit=..., version=..., profile='A',
listener=...)`. All four inputs are explicit. Source commit is a full nonzero
lowercase Git SHA, version is numeric semver, and listener must exactly match the
admitted publish address. This validates shape and binding, not provenance:
the host owns the release receipt. No Git lookup, environment/config fallback
or HTTP identity parameter is used. A second different identity refuses.

The return bytes equal authenticated `GET /openapi.json` bytes using the same
existing response codec. Before that owner call the endpoint returns typed503
`contract_identity_unbound`. Credential, configured-project selection, duplicate
header admission and the shared request deadline remain those of the existing
C11 gateway. Export does not perform a provider call, SQL mutation, enrollment,
package install or listener startup. Its startup schema checks reuse the stock
jsonschema implementation already in the packaged closure.

OpenAPI 3.1 contains `x-cortex-binding` (source_commit, version, profile, listener)
and `x-cortex-contract-sha256`. The latter is SHA256 of the existing canonical
codec's document bytes with that digest field excluded. The SHA256 of the
returned wire bytes is a separate raw-spec receipt. The wire codec's encoding
can differ from canonical bytes for Unicode; do not substitute the two hashes.

Bound success operations are health, memory write, session ingest, session IDs,
record read and this contract read. Health is public. Protected operations use
Bearer and configured X-Project; session IDs also accepts the existing project
query alternative. Inherited unbound operations advertise typed errors and no
2xx. Retired SQL routes retain their existing public410 behavior. An ingest
read Link uses `receipt.record_id`, because `session_id` is not the record ID.
Memory uses the returned `id`. No delete Link is fabricated before that handler
exists. Ingest retains its existing8MiB bound; memory retains1MiB. One current
request budget remains at most10s.

Limits: the operator has no trusted release-identity composition join. Unit
resource doubles qualify source wiring only, never live/installed SQL or
paired-platform proof. The current independent bound-spec guard conservatively
refuses public/refusal-only operations and nested receipt Link expressions;
that gate stays OPEN. Its frozen semantics and LINK001 are not changed here.

LEAN: reused load_routes, ConsumerGateway, C11b, the canonical codec and the HTTP
codec; added no framework/parser/server/config path; could merge descriptor
construction with the eventual fixed route registration.
