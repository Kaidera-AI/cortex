# Memory: decisions, lessons, progress

Three kinds of durable memory, all authored as `worker@project`:

```bash
cortex-log kai decision "kai@my-product chose Postgres-only queues; evidence: load test 12k/s"
cortex-log kai lesson   "podman stamps an empty volume with the first container's uid; check errno first"
cortex-log kai progress "extraction P2: api packaged, compose smoke green"
```

- **decision** — what was chosen and *why*, with the evidence. The future reader is a
  teammate (or you) six months out.
- **lesson** — what reality taught that documentation didn't. The most valuable rows in the
  store; write them the moment the surprise happens.
- **progress** — durable state for continuity across sessions.

## Finding it again

```bash
cortex-search "volume ownership uid"
```

Search runs semantic + rerank + graph over everything: memory rows, handoffs, ingested
documents. Enrichment (embeddings, graph edges) happens in workers after the write — logging
is never blocked on it.

## Retention

Memory is tiered and retention is a **policy the system enforces**, not a comment. The
doctor fails when the oldest row disagrees with the declared policy — a declared-but-dead
policy once sat unenforced for 71 days past its window before this check existed.

## What makes memory good

- State identity and evidence in the text itself (`kai@my-product ... ; evidence: ...`) —
  rows outlive their context.
- Log at the moment of decision, not in a batch at the end.
- Wrong memories get corrected or deleted; a stale row that names a dead flag costs more
  than no row.
