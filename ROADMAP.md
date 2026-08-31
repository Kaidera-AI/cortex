# Roadmap

## v0.1.0 — the extraction (in progress)

Clean import from Kaidera OS at a pinned revision: `cortex-api`, embed/graph/pdf workers,
Postgres schema + forward-only migrations, the `cortex-*` CLI, packaged as a wheel.

Ships as the **six-layer containerised appliance** with one-command installers — macOS
(installs Apple Container if missing) and Linux (rootless podman, fail-loud requirements) —
plus the **discovery contract**: `/.well-known/cortex` capability manifest, agent boot
context, and generated harness instruction files. Real-engine deploy smoke in CI.

## v0.1.x — debts the production system already named

Carried over from the source deployment, in priority order:

1. **Retention enforcement** — the policy declares N days; a scheduled job must make the
   oldest row agree, and doctor must fail when it does not. (Found: 161d rows under a 90d
   policy, with every surface green.)
2. **Search quality under budget** — rerank currently sits last in a fixed time budget and
   is silently dropped; rerank must either run or report that it did not.
3. **CLI/API contract parity** — the CLI can request states the API rejects (`--mine` /
   `returned`); the contract becomes one generated surface.
4. **Backfill verification** — embedding backfill's `--dry-run` proves nothing; backfills
   report verified effects.
5. **Storage efficiency** — index bloat reduction (−31% measured once; make it routine).

## Later

- Client SDKs beyond the CLI (Python first).
- Pluggable embedding providers via the OpenKai provider registry adapter.
- Multi-project federation and cross-project consult flows.
