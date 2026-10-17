# Versioned Cortex v2 CLI

`_cortex_api.sh` is the one packaged A transport. Core owns config admission,
project selection, encrypted custody and the seven-field private projection.
This source slice has loopback/unit evidence only. It does not qualify an
installed client, API runtime, real scope enforcement or a signed release.

The launcher supplies `CORTEX_CLIENT_PROJECTION_FD`, a nonsecret descriptor
number for an owned read-only anonymous pipe. Its complete UTF-8 frame is seven
NUL-terminated fields followed by EOF: protocol `cortex.client.v1`, loopback
URL, installation UUID, project UUID, project name, committed 64-hex Bearer,
and total request seconds (1–10). Reading is bounded to one second and 8192
bytes. Regular files, extra fields, unclosed pipes and unsafe values refuse.
No config file, legacy project discovery, environment credential, admin.env
scan, token file or alternate credential source is consumed.

Source the helper without shell tracing or export-all. It caches the admitted
frame in unexported shell variables for repeated calls in that invocation.
`cortex_api_call METHOD PATH [JSON] [AGENT]` uses only projected selectors.
Mutations require the caller's stable, nonsecret `CORTEX_IDEMPOTENCY_KEY` and
agent. The helper never generates a key or automatically retries. An uncertain
outcome must be explicitly reconciled with the SAME key and identical intent.
Caller curl flags and legacy payload-file overrides refuse.

Curl remains the sole network transport. Python's stdlib supervises its private
config/input pipes, response bounds and one absolute deadline. Authorization
is absent from argv, the child environment, temporary files and public health.
Curl startup configuration and proxies are disabled; redirects are refused;
HTTPS keeps stock certificate/hostname verification. No insecure flag or
certificate override exists. A signal delivered to the Python supervisor closes pipes and reaps its owned
curl. Bob still owns wrapper/service cancellation propagation.
Responses are bounded to 1MiB and decoded once as unambiguous UTF-8 JSON.
Malformed responses and transport failures emit one typed JSON refusal on stderr.

The HTTP design in `J06-http-composition-v1.md` is reconciled by Cox's explicit
Option A health ruling. Public `/health` must return HTTP 200 and exactly four
JSON fields: `component=cortex`, `status=ok`, `readiness=ready`, and the exact
projected `installation_id`. Every condition must hold before Bearer transmission.
Extra or duplicate fields, missing readiness, nonready states, malformed types,
wrong component/identity, and the legacy `status=ready` shape refuse through the
existing strict JSON decoder and transport. There is no legacy fallback;
successful mutations must contain `receipt` with canonical nonzero UUID
`record_id,event_id`, positive integer `revision`, and lower-case SHA256
`payload_sha256`. Legacy 2xx memory responses without that receipt refuse.
Core error objects preserve `code,capability?,state?,reason?,retryable,message?`
on stderr with exit 22. The optional public message must be a string of at most
256 UTF-8 bytes; arbitrary error fields still refuse. This preserves the existing
gateway's fixed unauthenticated 401 packet. Actual runtime health and committed
response binding remain Cox's source dependency. The seven-field protocol has
no release field; installed and signed release qualification require their own
receipts.

`test_transport_composition.py` joins the published Core encrypted custody and
actual `runtime.operator config client` subprocess to this helper through an
inherited anonymous pipe. Its public fixture stages a committed encrypted record
without SQL enrollment; its HTTP recorder is synthetic loopback. The 401 packet
alone comes from the existing gateway `_error_status`. These controls demonstrate
the source composition and never grant SQL, installed, native or release credit.

Run source controls with `python3 -B -m unittest discover -s next/modules/cli-v2/tests
-p 'test_transport_*.py' -v` with the composed Core source and its pinned crypto
dependency available. Bind PATH to the same interpreter directory so the helper
subprocess uses that actual runtime too. Existing paused CLI sources are retained; this
slice does not claim their complete A command-surface integration.

LEAN: one Core config/custody authority, one private transient projection, one
existing helper and one stock curl transport; no second HTTP/config client or
persisted credential store.

## Versioned entries

Only `cortex-v2-boot`, `cortex-v2-log`, and `cortex-v2-search` in this module
source this strict helper. Bob owns installation/launcher names and the actual
projector -> wait/reap -> same-reader-FD transfer. This source move never edits
PATH or a live installation. All retained `packages/cli` sources are restored
to main `7a1078213ac2497d6219e7778c7866001f97f30b`; v1 command names retain
their prior contract and never source this module.

The three adapters reuse the existing shell argument and JSON serialization.
Boot keeps its agent/budget/full/query API shape; unsupported historical requests
must receive the actual Core refusal. Search sends explicit `mode=keyword`,
`type=all`, `hall=project`, `rerank=false`, `graph=false`; other existing flags
remain explicit requests for Core to admit or refuse. Log preserves exact
summary strings and ordered duplicate file names, requires a caller-supplied
stable `CORTEX_IDEMPOTENCY_KEY`, the committed receipt and verified response,
and defaults to the existing exact `/verify/write` readback. No automatic goal
metadata or summary prefix is added; legacy `--goal` is rejected. No command
fabricates a capability or retries against v1. Actual enrolled API capabilities
remain Core's responsibility. Loopback command tests grant no SQL/installed credit.

## Health contract reconciliation

Cox's retry5 ruling is recorded in
`2026-10-17_cox_finn_a-day2-health-ruling.md`, SHA256
`5be624ca46df50462669085064b86d95671d102876dd9e001061e64af97113cd`.
`test_health_join.py` derives packets from actual bound Core services with
explicit SQL/policy fixtures. Its three test cases remain byte-identical to
the committed BODY-RED source. Additional controls cover exact packet fields,
readiness states/types, component and identity, duplicate/malformed JSON,
HTTP status and legacy-status refusal before any bearer-bearing request.

The separate test-contract commit changes only old synthetic success-health
packet inputs to canonical `ok` plus `readiness=ready`. All existing assertion
calls, expected outcomes, payload/private-FD/idempotency/receipt negatives and
v1 baseline bytes are preserved. Original packets and all RED histories remain
in the immutable handoff evidence. The explicit old wrong-installation packet
is retained; a canonical wrong-installation case separately isolates that guard.
These source fixtures grant no SQL enrollment, installed, native or release
qualification. Cox owns the final independent source review and composition.
