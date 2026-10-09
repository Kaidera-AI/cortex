# C08 verification and binding contract

```text
restricted installation pool -> acquire singleton lease + monotonic fence
  -> heartbeat/release
  -> locked PG-only control intent, bounded by remaining lease, rollback on expiry
Core outage -> explicit health state
request/search timers + Core bytes/backlog -> four closed Design59 metric families
```

RED: 96f4e2a commits the initial contract before implementation (missing module).
d7547d5 adds the stalled-control regression before its timeout fix. Raw evidence
is in helix docs lane-F/gtm-conductor-evidence/red.log and stalled-control-red.log.

GREEN: 71 full-stack checks; 11 source/SQL mutants fail their expected named
assertion, admitted by a clean baseline and followed by a restored full suite.
Raw baseline, per-mutant logs and target/status JSON are under
lane-F/gtm-conductor-evidence/mutations/. Run:

```sh
.venv/bin/python next/tests/integration/run_conductor.py
.venv/bin/python next/tests/integration/mutate_conductor.py .worktrees/nemo-c08-mutation-proof
```

Use conductor-requirements.txt with Python3.12. The runner uses one disposable
PG18.4/pgvector0.8.2 container, <=1GiB/2CPUs, worker label nemo, tmpfs only, random
loopback port and mandatory cleanup. Host tests ran on macOS arm64; this does not
qualify Linux x86_64 release artifacts or production workload acceptance.

Cox C03 supplies core.installations(id UUID) and registers this distinct migration;
C04 provisions a private non-bypass installation-supervisor role and binds its
installation, never user-supplied headers. Search runtime is denied this table.
C07/C11 integrate canonical control-intent writers and authenticated status/metrics
wire envelopes. guarded accepts cancellable PG-only callbacks; external effects
must independently enforce fences. Its rollback/timeout is not an external API fence.
General coordination.leases/jobs/outbox remain Cox-owned and unchanged.

No data-path relay, threshold policy, HA, new live process or service-manager action.
Status is healthy only while a lease is live; missing/expired supervisor is down;
Core outage is core_unavailable. DB bytes are observed from Postgres; backlog is an
injected authoritative Core sampler. Unobserved metrics are omitted. API/search
latencies are last measured gauges, with exactly deployment_id/environment/component/
release labels. H-D460 monitoring-stack selection stays with the CTO/Kai: this code
selects/installs no backend, agent, issuer or support mesh. C11/monitoring owners bind
the export to the authorized gateway and scoped credential seam.

Door: two-way for this undeployed source slice. Blast radius: control. Removing the
control table after deployment requires an explicit migration/data decision.
