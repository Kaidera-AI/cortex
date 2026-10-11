# X02: external Conductor supervision and consumer isolation

Owner: nemo@helix. Stage: **PLAN ONLY, awaiting Kai acceptance**.
Authority: Nemo DO NOW, 2026-10-10 08:29 local; rebalanced from Bob.
Scope: one worker-day, about six productive hours. No code, source branch,
worktree, test stack or service installation before acceptance.

## Intent and source boundary

Prove that an external manager can observe and restart a dead Conductor, that
overlapping or stale Conductors cannot commit control intents, and that an
independent consumer and canonical fixture operations continue without the
Conductor. This is the small C08/PR39 proof unit dispatched today; expanded
add-on lifecycle and native release acceptance remain separate.

Inputs: [programme PLAN, X02 row](../../PLAN.md:345), Design62 sections 4.3 and
CXV2-002, and the merged C08 and health gateway. Fresh `origin/main` is
`3401d1a1b4be7a3a115a440d5415ea937ff8e003`. Its `Supervisor.run()` delegates
restart externally; `guarded()` permits PG-only control intents; `/health`
already distinguishes Core availability from `supervisor_down`.

Dependency: **merged C08 only**, building on merged PR39. No dependency on B02,
C06/C07, a future engine, a production dataset or a release install. Existing
C03/search fixtures provide synthetic records and restricted PG pools; this does
not ratify the pending production auth/outbox/client integration.

## Proposed topology

```text
test parent: external manager + health observer + independent consumer
       | owns/reaps at most two fixture child PIDs
       v
Conductor child A/B -> existing Supervisor -> PG lease / fenced control intent

independent record producer + consumer -> separate PG data pool / fixture sink
health observer -> existing TelemetryGateway -> separate read-only observation
```

The external manager is a **test fixture**, running real child processes. It
starts the published `Supervisor.run()` with a synthetic installation identity,
observes child exit, retries within a bounded restart policy, and reaps every
owned child. It is outside the Conductor and outside the record/consumer path.
PG control callbacks never execute process restart or another external effect.

The consumer reads a bounded synthetic record feed through its own PG pool and
commits its own sink/checkpoint. It has no Conductor reference or lease gate.
The parent retains an independent ID/content-digest ledger. Exercise normal,
shadow-apply and pre-checkpoint phases with deterministic barriers; these are
fixture lifecycle points, not an implemented engine rebuild or alias cutover.

Health runs in the surviving parent through fake ASGI transport and the existing
gateway, with a real PG lease reader. A killed process can leave a live lease
until expiry: external process observation must notice its exit immediately
within the fixture bound; `/health` must become degraded after lease expiry.
Do not falsely describe the lease receipt as instantaneous process liveness.
Core-down remains 503/unavailable; Conductor-down with available Core remains
200/degraded. Process restart never starts, stops or recreates Core.

## Files proposed for the eventual source PR

| Path | Change and purpose |
| --- | --- |
| `next/docs/x02-conductor-isolation/PLAN.md` | New; fold this accepted plan into the source tree. |
| `next/docs/x02-conductor-isolation/CONTRACT.md` | New; fixture topology, timing, restart/stop semantics and acceptance limits. |
| `next/tests/integration/x02_process_fixture.py` | New; bounded external manager, actual Conductor child, independent consumer and safe process receipts. |
| `next/tests/integration/test_conductor_isolation.py` | New; cross-process real-PG assertions, health and consumer isolation. |
| `next/tests/integration/mutate_conductor_isolation.py` | New; semantic recipes using the existing bound proof driver and shared test-body classifier. |

Permanent edits to Supervisor, gateway, existing tests, shared runners/classifier,
schemas, Core/API, packaging, service units and monitoring are excluded. If a
new test exposes a source defect, retain its RED and return an amended file/scope
plan before fixing it. Do not change another lane's active C06 tree.

## Order and fail-capable acceptance

1. After Kai accepts, refresh the target and repeat scope/ownership checks. Create
   one Nemo worktree under the root's `.worktrees/` convention, based on current
   main. Fold the plan; set up Python3.12 and the existing pinned
   `graph-requirements.txt`. No shared-checkout merge/reset.
