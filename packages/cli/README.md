# Packaged Cortex HTTP helper

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

The following HTTP join is PROPOSED and remains OPEN pending Cox's accepted
wire/source receipt: public `/health` must report `component=cortex`,
`status=ready` and the projected installation UUID before Bearer transmission;
successful mutations must contain `receipt` with canonical nonzero UUID
`record_id,event_id`, positive integer `revision`, and lower-case SHA256
`payload_sha256`. Legacy 2xx memory responses without that receipt refuse.
Core error objects preserve `code,capability?,state?,reason?,retryable` on stderr
with exit 22. The caller key selector is likewise published for reconciliation.
A release-binding field and actual Core/Bob/Nix installed composition still need
qualification; synthetic response fixtures do not supply it.

Run source controls with `python3 -B -m unittest discover -s packages/cli/tests
-p 'test_transport_*.py' -v`. Existing paused CLI sources are retained; this
slice does not claim their complete A command-surface integration.

LEAN: one Core config/custody authority, one private transient projection, one
existing helper and one stock curl transport; no second HTTP/config client or
persisted credential store.
