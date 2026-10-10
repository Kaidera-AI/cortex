# C11b sub-slice 3 acceptance gates

Authority: Kai 2026-10-10 20:14 DO NOW. Stack on PR #79 exact head.
RED first; one feature branch/PR; no live host or provider call. Disposable
PG18+pgvector, 2 CPU/1 GiB/no network, 35% free-memory gates and full cleanup.

- [x] Query vectors come from P01's provider `embed` port through its bounded
  query cache; a fake provider in tests records the complete model identity.
  Unavailable, malformed or timed-out providers yield a typed unavailable
  refusal and no `results` success, including after cancellation/revocation.
- [x] Session source paths and UUIDs cannot cross a project boundary; the
  claim is atomic with the C05 record and C06 event, including replay/races.
- [x] Session timestamps accept valid ISO-8601 and reject invalid values;
  bounded requests above 1 MiB but below the new limit succeed, oversized
  requests fail without a write.
- [ ] Nemo search, query-cache and graph SQL enter the Core forward-only
  migration manifest in dependency order, with exact hashes and ledger proof;
  required RLS/pgvector tables exist after one fresh install. Nemo reviews
  the manifest entries.
- [x] Graph mutations and search graph fusion remain explicitly unavailable;
  asyncpg packaging is assigned to the installer owner, not this slice.
- [ ] Regressions pass and source/SQL mutants die by expected test-body
  assertions. Exact-head raw evidence and cleanup are retained.

Nemo found RM-001 in the first review: auth-0002 predates these tables, and the
fixture's broad grants masked the absence. A forward-only retrieval authority
migration and non-owner/forged-scope tests now cover the repair; Nemo's review
of the new exact head remains pending. The exact-head evidence gate closes
after commit and qualification.