2. Commit frozen new controls with a minimal selectable fixture stub. Capture
   genuine expected-body REDs for missing restart/isolation harness behaviour;
   existing C08 behaviour that already passes is recorded GREEN, not invented
   RED. Import/setup/runner failures do not count as RED or mutant kills.
3. Implement the fixture only; preserve the frozen controls. Use explicit child
   ready/dead/blocked barriers and PG server-clock expiry, not blind sleeps as
   proof that a race occurred. Keep test/cleanup deadlines and fail on timeout.
4. Freeze the complete committed source and run the full integration baseline,
   named semantic mutants and full restored suite. Independently reconcile
   exact HEAD/tree/all-next source digests, recipe bytes and raw output digests.
5. Merge the current target into the owned branch if needed, without rebasing a
   reviewed head; rerun final bound proof after any source change. Scope-check,
   push one PR, and return source-only receipts for independent review. Remove
   the worktree after safe handback/custody. Merge/release needs its later GO.

| Case | Required assertion / failure receipt |
| --- | --- |
| Hard process death | Kill only the manager's verified child PID with SIGKILL. External observer records that exact exit; manager creates a different child. Lease recovery yields a strictly higher fence. Core data remains available throughout. |
| Restart before expiry | A replacement cannot acquire while the old lease is live. Record LeaseBusy, then successful expiry/reclaim; no token reuse or silent second owner. |
| Overlap and stale control | Two actual Conductor children contend for one installation. Only one live lease; loser/stale token cannot commit a PG intent. Assert exact control-row fence and absence of stale rows. |
| Mid-control expiry | Pause a PG-only intent after its insert and across lease expiry. Require StaleLease and zero committed intent; replacement reclaims without an unbounded row lock. |
| Consumer independence | At each normal/shadow/pre-checkpoint barrier, kill or overlap the Conductor. Separate record writes/search and consumer progress still finish; reconcile exact IDs/digests and checkpoint monotonicity, with no missing or duplicate sink rows. |
| External health | Surviving parent observes exit; after PG expiry, gateway returns the exact degraded/Core-available receipt. A refused Core reader returns unavailable, never healthy. No status change gates the consumer. |
| Bounded restart and stop | Restart exhaustion yields an explicit safe failure; never a retry storm or Core action. Requested clean shutdown and test cancellation do not restart the child. |
| Cleanup / custody | Reap all manager-owned PIDs/tasks/pools, remove the one disposable PG and confirm empty Nemo inventory. Any remaining child/container or removal failure makes the run fail. Shared checkout HEAD/status must match the pre-run receipt. |

Fixture defaults proposed: lease 0.5 s, gateway budget 0.25 s; process-exit and
post-expiry health assertions bounded at 2 s; readiness/reclaim at 5 s; at most
three automatic restart attempts with bounded backoff; at most 16 synthetic
records per lifecycle case. These are test watchdogs, not product SLAs. Use
server-clock state and monotonic deadlines; preserve raw elapsed observations.

At least eight independently attributable recipes: disable restart, admit a
second live holder, reuse a fence, allow a stale intent, commit an expired
intent, introduce a Conductor gate into the consumer, map supervisor-down to
healthy/unavailable, and ignore restart exhaustion/clean stop. Distinguish
fixture-policy mutations from mutations of the existing C08/gateway source.
Temporary product mutations occur only in the owned disposable worktree and are
restored byte-for-byte; they are not permanent source changes.

Reuse `mutate_conductor.py` and `next/tests/test_receipts.py`; do not create a
second classifier. Before running any recipe, validate that its literal source
replacement occurs exactly once and its selected test exists and is accepted
by the existing selector. Class name `ConductorIsolationTests` and test method
names use the selector's letters/underscores rules, without digits.
Only the expected test-body `builtins.AssertionError` counts as KILLED.
Import/setup/teardown/runner failures are INCONCLUSIVE and keep acceptance RED.

