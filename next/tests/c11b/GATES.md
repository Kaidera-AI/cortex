# C11b gates (PR #76 stacked base)

Accepted scope: Cox's C11/Core plan, Kai H-D455 19:27. All checks run against
this branch's final pushed head on disposable PostgreSQL; no live host.

- [ ] G1 Released input freeze: Cortex v0.1.002, the actually released KOS
  source/artifact, shipped cortex-* scripts and OpenKai 0.1.15 are each pinned
  with hash and exact request/response/error fixtures. A held candidate is not
  relabelled as a release.
- [ ] G2 The real ASGI API writes through C05/C06, then reads the exact bytes
  from Core on fresh authorized request, with unchanged released response
  shapes. A rollback has neither success response nor outbox effect.
- [ ] G3 Same logical request after lost response replays one committed receipt;
  a changed body under the same key conflicts. Headerless released clients have
  a server-derived idempotency policy with explicit limits.
- [ ] G4 Cross-tenant IDs, forged writer/scope and revoked grants are refused
  after authoritative Core recheck. No client-supplied tenant/principal grants
  authority.
- [ ] G5 Paging, cancellation, search capability/freshness, lag and Core-down
  cases return explicit bounded outcomes through the real API.
- [ ] G6 Mutation BODY faults in the new adapter and its private SQL are killed
  by the expected assertions; raw outputs and cleanup are retained.
- [ ] G7 Exact stacked head, resource limits, PostgreSQL version, command output,
  origin ref, PR and reviewer handoff are recorded. Main/adoption stays held.

Every open gate is named NOT_RUN or RED in the return. A smaller green subset
does not qualify all current clients.
