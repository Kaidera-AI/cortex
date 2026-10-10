# X02 bounded source-fixture contract

Kai accepted the PLAN SHA256 f7487733cf4ccb0145fb40833aada5b16b842744ec55a04ce421193060f0c3ca
in Nemo DO NOW at 2026-10-10 08:42 local. Mike reviews. The plan's draft status is
its historical proposal state; this external acceptance precedes BUILD.

Only disposable synthetic PG, child processes owned by the test and fake ASGI
transport are used. No product process entrypoint, native service unit, listener,
Core schema/record adapter, production consumer or monitoring deployment changes.

`Child` uses the published Supervisor with either its real heartbeat loop or a
manual-heartbeat fault mode to expose expiry and stale captured tokens. Child
requests admit only bounded PG intent writes. External process effects are
owned by `Manager`, never by the fenced DB callback. Safe receipts carry kind,
PID, fence and exit code; no DSN, environment or exception text is emitted.

`Manager` owns at most one running child at a time; overlap tests separately own
a second child. It records exits, reaps each generation, makes at most three
restart attempts after the initial start, and backs off within the fixture
deadline. Explicit shutdown is terminal. Exhaustion is an explicit failure, not
a healthy observation or a request to restart Core.

`Consumer` takes only a separate data pool. Its synthetic feed/sink/checkpoint
has normal, selected-batch shadow and pre-checkpoint barriers. All barriers follow
batch selection, so records produced while paused belong to the next batch.
Revision/vector writes and search use the existing restricted retrieval fixture.
An independent parent ledger reconciles IDs and synthetic payload digests (the
payload is the record ID, not a Core body). Feed insertion is a test witness, not the C06
transactional outbox or C07 production consumer contract.

The observer survives a killed Conductor and reads the published gateway over
fake ASGI with real PG lease state. Process exit and lease expiry are different
observations. Available Core plus expired/missing lease is 200/degraded;
refused Core observation is 503/unavailable. Native service-manager policies,
signed platforms, real listener/C11 and expanded X02/CXV2-002 remain open.

Every stack starts only after `memory_pressure` reports at least 30% free and
the single team-wide v2 test slot is clear. Keep resource/cleanup observations
with the exact-head proof. The frozen controls begin with expected-body REDs;
final acceptance requires clean and restored full suites, named semantic kills
and an independent source/recipe/raw-output audit. Runtime or cleanup errors
are inconclusive. The source-fixture restart policy is independently mutated;
it proves no native service-manager policy.

## Reviewer lifecycle repair controls (2026-10-10)

Stop during replacement readiness reaps the published replacement before returning. Only declared normal stop, deliberate crash/lease-busy and an intentional fixture kill are admitted exit classes; runtime exit80 and all unexpected exits raise FixtureError after reaping, so they cannot count as semantic kills or successful restarts. A late heartbeat failure survives pool cleanup. Composed setup registers owned cleanup before any await; teardown attempts all managers, standalone children and the PG fixture, falls back to all acquired pools/admin on partial or failed fixture teardown, then reports FixtureError. Mike's three frozen reviewer bodies and four additional fault controls precede these changes; all original assertion bodies and ten literal recipes stay unchanged. Every author stack now requires >=35% free memory and an empty team test inventory. No native qualification changes.
