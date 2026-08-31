# Deployment

## Standalone

One compose project, three tiers, strict order: `db` → migrations → `api` → workers.

- **Linux:** rootless podman (>= 5.0 recommended, cgroup manager `systemd`).
- **macOS:** Docker or Apple Container. One containerisation technology per machine —
  do not mix engines on a host.
- Health endpoints gate readiness; a service is up when its probe answers, not when its
  container starts.
- **Migrations are forward-only** and receipted: the running schema baseline is recorded,
  and a newer migrator adopts exactly the inventory the old one represented before applying
  newer files. Skipping intermediate patch versions is supported; downgrades are not.
- Backups are the host's job (Cortex documents its dump contract: `pg_dump` of the single
  database + the receipts table); Cortex never touches volumes.

The compose file and image builds land with v0.1.0.

## As a module inside Kaidera OS

The OpenKai pattern, exactly:

1. Each Cortex release publishes a **versioned artifact** (`kaidera-cortex-X.Y.Z` wheel:
   API + workers + migrations + CLI).
2. The consuming product **vendors the exact artifact, hash-pinned** — no network at image
   build, no git clone at build time, provenance receiptable.
3. The consumer's images install the pinned artifact; the consumer owns the images, Cortex
   owns the code.
4. Cortex-built images carry `org.opencontainers.image.source=https://github.com/Kaidera-AI/cortex`
   so supply-chain evidence can attribute every layer to its source repo.

## Operations

`cortex-doctor` verifies **effects**: retention applied (oldest row vs policy), search
answering, enrichment queues draining, schema receipt matching. A scheduler (cron, an
orchestrator agent, anything) runs the periodic jobs — retention enforcement, ingest sweeps,
health checks. Cortex executes them and reports honestly; **a repair path must report
whether it repaired, not merely that it ran.**
