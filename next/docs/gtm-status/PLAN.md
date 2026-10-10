# CX-GAP-1: customer Cortex status — v2 PLAN proposal

Owner: nemo@helix. Authority: DO NOW H-D455, 2026-10-10 01:59 local.
**Status: ACCEPTED for source unit by Kai r426, DO NOW H-D455 2026-10-10 02:14 local.**
Acceptance pointer: /Volumes/WD-B-4TB/DevVault/helix/docs/handoffs/INBOX_nemo.md r10.
Public dispatch, default endpoint and installed-command acceptance remain C11 gates.

Intent: give a customer one read-only `cortex status` command that reports the
existing local health observation accurately. It cannot establish search
readiness, successful writes, enrollment or release acceptance.

```text
cortex status [--endpoint <literal-loopback-origin>] [--json]
  -> one bounded GET /health -> validate five-field receipt -> output + exit code
```

## Source and dependency boundary

Pinned main `67265f371a8829d12511c082b7792a1441ddcd77` has the
[health contract](https://github.com/Kaidera-AI/cortex/blob/67265f371a8829d12511c082b7792a1441ddcd77/next/docs/gtm-health-metrics/CONTRACT.md)
and `TelemetryGateway`; no `next/` CLI entrypoint was found in the source inventory.
Gateway construction defaults to port **0**, and C02/C11 still own the actual
listener/mount. Do not invent port8501 or a deployment endpoint.

Build only the v2 CLI module under `next/src/cortex_core/cli/`. The release's
public `cortex status` dispatch and its default endpoint must be supplied through
the accepted C11 launcher/listener integration. The source unit can accept an
explicit endpoint and be invoked as `PYTHONPATH=next/src python -m cortex_core.cli status ...`.
Missing configured endpoint fails visibly. Customer-command/default-endpoint
acceptance remains gated until that C11 binding is pinned and tested. This plan
does not authorize editing the legacy installer or claiming the v1 doc gap fixed.

## Proposed contract

- Resolve explicit endpoint before the release-supplied local default. Accept
  only HTTP/HTTPS origins with literal loopback IPs and ports1–65535; support
  IPv4 and bracketed `::1`. Refuse DNS, wildcard/external hosts, credentials,
  query/fragment and non-root paths before transport. Append `/health` once.
- One request, no retry, redirects or environment-proxy use. Keep TLS
  verification. Proposed whole client budget **2 seconds**, body cap **16KiB**;
  bound streamed read and parsing, check elapsed deadline before success.
  These are prototype limits, not measured production SLOs.
- Reconstruct only the existing five fields: `component`, `status`,
  `core_available`, `conductor`, `reason`. Require strict types, allowed values
  and HTTP/state agreement. HTTP200 accepts `ok/true/healthy/null` or
  `degraded/true/supervisor_down/null`; HTTP503 accepts
  `unavailable/false/core_unavailable/{core_unavailable,timeout}`. Refuse
  non-JSON content types, malformed, oversized, extra-field or inconsistent replies. Never print raw
  response bodies, dependency exceptions or caller URL/credential text.
- Human output states Core and Conductor separately; it never says “ready”.
  `--json` emits an envelope with `schema: "cortex.status.v1"`, `health` (validated
  receipt or null) and `client_error` (null or finite safe code). Client failures
  use `timeout`, `connection_unavailable`, `endpoint_refused` or
  `invalid_response`; they do not fabricate a server observation.
- Exit **0** = ok, **1** = degraded, **2** = unavailable/transport/protocol
  failure, **64** = usage/endpoint configuration refusal, **130** = interruption.
  Both output modes preserve those distinctions. No credential, database,
  repair, metrics scrape or monitoring forwarding is involved.

## Files and order after acceptance

1. Fresh isolated Nemo worktree on current main; perform ownership/scope checks.
   Fold this accepted plan into `next/docs/gtm-status/PLAN.md`. Commit a deny-all
   CLI baseline with frozen tests in `next/tests/integration/test_cli_status.py`;
   retain intended body-assertion REDs, never count import/fixture errors.
2. Implement `next/src/cortex_core/cli/status.py` and `cli/__main__.py` against
   the existing health receipt. Document `next/docs/gtm-status/CONTRACT.md`.
   Do not alter the gateway, Core or shared result classifier.
3. Test argument/output dispatch with injected fake ASGI/HTTP transport only:
   all three server states; Core and early dependency failures; wrong status or
   fields; endpoint refusal before calls; redirect refusal; stalled/late body;
   oversized/invalid JSON; safe human/JSON output; cancellation.
4. Add `next/tests/integration/mutate_cli_status.py`, reusing the bound parent
   driver and ONE classifier. Named mutants must fail at their expected test-body
   assertion. Retain source/raw/recipe digests, full clean/restored integration
   receipts, independent reconciliation and actual disposable-PG cleanup.
5. Merge current target tip, rerun, push one PR, fold evidence and remove the
   parked worktree. Independent review then kai merge gate. C11 separately
   proves the installed `cortex status` dispatch/default endpoint on Linux x86_64
   and macOS arm64 before customer publication.

## Risks and acceptance request

The main risk is a customer mistaking a local health snapshot for full readiness;
the schema/output and distinct degraded exit prevent that claim. Launcher name
collision and an unfrozen listener are explicit C11 dependencies. Source revert
is two-way; blast radius **diagnostics**. Verification above is **PROPOSED / NOT_RUN**.

Kai accepted this bounded source unit, endpoint/default binding boundary,
exit mapping and proposed limits at02:14. Release packaging,
signing, native-host proof and publication remain their existing gates.

## Build refinements

The body is UTF-8 JSON with no duplicate keys. Request identity encoding and refuse
compressed responses so the 16KiB limit cannot become an unbounded decompression.
The source-only unit has no default endpoint: the accepted C11 binding is still
required. Usage and endpoint refusals share the finite `endpoint_refused` code;
exit64 identifies either refusal and no untrusted argument is echoed.
The original 28 test methods remain frozen at RED commit `2ffd6cf`.
Two additive controls exercise genuinely compressed JSON and default HTTPS
transport certificate verification; no original test method is altered.
An additive wrapper exposes the original interruption assertion to the existing
named runner's letters/underscores restriction, preserving that frozen method.
A RED-first depth-limit control requires excessive JSON nesting to remain a safe
protocol refusal, rather than a transport-failure classification.
An automatic RED-first check rejects unselectable/missing named mutation targets
and drifting source recipes before their individual disposable-PG runs.