Planned commands, **not yet run**, from the owned committed worktree:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python next/tests/integration/run_conductor.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python next/tests/integration/mutate_conductor_isolation.py /Volumes/WD-B-4TB/DevVault/helix/docs/plans/cortex-v2-v0.1.020/lane-F/x02-conductor-isolation/source-head-mutations
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /Volumes/WD-B-4TB/DevVault/helix/docs/plans/cortex-v2-v0.1.020/lane-F/structured-proof/audit_bound.py mutate_conductor_isolation /Volumes/WD-B-4TB/DevVault/helix/docs/plans/cortex-v2-v0.1.020/lane-F/x02-conductor-isolation/source-head-mutations
```

Receipts: frozen controls/RED, full clean and restored raw outputs and metadata,
per-mutant raw/source/recipe/phase identity, independent audit, actual process
exit/restart and consumer ledger, cleanup, source scope, exact-head CI and a
sealed artifact index. Test count is discovered and recorded, not guessed from
the current inherited 180. No skip, weakening or changing a frozen assertion.

## Resources, timing and gates

One shared v2 test stack team-wide, per H-D458/07:39 pacing; serialize with Cox's
existing C06 run through Kai. Reuse the bounded disposable PG runner: one PG,
1 GiB/2 CPUs, owner labels, tmpfs, no SSD bind mount, random loopback port. Never
touch live8501/5499/5500, production hosts, another worker's process/container,
Podman prune, sudo, launchd or a systemd service. Child environment carries only
the synthetic runner connection and required runtime paths, never ambient
provider credentials; DSN is never an argv value or retained output.

Sizing: 1 h contract/frozen controls, 2 h fixture, 2 h full/mutation proof,
1 h final scope/receipt/PR. Target: reviewable source proof for the Oct10 18:00
local cut; test-stack contention or a source defect is returned with evidence
for re-sizing rather than hidden. Kai names the independent reviewer.

Linux systemd/Quadlet and macOS Podman restart are the existing architecture's
native adapters. This fixture installs neither and proves no native policy or
signed package. Linux/macOS native, real listener/C11, full C07 consumer,
expanded future-engine rebuild/cutover, release signing and customer-publication
acceptance stay **NOT_RUN/open**. X02 fixture completion alone does not close
CXV2-002 or the entire expanded X02 gate.

## Pre-plan evidence

[Scope/source receipt](pre-plan-scope.json), SHA256
`b7859dcb89ea67743e245c8e600285c6e72b1682aa865a6251bb051da69afb73`:
109 refs checked; 62 no-merge-base legacy refs separately inspected and contain
none of the five proposed paths; zero overlap or unresolved ref. It retains
the initial comparison refusals, every direct inspection and source digests.
Shared main stays `8b16cc6ef9bd25449a5680de3c0874f3ebadbeb9`, with no tracked
changes and its existing untracked inventory preserved. No Nemo source worktree
or runtime was created for this PLAN.

Gate requested: Kai accepts this **bounded source-fixture plan** before BUILD.
If Kai requires actual native service-manager acceptance in this same slice,
return the target/access/resource prerequisites and re-size before execution.

## 2026-10-10 10:41 review amendment (Kai DO NOW, H-D455)

Fix X02-FIXTURE-001 to003 within the five accepted paths. Freeze Mike's three exact reviewer test bodies in the existing test file before implementation; retain original external probe byte-for-byte. Stop checks after publishing a replacement and reaps it before returning. Child exit80/fixture_error and unexpected exits remain FixtureError after reaping; declared kills/crashes/lease-busy stay distinguishable. Watch never restarts an unexpected fixture failure. Late heartbeat errors propagate after pool cleanup. Composed teardown attempts every manager, standalone child and PG fixture, then raises collected FixtureError; resource cleanup is registered before setup can fail. Keep original assertions and ten recipes; add literal recipes for each repaired concern, full clean/restored source binding and raw receipts. Recheck current main and >=35% free before each actual stack; no native/production gate changes. Mike reviews new exact head.

Verification amendment: retain first missing-Starlette selection as INCONCLUSIVE, then install existing graph-requirements pin; retain first extended RED's non-awaitable mock as INCONCLUSIVE, then correct only the AsyncMock setup. Original frozen assertions stay unchanged. The first full repair baseline failed because re-awaiting the expected initial LeaseBusy heartbeat overrode exit75; freeze a new assertion control before preserving that declared refusal while keeping post-ready runtime failures fatal. A Cases factory removes a duplicate unittest class alias without changing reviewer bodies. Rename only the new refusal selector to satisfy the existing letters-only runner grammar. Final eight lifecycle controls and18 literal recipes are proved at the new exact head; no initial failure is called a kill.
