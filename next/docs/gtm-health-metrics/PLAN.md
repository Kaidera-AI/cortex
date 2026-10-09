# H-D455 health and metrics gateway execution plan

Authority: Nemo DO NOW2026-10-10 00:38; artifact pre-approval from Cortex boot.
This is Nemo's next named GTM unit. Merge/review findings on PR31/33/36 take
priority. Work begins on published graph b78d383; refresh that target before
return. One source PR, no live service, collector or deployment changes.

```text
existing Core health + typed Core sample -> dedicated loopback ASGI surface
  GET /health -> minimal sanitized typed JSON
  GET /metrics -> existing C08 four-family Prometheus exporter
fake local scraper -> fake forwarder; real collector forwarding belongs to ren-tk
```

1. Commit this plan; commit fake-scraper contract tests and retain the actual RED.
2. Add next/src/cortex_core/gateway/health_metrics.py. Required Core health and
sample ports, existing Metrics object, no credential/config/env or outbound client.
Fresh typed sample updates DB bytes/backlog; no-op/invalid/error/timeout refuses
metrics rather than serving stale success. Health survives Core outage with an
explicit unavailable state; Conductor-down with Core-up is degraded, not a data
path outage. Only allowlisted health fields are serialized.
3. Both transport binding and peer address must be literal loopback; missing or
external peers are refused before callbacks. Forwarded headers grant no access.
Provide validated options for the injected existing ASGI server (proxy headers
disabled); test that binding call with a fake runner, never start a listener.
Revalidate bind settings at the actual server call. Check the deadline after
metric serialization as well as after each Core await.
4. A whole 250ms health/scrape budget is a bounded prototype default, not a measured
SLO; test synchronized shorter budgets, cancellation and scrubbed dependency errors.
Return no-store health JSON and Prometheus text using the existing four identity
labels/families from Design59 §8.3. Unobserved latency is omitted, not fabricated.
5. Add next/tests/integration/test_health_metrics.py and mutate_health_metrics.py;
reuse the ONE shared classifier and parent's bound driver. Fake ASGI scraper only
for this contract; prior stack regressions run on the existing disposable PG.
Retain full clean/restored suites, named body-assertion mutants, exact tested tree,
actual mutant/raw digests, independent audit and actual container cleanup.
6. Document the typed port/status/content contract in CONTRACT.md; fold source
docs/evidence to helix docs, publish one stacked PR, return, remove parked worktree.

C01 R001/R002 names /metrics and /health, but their general API legacy schema is
unfrozen. This dedicated minimal loopback surface is not a claim of C02 middleware
or payload parity. C02/C11 accept mounting, exact wire integration and the chosen
listener; C07 binds actual Core readers. Design63/H-D465 owns the collector/backend
architecture; Design59 supplies the existing allowlist. Cortex reads no monitoring
credential and sends no telemetry. No backend provisioning, enrollment, dashboards,
real collector scraping/forwarding, Linux release qualification or live deployment.

Files: this PLAN, CONTRACT.md, gateway/health_metrics.py, integration test and
mutation recipe module only. Parent source/recipes remain unchanged. Pre-edit
worktree is clean at b78d383; no gateway health module exists there.
