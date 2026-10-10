# C02 D1–D4 fixture repair plan

Authority: Kai's 2026-10-10 21:30 and 21:33 rulings. Stack PR #84 on the
accepted PR #80 head `cee438f`; no OpenKai product or runtime route changes.

1. Forward merge PR #80. Freeze RED assertions in the existing C02 contract
   tests for D1 authorized record read/no-leak/tombstone/body digest; D2 search
   `min_revision` and bounded `wait_ms` with pending deadline; D3 READ status
   and typed-unbound control/jobs; D4 503 typed lag/partial and 200 ready-empty.
2. Rebind packet and fixtures with `ruled_unimplemented` where the C11 runtime
   does not yet implement the ruling. Keep the 9 observed route pairs and 6
   SDK-only pairs pinned to released OpenKai source. Do not add D1/D2 to C01
   OpenAPI or pretend the current consumer executes these exchanges.
3. Extend the source-only validator so malformed rulings, leaking 403s,
   unbounded waits, 206 partials, zero-hit lag responses or owner-level
   status requirements are refused. Run the full 39-test contract suite and
   8 named mutants in the bounded offline container at the final head.

Risk: a fixture could overclaim native implementation or normalize a missing
projection to empty search. All ruled-unimplemented states remain explicit;
native D1/D2 implementation is a later C11 slice.
