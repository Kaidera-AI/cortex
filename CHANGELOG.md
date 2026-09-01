# Changelog

## Unreleased

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
