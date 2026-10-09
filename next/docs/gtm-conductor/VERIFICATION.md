# C08 verification and binding contract

```text
restricted installation pool -> acquire singleton lease + monotonic fence
  -> renewal/release: lock matching token first -> fresh server-clock expiry check
  -> locked PG-only control intent, bounded by remaining lease, rollback on expiry
Core outage -> explicit health state
request/search timers + Core bytes/backlog -> four closed Design59 metric families
```

RED: 96f4e2a commits the initial contract before implementation (missing module).
d7547d5 adds the stalled-control regression before its timeout fix. Raw evidence
is in helix docs lane-F/gtm-conductor-evidence/red.log and stalled-control-red.log.

The original slice retained 71-check / 11-mutant proof under
lane-F/gtm-conductor-evidence/mutations/; subsequent parent restacks retained their
own receipts. Those logs do not qualify this rework's source tree.

C08-001 RED is committed at f43d697: both renewal and release queue on an observed
PG row-lock wait while the lease is still live, then incorrectly succeed after
the guarded action expires and rolls back. The source-drift receipt test also
fails on its expected assertion. Raw output: lane-F/pr33-c08-001-red.log (87 checks,
three assertion failures, cleanup PASS). After the lock-first and proof-driver
changes, the initial full run passes 87 checks, cleanup PASS:
lane-F/pr33-c08-001-green.log. The RED synchronization is retained in both tests.

Final acceptance requires a committed, clean tree: full baseline and restored
87-check suites, plus all 13 named source/SQL mutants killed only by their expected
single assertion (exit 1; no setup/import/runner ERROR; mandatory cleanup PASS).
The two new mutants restore pre-lock qualification separately for renew/release.
Run:

```sh
.venv/bin/python next/tests/integration/run_conductor.py
.venv/bin/python next/tests/integration/mutate_conductor.py /absolute/proof/directory
```

Each *.source.json binds pre/post Git HEAD, committed Git tree and a canonical
SHA256 manifest of all tracked/nonignored files under next/, plus raw-output
SHA256. Any source change during a child check is INCONCLUSIVE. Fresh Python cache
paths prevent stale bytecode from substituting earlier source. mutations.json
binds each literal mutation recipe, original/mutant file digests, entire mutant
manifest digest, expected test and attribution. The restored manifest must equal
baseline. Raw logs and these packets are retained in lane-F/pr33-c08-001-mutations/;
the handback binds the published head to these completed receipts.

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
