# Ingest

Cortex ingests documents and session transcripts into the same searchable store as memory.

- **Documents / PDF** — `pdf-worker` extracts, chunks and stores; `embed-worker` and
  `graph-worker` enrich asynchronously.
- **Sessions** — agent session transcripts can be ingested so past work is searchable, not
  archaeology.

Rules:

- **Point ingestion at an explicit corpus directory.** Never ingest a repo root or a home
  directory into project memory — scope is a feature.
- **Ingest is idempotent by content**, re-runs must not duplicate.
- **Verify the effect**: an ingest is done when search *returns* the content, not when the
  command exits 0. `--dry-run` proves nothing about the real run.
