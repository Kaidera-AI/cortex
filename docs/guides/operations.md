# Operations

## Doctor — verify effects, not declarations

```bash
cortex-doctor
```

Every check asks about an **effect**: schema receipt matches, search answers with a row just
written, enrichment queues drain, oldest row within retention policy, workers alive *and
last run succeeded*. Never "is the config present", never "is the unit enabled" — a unit
that ran and failed still reports enabled, and an introspection API asked about a thing
that does not exist may synthesise a default answer. Both burned us; both are designed out.

## Scheduled jobs

Retention enforcement, ingest sweeps and health checks are **jobs a scheduler runs** —
cron, a project orchestrator agent, anything. Cortex executes and reports honestly:
**a repair path reports whether it repaired, not merely that it ran.**

## Backup

The host owns backup. Cortex's contract: one Postgres database, dumpable with `pg_dump`;
restore + migrations forward from any receipted baseline. Verify a backup by its content
(row counts, a sentinel row round-tripped) — an exit-0 backup once produced 0 bytes.

## Upgrade

Forward-only, receipted migrations. A newer migrator adopts exactly the inventory the old
one represented, then applies only genuinely newer files — skipping patch versions is
supported, downgrades are not. Data rollback = restore a dump, an explicit operator act.
