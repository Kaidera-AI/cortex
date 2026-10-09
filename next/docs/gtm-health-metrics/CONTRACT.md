# Dedicated local health and metrics contract

This prototype supplies a minimal loopback ASGI surface for the Kaidera collector
to pull. Cortex contains no monitoring credential, outbound telemetry client or
backend selection. The fake scraper transports requests in process; its fake
forwarder stores strings only. No listener or live collector is started here.

```text
Core health reader -> GET /health -> allowlisted JSON
Core health + sample readers -> C08 Metrics -> GET /metrics -> Prometheus text
collector pulls locally -> collector owns authenticated forwarding
```

`TelemetryGateway` requires the existing C08 `Metrics` instance and two async
readers. `read_health()` supplies a dictionary with a strict boolean
`core_available` and `state` equal to `healthy`, `supervisor_down` or
`core_unavailable`. The boolean must agree with the state. Extra fields are never
serialized. `read_sample()` supplies exactly `CoreSample(db_bytes, embed_backlog)`;
both values must be nonboolean integers in the existing Metrics range, 0 inclusive
through 2^63 exclusive. An invalid/no-op/erroring reader refuses the scrape and never
exports the previous Core values as a successful fresh sample.

| Request/observation | HTTP | Response |
| --- | --- | --- |
| GET /health; Core up, Conductor healthy | 200 | JSON status `ok` |
| GET /health; Core up, Conductor down | 200 | JSON status `degraded` |
| GET /health; Core down or invalid/erroring reader | 503 | JSON status `unavailable`, reason `core_unavailable` |
| GET /health; budget exceeded | 503 | JSON status `unavailable`, reason `timeout` |
| GET /metrics; valid fresh health and sample | 200 | `text/plain; version=0.0.4`, existing C08 export |
| GET /metrics; Core down | 503 | Typed metrics refusal, reason `core_unavailable`; sampler not invoked |
| GET /metrics; invalid/erroring health or sample | 503 | Typed metrics refusal, reason `sample_unavailable` |
| GET /metrics; budget exceeded | 503 | Typed metrics refusal, reason `timeout` |
| Either request; external, named or missing HTTP peer | 403 | `permission_denied` / `loopback_required`, before readers |
| POST to either path | 405 | No reader invocation |

Health JSON has exactly `component: "cortex"`, `status`, boolean `core_available`,
`conductor` and `reason`. Unavailable error responses use
`conductor: "core_unavailable"` and `core_available: false`. Healthy/degraded
responses have a null reason. Metrics refusal JSON is
`{"error":{"code":"capability_unavailable","capability":"metrics","reason":...}}`.
Dependency exception text, credentials and supervisor fencing data are omitted.
Successful health/metrics and explicit refusal responses set `Cache-Control:
no-store`. Caller cancellation propagates and cancels an awaited reader.

The four metric families remain `cortex_api_latency_seconds`,
`cortex_search_latency_seconds`, `cortex_db_bytes` and `cortex_embed_backlog`.
Labels remain exactly `deployment_id`, `environment`, `component`, `release` as
specified by Design59 §8.3. API/search latency values are existing observed
seconds; an unobserved latency family is omitted. DB bytes/backlog come from the
fresh supplied Core sample. No tenant, project, record, provider or secret label
is introduced.

Construction and `serve(runner)` validate a literal loopback IP and a nonboolean
integer port from 0 through 65535. IPv4 loopback and `::1` are supported; DNS names
and wildcard binds are refused. The injected existing server receives
`proxy_headers=False, forwarded_allow_ips=""`. HTTP access also checks the actual
ASGI peer, ignoring forwarded/Host headers. This contract requires the integration
owner to preserve that binding and peer provenance when mounting or serving it.

The default whole-request budget is 250ms, configurable above zero through two
seconds. This is a prototype hypothesis, not a measured production SLO. It covers
both health/sample awaits and metric serialization, with monotonic checks after
each await and export to refuse late success. Readers must cooperate with
cancellation and avoid blocking the event loop. A callback that never returns or
ignores cancellation indefinitely cannot be forcibly bounded by an in-process
ASGI coroutine; a late returning/blocking callback is refused rather than served
as success.

The existing bound C08 mutation runner and ONE shared result classifier provide
full clean/restored stack receipts plus named expected-test-body assertions.
Fixtures/import/runner failures are inconclusive. Raw output and actual source,
mutant and recipe digests are retained outside the source worktree, then
independently reconciled. All health tests use only a fake scraper; parent
integration regressions use the resource-bounded disposable PG runner.

C02/C11 own general API schema/middleware parity, route mounting and the existing
listener. C07 supplies actual Core reader implementations. This dedicated surface
does not claim those integrations are complete. Design63/H-D465 and ren-tk own
real collector/backend configuration and forwarding; no backend provisioning,
credential enrollment, deployment or Linux/native-release qualification occurs.
