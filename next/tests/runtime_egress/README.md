# E0 tests first

These tests freeze E1/E2 expectations before product source. They do not qualify
R01 signatures, native images, C04/PG authorization, provider service behavior or
deployment. All successful admission uses the existing non-authoritative RC2
double. Synthetic method seams do not grant authority or prove a service join.

Run from this worktree with the existing test dependencies (including pinned
psycopg 3.2.9 from next/requirements-db-test.txt), without a database:

```
PYTHONPATH=next/src python3 next/tests/runtime_egress/run_oracle.py
```

The shared `next/tests/test_receipts.py` owns assertion/fixture/error attribution.
`run_oracle.py` adds effect guards and loaded-file hashes only. Baseline exit 1 is
expected RED, never a GREEN implementation claim. Partition its body assertion
receipts by exception type: `SurfaceAbsent` means the named future interface is
absent; other assertion failures are semantic RED. Any import, setup, runner or
test error, fixture-phase failure, or effect attempt makes the run inconclusive.
No `expectedFailure`, skip, xfail or missing-import-as-success convention is used.

The exact future internal callable contracts are documented in E0 POLICY.md.
All earlier tests remain byte frozen. E1/E2 must make these oracles GREEN along
with inherited controls; independent real-PG and provider qualification are later
gates, not replaced by these synthetic checks.
