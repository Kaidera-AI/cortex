# Architecture

## Services

| Service | Job | Talks to |
|---|---|---|
| `cortex-api` | FastAPI. Memory, handoffs, registry, search, boot context, doctor | Postgres |
| `embed-worker` | embedding enrichment for memory + ingest rows | Postgres |
| `graph-worker` | graph enrichment (entities, relations) | Postgres |
| `pdf-worker` | document/PDF ingest | Postgres |
| `db` | Postgres 16 — the single source of truth | — |

Workers are enrichment, not request-path: the API accepts writes immediately and workers
catch up. The CLI (`cortex-boot`, `cortex-handoff`, `cortex-log`, `cortex-search`,
`cortex-projects`, `cortex-roster`, `cortex-state`, …) is an HTTP client — **nothing
mutates Postgres directly except the API and its migrations.**

## Data domains

- **memory** — decisions / lessons / progress, embeddings, graph edges, retention tiers
- **coordination** — handoffs (pending → claimed → complete, plus consult), evidence fields
- **registry** — projects, rosters, worker identities, runtime profiles, boot context
- **ingest** — documents, sessions, enrichment queues (Postgres tables, not a broker)

## Principles

1. **Postgres only.** No Redis, no second store. Queue state is rows.
2. **API-only access.** A missing endpoint is a tooling gap to fix, not a reason to touch
   the database.
3. **Verify the effect.** Doctor asks "is the oldest row within the retention policy",
   "does search return the row just written" — never merely "does the config exist".
   `systemctl`-style introspection that synthesises defaults taught us this the hard way.
4. **No-privilege runtime.** No root, no password prompts, no OS-global mutable state in
   the data path. Every repair is possible as the owning user.
5. **Fail loud.** Fresh deploys bootstrap schema explicitly and write a receipt. Silent
   defaults are the most expensive defect class we know.
6. **One owner per fact.** Provider configuration belongs to OpenKai (Cortex ships an
   adapter, never a second implementation). Host lifecycle (volumes, backup, upgrade)
   belongs to the deployment, not to Cortex.

## Security

Hashed API tokens, TLS off-loopback, login on human surfaces. Keys never in code. See
[SECURITY.md](../SECURITY.md).
