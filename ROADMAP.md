# Roadmap

Cortex ships in dated partial releases on the way to its first complete one. The
three-digit patch is deliberate: `v0.1.001` and `v0.1.002` are partial, `v0.1.003` is the
first complete release. Each entry names what is measured, not what is hoped.

## v0.1.001 — shipped 2026-09-08 (partial)

The actual codebase in the open: API, CLI, schema + 90 migrations, three enrichment workers,
the compose deployment with its receipt-driven lifecycle launcher, and the Node installer.
Handoff lifecycle repairs, the bounded export CLI, the restore evaluator, the artifact
embedding ledger, search degradation reporting and exactly-once handoff returns are in.
Not qualified on a fresh host. The full list of gaps is in the
[README](README.md#known-gaps-v01001).

## v0.1.002 — shipped 2026-09-09 (partial)

Two of seven items landed; the launcher is installable, the stack is still not. What
shipped:

- **MCP over HTTP now refuses** instead of starting an unqualified listener: `main()` exits
  non-zero naming SEC-06 qualification as the blocker. `stdio` stays the supported
  transport. *(was item 2)*
- **Launcher channels are live**: `@kaidera/cortex` on npm and bun, `cortex` in the
  `kaidera-ai/kaidera` Homebrew tap — all three run the identical `bin/cortex.js`.
  `preflight`/`version` work through every channel; `install` still refuses without a
  payload. *(brings forward part of item 3 — the channels, not yet the payload)*
- The SDK-2 MCP test dependency is now declared and its test passes
  (`tests/test_mcp_sdk2_protocol.py`); the repository-relative-path test
  (`tests/test_db_tls.py`) is not fixed yet. *(closes half of item 4)*

Still open, in priority order, each with a test that fails before the fix:

1. **Client and API authentication, end to end** — installation identity, issuer-owned
   API TLS custody, one HTTPS listener, digest-only standalone admission, rotation with a
   finite renewal, backup compatibility and refusal to roll back into plaintext HTTP.
2. ~~MCP over HTTP refused until the bearer requirement lands~~ — shipped above; the
   fail-closed bearer check on the HTTP transport itself is still open.
3. **Published payload and images** — a deterministic release archive from the committed
   projector with source, script, manifest and migration hashes and a repeat proof; the
   installer's `install` path gets a real `--payload`. The channels that will carry it
   (npm, bun, Homebrew) already shipped.
4. **`tests/test_db_tls.py` self-contained** — no repository-relative path.
5. **Python base decision** — 3.14 (this release) or 3.12 (the embedded build), one lock,
   one label, in both images.
6. **Restore invalidates restored token generations; rollback after a failed pending build
   is covered by a read-only schema discovery that does not need the failed API.**
7. **Fresh-host qualification** — fresh boot, seeded upgrade, DB/config/PKI restore and
   launcher upgrade/rollback demonstrated on Linux (rootless Podman) and a macOS Podman
   machine. A first data point: on a clean Rocky Linux 10 / Podman 5.8.2 host, the
   published launcher's `preflight` passes all six checks and `install` refuses correctly
   (exit 2, no payload) — expected behaviour, not qualification.

## v0.1.003 — first complete release

The full standalone programme: the memory-system upgrade waves (27 capabilities derived
from the Hindsight research programme), document-format ingestion (W3), audio/video
ingestion off by default (W4), reproducible optional vision models, and the complete
acceptance run.

## v0.1.x — debts the production system already named

Carried over from the source deployment:

1. **Retention enforcement** — the policy declares N days; a scheduled job must make the
   oldest row agree, and doctor must fail when it does not. (Found: 161d rows under a 90d
   policy, with every surface green.)
2. **Search quality under budget** — rerank sits last in a fixed time budget and was
   silently dropped. Since v0.1.001 the degradation is reported with the provider outcome;
   rerank scheduling itself is still open.
3. **CLI/API contract parity** — the CLI can request states the API rejects (`--mine` /
   `returned`); the contract becomes one generated surface.
4. **Backfill verification** — embedding backfill's `--dry-run` proves nothing; backfills
   report verified effects.
5. **Storage efficiency** — index bloat reduction (−31% measured once; make it routine).

## Later

- Client SDKs beyond the CLI (Python first).
- Pluggable embedding providers via the OpenKai provider registry adapter.
- Multi-project federation and cross-project consult flows.
