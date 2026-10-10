# C11B3-ISO-001 repair plan

Authority: Kai's 2026-10-10 21:22 DO NOW and Vera's PR #80 review.
Scope: PR #80's session API validator, its real-PG route regression, and the
named mutation runner. No SQL, manifest, provider, or installer changes.

1. Freeze a real `/sessions/ingest` regression for `2026-10-10Q20:14:00`:
   typed `400 invalid_input`, with unchanged C05 record/revision, C06 outbox,
   and installation-wide session-source counts. Keep valid `T...Z`, offset,
   and fractional forms. Run the native suite RED; commit the test alone.
2. Apply an explicit timestamp grammar (date, `T`, clock, optional fraction,
   optional `Z` or numeric offset), then parse to reject impossible dates.
   Make the Q case GREEN without modifying the frozen test.
3. Mutate away the grammar guard to restore fromisoformat-only validation.
   Require an expected test-body assertion; rerun the native suite and the
   timestamp, size, and source-claim mutants at the final head. Retain raw
   output, environment bounds, and cleanup receipt. Push the exact head.

Risk: an overly strict grammar could reject valid producer timestamps, while
an overly permissive parser persists malformed source data. Both sides have
native API controls. This draft source change is reversible; adoption of the
forward-only migrations remains held at the existing gate.
