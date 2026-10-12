# C11b sub-slice 2 acceptance gates

Authority: Kai 2026-10-10 19:50 DO NOW, stacked on draft PR #77 exact head.
No host, no live :8501/:5500, disposable bounded PG only.

- [x] Tokenless released clients get HTTP 401 `credential_required`, non-retryable,
  with an upgrade message; no X-Agent/loopback trust. Revoked credentials stay
  forbidden and Core-down still takes precedence.
- [x] The signed KOS v0.2.009 release manifest verifies; exact Linux arm64 and
  x86_64 artifacts and the CLI/client members inside them are frozen by hash.
- [x] Real PG search calls Nemo's merged PostgresSearch port, not a fabricated
  empty result. `results` and `degraded` envelopes match released consumers;
  model identity, generation, freshness and C04 recheck guard `ready`.
- [x] Real PG graph read calls Nemo's merged PostgresGraph port with bounded selectors
  and scoped read; unavailable/lagging never becomes an empty success.
- [x] Session route, paging and cancellation have fail-capable real-API fixtures.
  A cancelled request cannot emit success after stale scope or deadline.
- [x] New source/SQL faults are killed by expected test-body assertions. Raw
  logs, exact head, resource limits and cleanup are preserved.

An unchecked item remains RED or NOT_RUN in the return, never implicit acceptance.

Sub-slice boundary: graph extract/build/prune/job routes still refuse as unbound;
query embeddings are an injected port (the PG fixture uses a synthetic vector);
session data is one C05 record rather than the released four normalized tables.
These require further binding/qualification before whole-C11b acceptance.
