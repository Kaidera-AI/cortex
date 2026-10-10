# CX-GAP-1 status source contract

Accepted source unit: `PYTHONPATH=next/src python -m cortex_core.cli status
--endpoint <literal-loopback-origin> [--json]`. This is the source invocation;
installed `cortex status`, default endpoint and public dispatch remain **C11 gates**.
Missing endpoint refuses with exit64. No listener, database, credential lookup,
repair or monitoring forwarding is involved.

An explicit HTTP/HTTPS origin must use a literal loopback IP and explicit port
1–65535. IPv4 loopback and bracketed `::1` are supported. Empty/root paths are
accepted; DNS, external/wildcard IPs, userinfo, non-root paths, query, fragment,
scope IDs, whitespace, controls and backslashes refuse before transport.

One `GET /health`, no retry, redirect or environment proxy. TLS verification is
enabled. A 2-second whole-operation budget covers transport, body read and parse;
monotonic checks refuse a callback/parser that blocks the event loop past it.
Actual body bytes must total at most 16KiB, regardless of Content-Length. Ask for
identity encoding and refuse compressed replies. Require UTF-8 JSON with media
type `application/json`, an object with exactly the five health fields and no
duplicate keys. No response body, raw URL or dependency exception is echoed.

| HTTP | status | core_available | conductor | reason | Exit |
| --- | --- | --- | --- | --- | --- |
| 200 | ok | true | healthy | null | 0 |
| 200 | degraded | true | supervisor_down | null | 1 |
| 503 | unavailable | false | core_unavailable | core_unavailable or timeout | 2 |

`component` must be the string `cortex`; `core_available` must be a JSON boolean.
Every other combination refuses as `invalid_response` with exit2.

JSON prints one envelope:

```json
{"schema":"cortex.status.v1","health":{"component":"cortex","status":"degraded","core_available":true,"conductor":"supervisor_down","reason":null},"client_error":null}
```

Client failures set `health` to null and `client_error` to `timeout`,
`connection_unavailable`, `endpoint_refused` or `invalid_response`. Endpoint and
usage refusals exit64; transport/protocol failures exit2. Human output prints
the status, Core availability, Conductor state and server reason or client code
separately. It never establishes readiness, enrollment or write/search success.
KeyboardInterrupt at the source entrypoint exits130 without a traceback; caller
task cancellation propagates and the response/client are closed.

Verification: `test_cli_status.py` uses fake HTTP/ASGI transports only. Named
mutations reuse `mutate_conductor.py` and the one shared `test_receipts.py`
classifier. Full clean/restored integration runs inherit one disposable,
resource-bounded Postgres runner; status itself never connects to Postgres.
Only the expected test-body AssertionError counts as a semantic kill.
Linux/native installed-command and actual-listener acceptance are **NOT_RUN**.
