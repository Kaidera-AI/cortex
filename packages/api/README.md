# Cortex API — Agent Memory Service

Agent-facing Cortex operations go through this API at `http://localhost:8501`.
The listener is loopback-only. The current local stack is Postgres plus
`cortex-api`; Redis service/dependency wiring has been removed. Legacy
maintenance commands still use token-gated admin SQL compatibility endpoints
through the same API while the long-tail migration continues.

This document describes the local Cortex listener on this Mac only. It is not
the Kaidera AI platform deployment spec and not an external project/customer
deployment spec.

## Quick Start

```bash
# Verify the API is running
curl http://localhost:8501/health
```

Expected local health includes `event_backend=postgres` and
`event_bus=postgres`. A healthy post-Phase-2 response should not require a
Redis service field.

## Endpoints

| Method | Path | Purpose | Required Headers |
|--------|------|---------|-----------------|
| GET | `/health` | Service health check, including API/schema version visibility | None |
| GET | `/boot/{agent}` | Session boot context. Add `?query=<topic>` for topic recall | `X-Project` |
| GET | `/state` | Sprint summary + counts | `X-Project` |
| GET | `/roster` | Visible agent roster | `X-Project` |
| GET | `/board` | Task board | `X-Project` |
| POST | `/board` | Create task | `X-Project` |
| PATCH | `/board/{id}` | Update task status | `X-Project` |
| GET | `/search?q=...` | Hybrid search across Cortex memory and L5 artifacts. Supports `type`, `room`, `hall`, `graph`, `rerank` | `X-Project` |
| POST | `/log` | Log decision/lesson (auto-embeds) | `X-Agent-Name`, `X-Project` |
| GET | `/history` | Recent message history | `X-Project` |
| GET | `/handoffs` | List handoffs. Add `?agent=<name>` for role-filtered `--mine` behavior | `X-Project` |
| POST | `/handoffs` | Create handoff | `X-Agent-Name`, `X-Project` |
| PUT | `/handoffs/{id}/claim` | Claim handoff | `X-Agent-Name`, `X-Project` |
| PUT | `/handoffs/{id}/complete` | Complete handoff | `X-Project` |
| POST | `/diary/{agent}` | Write session diary | `X-Project` |
| GET | `/diary/{agent}` | Read session diary | `X-Project` |
| POST | `/memory` | Upsert durable knowledge | `X-Agent-Name`, `X-Project` |
| POST | `/work-products` | Record completed work receipts for reusable project memory | `X-Agent-Name`, `X-Project` |
| GET | `/work-products` | Search/list work receipts by query, file, symbol, handoff, or status | `X-Project` |
| GET | `/work-products/{id}` | Read one work receipt | `X-Project` |
| POST | `/invalidate/{id}` | Invalidate / undo an item | `X-Project` |
| GET | `/projects` | List all registered projects | None |
| GET | `/projects/{project}` | Read one registered project and roots | None |
| GET | `/projects/{project}/runtime` | Effective project runtime profile for launchers, Beat, setup, and package verification | None |
| POST | `/projects` | Register/update project roots, default agent, and initial agent roster | `X-Cortex-Admin-Token` |
| GET | `/identity/audit` | Identity v2 audit for actor aliases, unresolved project/actor references, and legacy identity drift. Optional `?project=<project-key>` | `X-Cortex-Admin-Token` |
| POST | `/sessions/ingest` | Ingest a provider chat transcript into a registered project | `X-Project` |
| POST | `/artifacts` | Idempotent typed L5 artifact write with optional edge | `X-Agent-Name`, `X-Project` |
| POST | `/artifacts/transcribe` | Proxy audio/video multipart content to the internal Whisper worker | `X-Agent-Name`, `X-Project` |
| POST | `/artifacts/describe-image` | Proxy image multipart content to the internal vision worker | `X-Agent-Name`, `X-Project` |
| GET | `/workers/health` | Reachability snapshot for optional internal workers | `X-Project` |
| GET | `/beat/embeddings/backlog` | Embedding backlog/stats by table | `X-Cortex-Admin-Token` |
| POST | `/beat/embeddings/backfill` | Queue or run typed embedding backfill job | `X-Cortex-Admin-Token` |
| GET | `/beat/embeddings/jobs/{id}` | Read embedding job status/progress | `X-Cortex-Admin-Token` |
| POST | `/cortex-graph-extract` | Entity extraction through API-owned admin boundary | `X-Cortex-Admin-Token` |

## Headers

Project-scoped agent requests include these headers:
- `X-Agent-Name`: the agent making the request (e.g., `lead`, `worker`, `reviewer`)
- `X-Project`: the project scope (e.g., `<project-key>`)

Identity v2 normalizes `X-Agent-Name` to a project-scoped display identity on
write. Normal clients should send base agent names such as `lead`; Cortex stores
display identity as `lead@<project-key>` and attaches structured `project_id`/actor
IDs. The retired `agent:hex` form is rejected on new writes.

## Examples

### Boot a session
```bash
curl http://localhost:8501/boot/lead -H "X-Project: <project-key>"
```

### Log a decision (auto-embeds)
```bash
curl -X POST http://localhost:8501/log \
  -H "X-Agent-Name: lead" -H "X-Project: <project-key>" \
  -H "Content-Type: application/json" \
  -d '{"event_type": "decision", "summary": "Decided to use OpenAI for embeddings"}'
```

### Search
```bash
curl "http://localhost:8501/search?q=dashboard+requirements&hall=project&graph=true" \
  -H "X-Project: <project-key>"
```

