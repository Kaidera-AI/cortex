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
PID, fence and holder; no DSN, environment or exception text is emitted.

`Manager` owns at most one running child at a time; overlap tests separately own
a second child. It records exits, reaps each generation, makes at most three
restart attempts after the initial start, and backs off within the fixture
deadline. Explicit shutdown is terminal. Exhaustion is an explicit failure, not
a healthy observation or a request to restart Core.

`Consumer` takes only a separate data pool. Its synthetic feed/sink/checkpoint
has normal, in-memory shadow and pre-checkpoint barriers. Actual record writes
and search use the existing restricted PG fixture. An independent parent ledger
reconciles IDs/content digests. Feed insertion is a test witness, not the C06
transactional outbox or C07 production consumer contract.

The observer survives a killed Conductor and reads the published gateway over
fake ASGI with real PG lease state. Process exit and lease expiry are different
observations. Available Core plus expired/missing lease is 200/degraded;
refused Core observation is 503/unavailable. Native service-manager policies,
signed platforms, real listener/C11 and expanded X02/CXV2-002 remain open.

Every stack starts only after `memory_pressure` reports at least 30% free and
the single team-wide v2 test slot is clear. Keep resource/cleanup observations
with the exact-head proof. All new tests and receipts are initially NOT_RUN.
