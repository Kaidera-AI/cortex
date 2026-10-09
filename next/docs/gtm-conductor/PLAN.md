# H-D455 C08 minimal Conductor execution plan

Nemo's dispatched third slice; accepted H-D455/H-D448/Design62; artifacts pre-approved. Mike reviews, Kai accepts. Base P01 PR31; one stacked PR contains only C08. No service installation, external restart-policy change, live monitoring registration or remote-write delivery.

## Files, order and proof

1. Commit plan and RED integration/unit tests.
2. `next/schema/coordination/c08-supervisor-leases.sql`: installation-scoped singleton lease, monotonically increasing fence, expiration with server clock and installation FK. This is separate from Cox's tenant/project job lease: the supervisor covers the installation, never invents a tenant or project. C03 owns core.installations and general job leases; its fresh source shape (id UUID) is the fixture seam. No job/outbox or Core record table changes.
3. `next/src/cortex_core/conductor/supervisor.py`: acquire/refuse a second holder, heartbeat and release; expired holder cannot renew, release or run a control intent. Guard PG-only control actions under a locked lease and check expiry again before commit. A stopped/missing supervisor reports supervisor_down; Core outage is explicit. Background heartbeat restart is external (systemd/Quadlet/Podman), never on the record/search data path.
4. `next/src/cortex_core/conductor/metrics.py`: Design59 v1.2 export only cortex_api_latency_seconds, cortex_search_latency_seconds, cortex_db_bytes, cortex_embed_backlog with deployment_id/environment/component/release labels. Latencies are last measured observations (gauges), no extra histogram labels or invented families. Core bytes are a real PG probe; backlog comes through Cox's authoritative observation port. Missing observations are omitted, never fabricated as healthy zero. Emit Prometheus text for the enrolled gateway agent; no new remote credential store/issuer or gateway mutation.
5. `next/tests/integration/test_conductor.py`, `run_conductor.py`, `mutate_conductor.py`: full search/P01 plus real disposable PG Conductor tests. Prove second refusal, expiry/reclaim fence increase, stale control rejection, expired-during-action rollback, release/renew refusal, heartbeat shutdown and canonical operations continuing without Conductor; health Core outage; actual bytes, bounded labels/families and timed metric observation. Semantic mutants for supervisor, metrics and SQL. Retain RED/GREEN outputs and source identity.
6. Fetch/merge current target tip, rerun and verify scope; push only Nemo branch, one PR and completion handback, remove parked worktree. Then graph routes.

## Boundaries and risks

The pool is a restricted installation-supervisor service role, never an end-user/client connection. C04 grants access to this private control table and C11 binds authenticated status/metrics routes; health stays available to report Core outage. No raw header authority. External effects cannot be fenced by a DB callback: this slice's guarded actions must be PG control-intent writes only; later executors independently enforce the fence. A long action rolls back when its lease expires. No HA/failover, add-on lifecycle switching, threshold policy or event relaying.

## Pre-edit evidence

Own SSD worktree nemo-gtm-conductor-20261009 based on P01 b54218c8; clean at creation. Fresh worktree/ref scan finds no Conductor paths in other task branches. Cox's C03 has coordination.leases scoped to tenant/project and core.installations(id UUID); those files are read-only/excluded. This new c08-supervisor-leases.sql avoids collisions. Current Design62 section4.3 and Design59 sections8.3/8.7/8.9 checked. H-D458 applies: only the shared kos-e020-uat engine, one stack per worker, each container <=1GiB/2 CPUs, explicit worker=nemo label, no prune/live-container action, cleanup each run. Existing synthetic PG runner adds only that owner label; no source mounts.

## Resume after search/provider review rework

Base restacked on published P01 d70618c (contains fixed PR29 ab84b7e); full stack
70 checks passes locally. Apply the team named-assertion mutation rule to C08, with
clean baseline, raw outputs, inconclusive failures and full restored suite.
Add a RED-first stalled-control regression: a callback cannot retain the singleton
row lock indefinitely after its lease expires. Bound the PG-only callback by the
remaining server-clock lease, return StaleLease and roll back on timeout. Pass the
captured token to the action. No external side effects are authorized by this port.
H-D460 reopens monitoring stack selection; this slice exports the existing closed
Design59 metric families only. It installs/selects no monitoring agent or backend.

## Vera C08-001 review rework

On the published P01 a5413024 stack, first retain synchronized RED regressions
for renewal and release queued behind a stalled guarded action. Observe both the
row-lock wait and a still-live lease before expiry; require StaleLease afterwards.
Lock the token-matching row first, then check a fresh server clock while holding
that lock. Preserve existing expiry, heartbeat, timeout and fencing controls.
Add a RED source-drift check to the proof driver. Bind every run to Git HEAD/tree
and SHA256 of every next/ source file; bind each mutation to its literal recipe,
original and mutant file digests, named assertion and raw output digest. Freeze
the source commit before the final clean baseline, mutants and restored suite.
Publish the updated PR33 and return the bound receipts to Kai for Vera's ruling;
then resume the graph plan. No live services or release integration changes.
