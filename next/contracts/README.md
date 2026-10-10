# C01 I0 contracts

Authority: CTO H-D455, 2026-10-09; accepted C01/C02 definitions in the helix plan and design 62. This directory is the shared seam for `cortex_core`, engines and verification. It implements validation and fixtures, not an API server or database.

`openapi.json` and `route-matrix.md` preserve the accepted C01 draft exactly. Their 128 operations are inventory candidates with explicit response/auth/compatibility gaps. `provenance.json` binds their bytes and its versioned schema index selects the event and wire files. Event schema identity must match the index; unsupported index/event versions refuse. C02's legacy compatibility freeze still needs exact released Cortex/KOS/CLI pins; no live-server or released-consumer acceptance follows from these files. Released Cortex v0.1.002 is a partial launcher, **not a signed in-place upgrade origin**. The first eligible signed origin is v0.1.003 only after CTO signing; pre-signed/manual installations use backup, fresh install and restore.

`openkai-consumer-v1.json` freezes the separately defined **new** OpenKai 0.1.15 source inventory: 9 observed production method/path pairs, 6 SDK-only pairs kept separate, source/call-site hashes, C01 operation IDs and proposed scopes. The four `fixtures/openkai/` packets contain a proposed request/success/error exchange for every observed route, canonical write/read and retry meaning, distinct search states, and OpenKai-alone refusal/off/session/stale-recall behavior. `cortex_core.openkai_contract` validates these synthetic traces. It does not run the OpenKai product, the C11 API, a live host, or an installer. The authorized Core read, indexed-revision wait and standalone control operations remain named UNBOUND contract deltas; proposed partial HTTP mapping is not claimed as current C11 behavior.

`outbox-event.schema.json` is immutable event envelope v1. Event, installation, tenant, project, aggregate and payload IDs are canonical UUIDs; revision is positive and aggregate-local; delete implies tombstone and upsert excludes it. Payload reference/digest binds the exact immutable durable bytes. The occurrence timestamp is provenance, not ordering. A published feed cursor is separate, allocated only by C06's commit-safe publication protocol. Unknown schema versions refuse/quarantine through the consumer protocol; never skip them or infer a checkpoint from event IDs.

The small fixtures illustrate canonical commit with pending projection, ready capability, typed capability/Core refusal and upsert/delete. They are synthetic. `Proposed*` wire names retain their C01 status; new read-by-ID/revision-wait fields and exact HTTP mappings freeze in C02. GTM uses Postgres search and graph under H-D455; optional engines remain later modules and are not dependencies here.

Run the full unchanged contract suite in a disposable Podman container with copied inputs and no host mounts or published ports:

```text
python -m unittest discover -s next/tests/contract -v
python next/scripts/mutate_contracts.py
```

Test dependencies are pinned in `next/requirements-test.txt`. H-D450/H-D455 require RED before GREEN, mutation evidence and independent review. These checks do not prove storage atomicity, authorization, recovery or platform release qualification.
