# Changelog

## Unreleased — documentation corrections (2026-09-09)

Docs-only; no code, payload or release is attached to this entry. Re-measured on release
revision `c427d5a` on 2026-09-09: the CLI reference separates the projected command surface
(63 executable `cortex-*`, 7 support libraries, 2 fail-closed shims — each shim verified to
exit 2 naming its replacement) from the 72-command source inventory and lists the nine
harness-coupled commands this projection omits; migration count corrected 90 → 87; worker
count corrected three → five (compose default plus opt-in vision/audio); the admin-token
comparison is documented as constant-time (`hmac.compare_digest`) with issuance/rotation
still unwired. The private `docs/cortex-public` mirror was retired as a publication source;
public docs are authored in this repository.

## v0.1.002 — 2026-09-09 (partial release)

Source revision `e95f7ebc9458861f99ac9d8dbbede5dd0fb87922`; schema revision
`8cf7e8f0f8b817a352e4e5292107309199464d049cbd008da464b238568c1966`. Projected by the same
committed projector, verified with a clean dry-run (no secrets, no personal paths, no
`__pycache__`, no stray files outside `packages/` and the manifest).

### Added
- **Launcher channels**: `@kaidera/cortex` published on npm; the same package serves bun;
  `Formula/cortex.rb` added to the `kaidera-ai/kaidera` Homebrew tap. All three run the
  identical `bin/cortex.js` — see [Installing the launcher](README.md#installing-the-launcher).
- MCP-over-HTTP refusal: `CORTEX_MCP_TRANSPORT=streamable-http` now exits non-zero naming
  SEC-06 as the blocker, instead of starting an unqualified listener (was v0.1.001 gap 4).
- `tests/test_mcp_sdk2_protocol.py` collects and passes under the declared `mcp >= 2.1.1`
  test dependency (was half of v0.1.001 gap 9).

### Fixed
- Handoff confirmation: a plain confirmation failure (the ordinary "not yet confirmed"
  case) no longer gets overwritten by a later read-back; the CLI now distinguishes it from
  a real error.
- Two multimodal/graph-worker CLI tests asserted stale dependency pins
  (`setuptools==81.0.0`, `torch==2.13.0`, an older Ollama digest, and the pre-rename
  `Qwen3EmbedBackend` symbol) left over from before the source's own dependency upgrade;
  they now assert the pins the shipped Dockerfiles/locks actually carry. No runtime
  behaviour changed — this is test-fixture drift, not a product defect.

### Changed
- The installer package was named `@kaidera-ai/cortex`; that npm scope is not owned by the
  maintainer (confirmed 403 on publish) and nothing was ever published under it. Renamed to
  `@kaidera/cortex`, the scope this and every other Kaidera package publish under.
- Release identity: `v0.1.002` / npm `0.1.2` for this partial release.

### Verified on this revision
- API suite: 1451 passed, 57 skipped (offline-only tests deselected); the SDK-2 protocol
  test needs `mcp >= 2.1.1`, which the image installs (not the bare interpreter).
- CLI tests: 355 passed, 40 skipped. Standalone tests: 1206 passed, 2 skipped. Installer:
  28/28, `npm publish --dry-run` clean (15.5 kB, 4 files).
- `MCP over HTTP` refusal exits non-zero as designed.
- Projected suite: every file collects and passes except `tests/test_db_tls.py`
  (repository-relative path; unresolved, tracked in [ROADMAP.md](ROADMAP.md) item 4).

### Known gaps
Unchanged from v0.1.001 except where noted above — see
[README.md#known-gaps-v01002](README.md#known-gaps-v01002) for the current list.

## v0.1.001 — 2026-09-08 (partial release)

### Corrections after the tag (main, 2026-09-08, found by the Cortex lane's publication audit)
- The README's "Run from source" recipe was wrong and is withdrawn: the lifecycle launcher only
  acquires prebuilt images from an image lock and reads an install manifest, neither of which
  v0.1.001 ships, so there is no supported way to start the stack from this checkout yet. The
  install guides and the quickstart say the same now.
- The Node launcher reported `0.1.003`; it now reports `0.1.1`, matching the installer package
  identity. The tag `v0.1.001` itself is unchanged; main carries the corrections.

The first tag. The actual codebase, projected from the Kaidera OS production lineage at the
revision in `PROJECTION_MANIFEST.json`. Not qualified on a fresh host; the known gaps are
listed in the [README](README.md#known-gaps-v01001).

### Added
- Standalone deployment in `packages/deploy`: compose file, the receipt-driven `cortex-runtime`
  lifecycle launcher (`prepare-images`, `up`, `check`, `schema-status`, `rotate-leaves`,
  `backup`, `restore`, owner pairing, provider labels), DB/TLS/provider images, `release.json`.
- Node installer in `packages/installer` (`preflight`, `install --payload`, `backup`,
  `restore`; npm identity 0.1.1); 28 offline tests.
- Bounded export CLI; restore evaluator runner with a pre-restore backup.
- Artifact embedding ledger (migration `2026-09-07-01`) and handoff status reconciliation:
  eight-state handoff lifecycle, UUID-safe returns, history-only personas.
- Search degradation reporting: provider outcomes are carried into the degradation status
  instead of a silent fallback; query-embedding cache.
- Exactly-once handoff returns: every row update in the return path is one row or a 409
  inside the transaction; unclaimed handbacks and missing completion receipts are refused.
- TLS custody helpers (`api_tls.py`, `db_tls.py`), service-auth schema (present, inert) and
  the MCP server ported to the SDK-2 protocol.

### Changed
- Release identity: `v0.1.001` / npm `0.1.1` for this partial release; the `v0.1.003`
  programme continues.
- The key-format regex in `api_tls.py` is assembled from two literals so no PEM header
  exists at rest in source (secret scanners stay enabled).
- Python 3.14 stack for the API image, with the lock label bound to the copied lock.

### Removed from the projection
- `cortex-backup` (CLI) — harness-coupled; use the launcher's `backup`/`restore`.

### Docs
- README, roadmap and install guides rewritten for the partial release: rootless Podman on
  Linux and a Podman machine on macOS (Apple Container removed 2026-09-01), run-from-source
  instructions, the gap list.

### Docs carried from the pre-release main

- Docs: Apple Container → rootless Podman migration runbook
  (`docs/guides/migration-apple-to-podman.md`) from the measured 2026-09-01 production
  migration — full sequence, the `pg_restore --no-owner` ownership/ACL restore-fidelity
  discovery, podman-compose gotchas, verification checklist, rollback path; case study
  folded into the deployment-process guide, known-issues appendix in the Cortex guide,
  and the Podman-on-macOS alternative engine documented in the macOS install contract.
- Docs: model guide (how embeddings/rerank are used; Ollama → NVIDIA free → OpenRouter
  ladder), standalone provider guide (subscription plane vs enrichment plane), and the
  functionality reference — 21-area inventory over the measured 79-command / 121-route
  surface, with the code-graph/blast-radius exemplar written from recorded history.
- Docs: six-layer appliance architecture, macOS (Apple Container) + Linux (rootless
  podman) install contracts, the discovery contract (`/.well-known/cortex`, boot context,
  generated harness files), and guides — create a project, multi-agent teams, handoffs,
  memory, ingest, operations.
- Repository scaffolded: license (MIT, replacing the placeholder CC0), README, contract
  docs, contribution + security policy, roadmap.
- v0.1.0 will be the clean import of the production Cortex from Kaidera OS: API (~21k
  lines), 79 CLI commands, embed/graph/pdf workers, schema + migrations, packaged as the
  `kaidera-cortex` wheel with a standalone compose deployment.
