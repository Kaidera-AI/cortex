# Bound runtime-spec preflight

`bound_spec_complete(spec, binding, published_operations)` accepts raw export
**bytes** and checks their hash, independent runtime identity and required metadata
before any HTTP. It has no network client or request generation. A positive unit
fixture earns `preflight_only`, with `mutating_requests=0`.

```sh
python -m unittest discover -s next/tests/live_contract/tests -v
python -m next.tests.live_contract.contract_gate SPEC.json BINDING.json OPERATIONS.json
```

BINDING has exactly `source_commit` (full 40-hex), `version`, `profile`,
`listener` (explicit http/https host and port), `spec_sha256` (raw-byte SHA256).
The export must carry the first four fields in `x-cortex-binding`, the same
info.version, and exactly that explicit server URL. No URL is inferred from a draft.

OPERATIONS is an independently published actual route map:
`{"POST /records":["read"],"GET /records/{id}":["delete"],"DELETE /records/{id}":[]}`.
Values name required standard OpenAPI Links; the real runtime exporter/owner must
publish its own route/link inventory. This example is a unit fixture, not a runtime
inventory or new authoritative product schema.

Required operations, typed request/response/error shapes, security schemes, path
parameters and link target IDs/required response properties must be concrete.
DRAFT/unfrozen/placeholder exports, missing shapes/auth/links, unresolved/external
refs, drifted commit/hash/version/profile/listener refuse as `spec_incomplete` or
`spec_binding_mismatch`. The existing C01 DRAFT is a rejection fixture.

This is a deliberately narrow OpenAPI 3.1 metadata guard, **not** a schema validator
or fuzzer. It rejects unsupported shape/link forms conservatively. Standard local
typed shapes/compositions are inspected; general schema semantics, recursive types,
non-body runtime expressions, optional/no-content responses, public unauthenticated
operations and generation need the existing schema library/Schemathesis integration
and reviewed policy in day 2. No provider is selected or called.

No installed export has been qualified. Generated coverage, authorized outcomes,
all-401 rejection, two-identity CRUD/foreign-ID isolation, original-ID recovery,
bounded HTTP cleanup and both native installations remain release obligations.
