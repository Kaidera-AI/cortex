# Option A runtime owner

The public contract is [contract.md](contract.md). Run the foundation units with
`PYTHONPATH=next/src python3 -m unittest discover -s next/src/cortex_core/runtime/tests -v`.
Readiness units live in `next/src/cortex_core/schema/tests`.

One vault config is read once for an explicit purpose. The frozen result exposes
only its admitted DB role references, or the client reference in client mode.
The file resolver walks directory file descriptors without following links,
checks the opened regular inode's owner/mode/link count/size, reads with a byte
bound and checks that the file did not change while read. It never logs bytes.

The host decoder runs on stdlib-only Python (`-S` is exercised by the units).
Schema consumers still load the existing pinned jsonschema implementation.
Day 2 adds one encrypted custody adapter, fixed synchronous PostgreSQL resource
owners and the actual operator entry `python -m cortex_core.runtime.operator`.
The finite commands are `migrate` and `seed`; `config client` writes the existing
private projection to an explicitly supplied descriptor. `serve` starts one
stock Uvicorn/Starlette listener, and `status --config FILE` reads that listener's
public health using HTTPX without custody or database access. Effectful commands
require explicit `--unlock-fd` and `--key-id`; config admission and status do not.
No master-key generation, persistent key store or implicit unlock is provided.

All commands use the same strict config snapshot and existing JSON decoder.
Core owns connection, transaction, request and shutdown lifetimes; the fixed
`cortex_ship.install.roles` package owns catalog policy and Finn's schema module
owns seed. Missing source or failed readiness refuses. Shutdown stops admission
at the first signal and shares one absolute budget across startup, active SQL,
drain and reverse cleanup; unresolved cleanup is failure. Status emits the
existing `cortex.status.v1` JSON and returns ready=0, enrollment_required=1,
unavailable/invalid=2. It never treats a ready fixture as installation proof.

The currently bound API is the existing C11b memory write, session ingest,
session-ID read, record read and public health gateway. Its single actual
registry now carries plain schema/auth/error/Link descriptors. The owner-only
`export_bound_openapi` service port takes explicit source commit, version,
profile A and the configured listener after readiness. It returns the exact
bytes served by authenticated `GET /openapi.json`; the endpoint refuses with
`contract_identity_unbound` until that port is called. The operator has no
trusted release-identity join yet, so this does not claim automatic installed
export. See [../api/contract.md](../api/contract.md) for identity and digest rules.
Daily-command and search handlers remain unbound typed refusals, including
inherited `/log` and `/metrics`; none advertises a success response.
Stock runtime dependencies and licenses
are recorded in [UPSTREAM.md](UPSTREAM.md). Unit/driver-policy fixtures and
loopback/signal process receipts qualify source behavior only. Actual full-schema
SQL, least-privilege grants, encrypted descriptor mounts, installed closure,
paired-platform proof, independent review and release remain separate gates.

Kai's bounded P1 plan (2026-10-18, row
`c03024e3-1d32-46c2-8c6a-6c861a03fe57`) changes only lifecycle carrier
publication and its regression here: reconcile a shutdown received during
ExitStack transfer before binding/starting services, reuse the first absolute
cap, and preserve unresolved cleanup failure. The unchanged test
`test_sigterm_at_carrier_transfer_stops_next_sql_under_first_cap` must fail
against the prior source before the fix, then pass for normal and blocked
cleanup on both supported Python versions. J06 and all P2 work stay frozen;
Hale alone reviews/closes this finding at the exact pushed correction head.