Artifact rows are part of the `/search` candidate pool. Lexical display text falls back
through `caption`, `neighborhood_text`, `raw_content`, `section_context`, and
`source_file`. Embedding backfill uses non-empty `raw_content`, then `caption`, then
`source_file`; the vector stage returns semantic artifact candidates with the same
768-dimensional query contract.

```bash
curl "http://localhost:8501/search?q=<artifact-text>&type=artifacts&rerank=false" \
  -H "X-Project: <project-key>"

cortex-search "<artifact-text>" --type artifacts --limit 10
```

An artifact write is not accepted until this read path returns it. A blank or missing
result is a search/embedding defect, not an ingestion success.

### Read runtime profile
```bash
curl -s http://localhost:8501/projects/<project-key>/runtime | python3 -m json.tool
```

The runtime profile is read without `X-Project` because the project key is in
the path. It is the launcher/setup contract for project identity, roots,
roster, Beat ID, and launchd label.

For identity v2, runtime consumers should use:
- `agents[].runtime_id` / `beat.agent_id`: display identity in `agent@project` form
- `project_id` and actor IDs from API rows: canonical database references

`project_hex` is not part of the runtime contract after the clean cutover. Do
not export, derive, or persist it in worker scripts.

### Audit identity state
```bash
curl -s http://localhost:8501/identity/audit \
  -H "X-Cortex-Admin-Token: <admin-token>" | python3 -m json.tool

curl -s "http://localhost:8501/identity/audit?project=<project-key>" \
  -H "X-Cortex-Admin-Token: <admin-token>" | python3 -m json.tool
```

The endpoint returns actor and alias counts plus any unresolved actor/project
references or invalid legacy identity forms. A healthy result has
`"issue_count": 0`.

### Create a handoff
```bash
curl -X POST http://localhost:8501/handoffs \
  -H "X-Agent-Name: lead" -H "X-Project: <project-key>" \
  -H "Content-Type: application/json" \
  -d '{
        "to_role": "<role>",
        "priority": "urgent",
        "summary": "Fix the auth flow",
        "acceptance": {"criteria": ["tests pass", "operator can sign in"]},
        "evidence": {"required": ["test command", "diff summary"]},
        "retry": {"max_attempts": 2},
        "escalation": {"after_attempts": 2, "to_role": "lead"}
      }'
```

### Record completed work
```bash
cortex-work-product --write \
  --agent <agent> \
  --handoff <handoff-id> \
  --title "<short title>" \
  --summary "<what is now true and how to use it>" \
  --files "path1,path2" \
  --symbols "module.func,script-name" \
  --tests "pytest path/to/test.py=passed" \
  --risks "none"
```

Workers should query work products before rereading source for completed work:

```bash
cortex-work-product --brief "<feature, file, symbol, or handoff>"
cortex-work-product --list --file path/to/file --status all
```

## Multi-Project Support

All projects share one Cortex database, but project retrieval is now split into
explicit halls:
- `hall=project` searches the active project only. This is the default.
- `hall=shared` searches shared cross-project knowledge (`_global`) only.
- `hall=all` searches `project + _global`.
- `hall=local` searches isolated local-state rows (`_local_state`) only.

Claude todos/plans/IndexedDB local-state is stored in `_local_state` and is not
part of default project search.

Project keys are registry-backed. Bootstrap, search, handoff, `POST /agents`,
and `POST /sessions/ingest` require `X-Project` to match `cortex_projects`;
unknown project scopes fail with a clear 4xx before writes. Use `POST /projects`
or `cortex-init-project` to onboard a new project and arbitrary agent names
before ingesting transcripts.

Runtime consumers should read `GET /projects/{project}/runtime` instead of
embedding project keys, roots, rosters, Beat IDs, or launchd labels in scripts.
Workspace/project JSON remains an import/export surface;
Cortex registry data is the runtime source of truth.

`cortex-sync-workspace` updates configured projects without pruning unrelated
registered projects by default. Destructive registry pruning requires the
explicit `--prune-missing` maintenance flag.

The local workspace registry may include external project keys for dogfooding
and comparison work. That does not imply shared customer boundaries, shared
deployment, or shared production authority between those projects.

## Embedding

High-value write paths auto-embed where the API route supports it. Bulk catch-up
uses the typed `/beat/embeddings/backfill` job boundary via `cortex-embed`; the
CLI must not call providers directly or issue raw SQL for normal worker use.
Search uses the same embedding family for query embedding, then reranking when
configured.

```bash
cortex-embed --stats
cortex-embed --table all --async
cortex-embed --job <job-id>
```

## Architecture

```text
Agent session → cortex-* CLI scripts → HTTP → local Cortex API (:8501) → Postgres + internal workers
```

Agent-facing commands use typed API routes. Legacy maintenance commands still
reach token-gated `/admin/sql/*` compatibility endpoints over loopback while
the remaining maintenance scripts are migrated. The old Redis admin passthrough
is deliberately gone from normal and maintenance flows.

Normal workers use API-backed commands only: `cortex-bootstrap`,
`cortex-handoff`, `cortex-log`, `cortex-search`, `cortex-graph-search`,
`cortex-work-product`, `cortex-diary`, `cortex-roster`, read-only graph/code
queries, and `cortex-embed --stats`. Direct `psql`, `redis-cli`, container exec,
`_cortex_lib.sh`, `/admin/sql/*`, and recovery commands are operator surfaces.

## Admin Compatibility

`/admin/sql/query` and `/admin/sql/exec` exist only for local maintenance
compatibility. They require `X-Cortex-Admin-Token` and are intended for loopback
use only. `/admin/redis` returns `410 Gone`; Redis service/dependency wiring
has been removed from the normal local Cortex stack.
