# PR #79 freshness repair (Kai H-D455, 20:24)

Accepted scope: repair the reviewed PR #79 branch before resuming C11b3.
Vera's exact-head two-path control is the RED. The gateway can see `ready`
before a Nemo read returns `lagging`, so every bound capability-gated read
must refuse the returned stale result with HTTP 503.

1. Freeze Vera's two-path control and add real-PG timing tests for both stats
   routes plus a route-table-enumerated invariant sweep. Commit RED.
2. Add one shared result-freshness check in `api/c11b2.py`; route search and
   every bound graph read through it. Leave unbound graph job route typed 503.
3. Add two mutations: bypass one stats handler check; make the sweep omit one
   route. Keep expected test-body failures and raw output.
4. Run the bounded PG suite, C11a/C11b regressions, exact-head qualifier;
   update draft PR #79 and return the new head to Kai. Then merge it forward
   into parked C11b3 and resume that task.

Risks: a stale success leaks an apparently complete empty response; the
source check must handle `lagging`, `rebuilding`, `disabled` and positive
`pending_records` without changing current response envelopes. No host API,
live provider, deployment or merge to main is in scope.
