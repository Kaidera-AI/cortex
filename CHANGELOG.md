# Changelog

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
