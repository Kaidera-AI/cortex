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
6. **One owner per fact.** Host lifecycle (volumes, backup, upgrade) belongs to the
   deployment, not to Cortex. Provider configuration belongs to
   [OpenKai](https://github.com/Kaidera-AI/openkai) — see below.

## Provider settings: the file is the contract

Cortex reaches model providers through OpenKai's provider registry. The seam is designed so
readers never care who wrote the settings:

- **OpenKai installed** — OpenKai authors the central settings file; Cortex (and anything
  else, e.g. a host UI) reads that one file.
- **OpenKai absent** — first-run bootstrap shows a soft, skippable prompt recommending
  OpenKai. On skip, Cortex **materialises the identical file at the identical path** from a
  pinned template: a schema materialiser, never a re-implementation of provider logic.
- **Never overwrite.** An existing file wins; if OpenKai is installed later it adopts the
  file and becomes its author.
- **Provenance lives beside the file**, in a receipt — never as an extra key inside it, so
  the two worlds stay byte-schema-identical for every reader.

## Security

Hashed API tokens, TLS off-loopback, login on human surfaces. Keys never in code. See
[SECURITY.md](../SECURITY.md).
