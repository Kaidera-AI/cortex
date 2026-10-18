# Core API contract provenance

Source reused: Kaidera-AI/cortex, MIT, base
76361ab8580a5032fb85fafe0b24bf7674b5147a. Preserve the repository LICENSE.
ConsumerGateway/load_routes, C11b adapters, release.codec.canonical_bytes and
the existing HTTP response JSON encoding are reused without a fork.

Owned gap: plain OpenAPI descriptors and an explicit identity/export join for
the actual fixed Core dispatcher. No external code was downloaded or copied.
Schema syntax checking reuses jsonschema4.25.1 already in the existing closure
(upstream https://github.com/python-jsonschema/jsonschema/tree/v4.25.1,
MIT; preserve distribution notices). No new parser/framework/runtime dependency
or package installation path is introduced. Source-test environments and their
compiled platform dependencies do not qualify the installed release closure.
