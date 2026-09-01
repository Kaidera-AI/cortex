# The Cortex Guide

**The definitive "read this first" for anyone deploying Cortex standalone.**

**What it answers:** *"What is Cortex, how does it fit together, how do I stand it up,
point it at model providers, and put a team of agents on it — without reading the
source?"*

Cortex is **persistent memory and coordination for AI agent teams**, backed by a single
Postgres database. It was extracted from the battle-tested
[Kaidera OS](https://kaidera.ai) production lineage. The current extraction target is
≈20.7k lines of FastAPI, 72 executable CLI commands, 123 API routes, and five enrichment
workers; standalone availability remains governed by the payload and discovery gates.

This guide is organised as:

1. [What Cortex does](#1-what-cortex-does) — the functionality inventory
2. [How it works](#2-how-it-works) — diagrams of every core flow
3. [How to set up](#3-how-to-set-up) — standalone, with KOS, with OpenKai
4. [Providers configuration](#4-providers-configuration) — every provider type, every key
5. [Workflow examples](#5-workflow-examples) — practical, copy-pasteable
6. [Limits (honest)](#6-limits-honest) and [Sources](#7-sources)

---

## 1. What Cortex does

### What it is

Cortex gives a team of AI workers what a human team takes for granted: **memory that
survives the session, and work that moves between workers without a human relaying it.**

| Capability | What you get |
|---|---|
| **Memory store** | Decisions, lessons, progress and knowledge rows, authored as `worker@project`, with embeddings and graph enrichment |
| **Search** | Hybrid BM25 + trigram + vector + graph expansion, fused (RRF) and reranked, over memory, handoffs and ingested documents |
| **Handoffs** | Durable units of work with an atomic claim → complete lifecycle; survive sessions, restarts and model changes |
| **Agent identity** | Every worker is `name@project`; identity rides every row, every handoff, every log line |
| **Project registry** | Projects are the isolation boundary: own memory, roster, handoffs, rules |
| **Boot context** | One call tells an agent who it is, the project's rules, the roster, and what is in flight — a ~27× token cut over re-reading history |
| **Ingest** | Documents, PDFs, audio and session transcripts, enriched by workers off the request path |
| **Code graph** | Per-repo graph of functions/imports/call edges; blast radius and caller queries ([blast-radius](../functionality/blast-radius.md)) |
| **Operations** | An effect-verifying doctor, receipted forward-only migrations, retention enforcement |

### Why it exists (the history)

Cortex was not designed in the abstract; every principle below was paid for in
production inside Kaidera OS:

- **Postgres is the only store.** Cortex previously ran with Redis as a queue and
  cache. Epic **E006 (Cortex canonicalisation and Redis retirement)** removed it:
  queue state is rows, the event backend is `postgres`, and `/health` reports
  `event_backend=postgres`. One store, one backup contract, one thing to reason about.
- **Verify the effect, never the declaration.** Finding **F38** of the E020 appliance
  review (handoff `585fd83f`): `kos doctor` read `systemctl is-enabled` — a field that
  *cannot express failure*; a unit that exited 1 still reported `enabled`, and
  `systemctl show` on a nonexistent unit synthesised `Result=success`. Measured on
  kos26, systemd 259. This is why the doctor asks "does search return the row just
  written" and never "does the config exist".
- **Fail loud.** Rerank once sat last in a fixed search-time budget and was silently
  dropped on *every* query — search "worked", degraded, for weeks. The `/degradation`
  endpoint and the honest `degraded[]` list in search responses exist so that class of
  silence cannot recur.
- **It became a product the same way OpenKai did.** Handoff `cfa51cc9` (E021) records
  the CTO directive (2026-08-31): *split Cortex out of KOS into its own open-source
  product, same as OpenKai; KOS stays commercial and consumes both as pinned modules.*
  The E020 epic (v0.2.003 OS-Independent Appliance, handoff `585fd83f`) had just
  delivered the full containerised appliance — one artifact that starts with no source
  tree present, health-gated, on rootless podman or Apple Container — so the extraction
  inherits a proven runtime, not a rewrite.

### The six-layer appliance

Cortex deploys as six layers, health-gated in order. Nothing mutates Postgres except
the API and its migrations; workers are enrichment, not request-path.

| # | Layer | Job | Talks to |
|---|---|---|---|
| 1 | `db` | Postgres 18 + pgvector 0.8.2 — the single source of truth | — |
| 2 | `migrate` | One-shot schema custody: forward-only, receipted migrations | Postgres |
| 3 | `cortex-api` | FastAPI. Memory, handoffs, registry, search, boot context, doctor, the discovery manifest | Postgres |
| 4 | `embed-worker` | Embedding enrichment for memory + ingest rows | Postgres |
| 5 | `graph-worker` | Graph enrichment: entities, relations, per-repo code graphs | Postgres |
| 6 | `pdf-worker` | Document/PDF ingest: extract, chunk, store | Postgres |

The API accepts writes immediately and the workers catch up — logging a decision is
never blocked on an embedding call.

### The CLI

The 72 `cortex-*` commands are thin HTTP clients of `cortex-api` — if you cannot do
something through the API, that is a tooling gap to file, not a reason to touch
Postgres. The commands you will use daily:

| Group | Commands |
|---|---|
| Session | `cortex-boot`, `cortex-log`, `cortex-search`, `cortex-state`, `cortex-brief` |
| Work | `cortex-handoff`, `cortex-board`, `cortex-work-product`, `cortex-evidence` |
| Team | `cortex-projects`, `cortex-init-project`, `cortex-add-agent`, `cortex-roster`, `cortex-remove-agent` |
| Memory | `cortex-memory`, `cortex-entities`, `cortex-history`, `cortex-diary`, `cortex-invalidate` |
| Ingest | `cortex-ingest-artifact`, `cortex-ingest-session`, `cortex-ingest-all`, `cortex-ingest-memories` |
| Code graph | `cortex-graph-build`, `cortex-graph-blast`, `cortex-graph-callers`, `cortex-graph-search`, `cortex-graph-stats` |
| Ops | `cortex-verify`, `cortex-diagnose`, `cortex-maintain`, `cortex-backup`, `cortex-embed`, `cortex-apply-migrations` |

---

## 2. How it works

### Architecture: the six-layer appliance with data flow

```mermaid
flowchart TB
    subgraph clients["Clients (any harness)"]
        CLI["cortex-* CLI<br/>(72 commands)"]
        AGT["Agents: Claude Code, Codex,<br/>OpenKai, KOS console"]
        MCP["MCP server"]
    end

    subgraph appliance["Cortex appliance (one compose stack)"]
        direction TB
        API["3 · cortex-api — FastAPI<br/>memory · handoffs · registry · search<br/>boot · doctor · /.well-known/cortex"]
        MIG["2 · migrate — one-shot<br/>forward-only, receipted"]
        subgraph workers["Enrichment workers (async, off request-path)"]
            EW["4 · embed-worker<br/>memory + ingest embeddings"]
            GW["5 · graph-worker<br/>entities, relations, code graphs"]
            PW["6 · pdf-worker<br/>document/PDF ingest"]
        end
        DB[("1 · db — Postgres 18 + pgvector 0.8.2<br/>memory · coordination · registry · ingest<br/>queue state is rows")]
        MIG --> DB
        API --> DB
        EW --> DB
        GW --> DB
        PW --> DB
    end

    subgraph providers["Model providers (API plane)"]
        EMBP["embedding provider<br/>Ollama · NVIDIA · OpenRouter"]
        RRP["rerank provider<br/>NVIDIA · Cohere · OpenRouter"]
        LLMP["analysis LLM<br/>OpenRouter free chain · Anthropic"]
    end

    CLI -->|HTTP :8501| API
    AGT -->|HTTP :8501| API
    MCP --> API
    API -->|embed query| EMBP
    API -->|rerank| RRP
    EW -->|embed rows| EMBP
    GW -->|entity extraction| LLMP
    API -->|session analysis| LLMP
```

Startup order is 1 → 2 → 3 → 4·5·6, health-gated at each step: a service is up when its
probe answers, not when its container starts. Only `cortex-api` is published to the host
(`127.0.0.1:8501`); the workers and Postgres stay on the internal network.

### Handoff lifecycle

A handoff is a **durable unit of work with a lifecycle**, not a message. Claim is a
compare-and-swap: a 409 means a live sibling holds it — stop, do not force.

```mermaid
stateDiagram-v2
    [*] --> pending: create (summary, context,<br/>acceptance gates, priority)
    pending --> claimed: claim (atomic CAS;<br/>409 names the holder)
    claimed --> pending: release (reason recorded)
    claimed --> completed: complete (evidence required)
    claimed --> failed: fail (terminal reason)
    claimed --> abandoned: abandon (terminal reason)
    pending --> consult: consult handoff<br/>(blocked → concrete options)
    consult --> pending: answered
    completed --> [*]
    failed --> [*]
    abandoned --> [*]
```

The discipline that makes this work (all learned the expensive way):

- **Claim is start-of-work, not credit.** Claim before editing anything; keep edits
  scoped to the claimed handoff.
- **Complete only with evidence.** `--acceptance` names the gates at creation;
  `--evidence` records measurements. "Tests: 668 passed" beats "done".
- **Acceptance is durable on both sides.** Reviewers adjudicate completion against the
  acceptance the handoff was *created* with — moving goalposts is visible.

### Memory pipeline: ingest → embed → search → retrieve

```mermaid
flowchart LR
    subgraph write["Write path (never blocked on enrichment)"]
        W1["cortex-log decision/lesson<br/>cortex-ingest-* document/session"] --> APIW["cortex-api<br/>stores row immediately"]
        APIW --> PGW[("Postgres")]
        PGW -.->|enrichment queue rows| EW["embed-worker<br/>embed → vector"]
        PGW -.-> GW["graph-worker<br/>entities + relations"]
        EW --> PGW
        GW --> PGW
    end
    subgraph read["Read path: /search"]
        Q["query"] --> FP{"exact-ID<br/>fast path?"}
        FP -->|yes| OUT
        FP -->|no| S1["BM25 (tsvector)"]
        FP -->|no| S2["trigram"]
        FP -->|no| S3["pgvector"]
        FP -->|no| S4["graph expansion"]
        S1 & S2 & S3 & S4 --> RRF["RRF fusion (k=60)"] --> RR["rerank<br/>(or say it didn't)"] --> OUT["ranked answer + honest degraded[]"]
    end
```

Two production lessons are designed in: **rerank must run or say it didn't** (the
`/degradation` endpoint exists because silent rerank loss once degraded search for
weeks), and **provider model ids are exact strings** — OpenRouter free-tier ids need
the `:free` suffix or every call fails. The embedding provider is resolved per call
from the platform config row (see [§4](#4-providers-configuration)), with env as the
fallback.

### Boot flow: the 27× token cut

Re-reading a project's history at every session start is the most expensive way to
remind an agent who it is. Boot replaces archaeology with one call:

```mermaid
sequenceDiagram
    participant A as Agent (session start)
    participant CLI as cortex-boot
    participant API as cortex-api
    participant PG as Postgres

    A->>CLI: cortex-boot kai
    CLI->>API: GET /boot/kai (X-Project)
    API->>PG: L0 identity · L1 facts · L2 recent history
    PG-->>API: persona · rules · roster · pending/claimed handoffs<br/>quality decisions · degradation alerts · work-product briefs
    API-->>CLI: boot context (budgeted, ~1.2k tokens)
    CLI-->>A: who I am · what is in flight · what to do next
```

The response carries the versioned persona contract (`cortex.persona.v2`): identity
text, skill manifest, project rules, pending handoffs, and the harness adapter.
Measured across the L0+L1+L2 boot stack this is a **~27× compression** against
session-start context from raw history. `cortex-boot kai --query "<topic>"` adds topic
recall to the same call.

### Multi-agent coordination

Cortex's model is a **team of named workers inside a project**, not a swarm of
anonymous processes. Work moves by handoffs; memory moves by decisions and lessons;
the orchestrator keeps both flowing when no human is watching.

```mermaid
flowchart LR
    subgraph team["project: my-product"]
        KAI["kai@my-product<br/>role: lead"]
        REN["ren@my-product<br/>role: reviewer/CPO"]
        QA["qa@my-product<br/>role: specialist"]
        ORC["beat@my-product<br/>role: orchestrator"]
    end
    PG[("Cortex<br/>memory + handoffs")]
    KAI -->|"handoff: review the split boundary<br/>(to_role: cpo)"| PG
    PG -->|"claim (CAS)"| REN
    REN -->|"complete + evidence"| PG
    KAI -->|"log decision (evidence: ...)"| PG
    QA -->|"search → finds the decision"| PG
    ORC -.->|"watches event stream, routes,<br/>nudges stalled claims"| PG
```

Two conventions keep routing honest: **every routing responsibility resolves to
exactly one worker** (ambiguous ownership is an audit failure), and **the orchestrator
is not a handoff target** — it routes and watches; it does not own deliverables.

### Provider setup flow

Provider configuration has two planes — most confusion comes from mixing them up:

```mermaid
flowchart TB
    subgraph p1["Plane 1 — your agents (their own subscriptions)"]
        CC["Claude Code (Claude Pro/Max)"]
        CX["Codex CLI (ChatGPT sub)"]
        OK["OpenKai"]
    end
    subgraph p2["Plane 2 — Cortex enrichment (API providers)"]
        direction TB
        CFG{"How is the provider<br/>settings file authored?"}
        KOSUI["KOS console UI (:8765)<br/>Settings → writes via OpenKai"]
        OKF["OpenKai installed →<br/>OpenKai authors the file"]
        STND["Standalone → Cortex soft-prompts once,<br/>materialises the identical file on skip"]
        FILE[("ONE provider settings file<br/>ONE schema, ONE location<br/>+ provenance receipt beside it")]
        ENV["API keys as deployment env:<br/>OPENROUTER_API_KEY · NVIDIA_API_KEY ·<br/>COHERE_API_KEY · ANTHROPIC_API_KEY"]
        API["cortex-api + workers read<br/>platform config row (DB) → env fallback"]
    end
    CC & CX & OK -->|cortex-* CLI / HTTP| API
    CFG --> KOSUI & OKF & STND
    KOSUI --> FILE
    OKF --> FILE
    STND --> FILE
    FILE --> ENV --> API
```

The rules that hold regardless of path:

- **The file is the contract.** One file, one schema, one location — byte-identical
  whether OpenKai wrote it or Cortex materialised it from its pinned template. An
  existing file wins, always: OpenKai installed later *adopts* the file and becomes
  its author. Provenance lives in a receipt beside the file, never as an extra key
  inside it.
- **Keys live in env / the settings file — never in code, never in Cortex's
  database.** Provider *selection* and *model* live in the `cortex_platform_config`
  row (API-owned operational state, changeable centrally without rebuilding
  containers); the *secret* comes from process env only.
- **Never hand-maintain two copies.** Two credential planes once let an operator
  watch a key **Test succeed** in KOS Settings while every Cortex embedding failed —
  both sides internally correct, and not the same plane. This flow exists so that
  cannot happen.

---

## 3. How to set up

### Path A — Standalone (no KOS, no OpenKai)

**What to set up:** the compose stack, an env file, and one provider key. This is the
whole job.

#### Step 1 — Prereqs

- **Linux:** rootless podman ≥ 5.0, cgroup manager `systemd` — [install-linux](../install-linux.md)
- **macOS:** Apple Container (the installer installs it if missing) — [install-macos](../install-macos.md)
- One containerisation technology per machine — never mix engines on a host.

#### Step 2 — The env file

Cortex takes configuration from environment variables, conventionally loaded from an
env file the compose file references (`env_file:`). Create `.env` beside your compose
checkout (in Kaidera OS this file is `local-cortex/.env` — the canonical home for
local-only secrets):

```bash
# ── Required: an enrichment provider key (pick at least one) ─────────────
OPENROUTER_API_KEY=sk-or-...          # one key, every provider (recommended)
# NVIDIA_API_KEY=nvapi-...            # free tier: embeddings + rerank
# COHERE_API_KEY=...                  # rerank
# ANTHROPIC_API_KEY=...               # analysis LLM (optional)

# ── Embedding (write-path enrichment + query embedding) ──────────────────
CORTEX_EMBED_PROVIDER=openrouter      # openrouter | nvidia | openai
CORTEX_EMBED_MODEL=nvidia/llama-nemotron-embed-vl-1b-v2:free
CORTEX_EMBED_DIMS=768

# ── Rerank (search quality; disable only knowingly) ──────────────────────
CORTEX_RERANK_ENABLED=true
CORTEX_RERANK_PROVIDER=nvidia         # nvidia | cohere | openrouter
CORTEX_RERANK_MODEL=nv-rerank-qa-mistral-4b:1

# ── Analysis LLM (session analysis, graph extraction) ────────────────────
# Leave CORTEX_ANALYSIS_MODEL empty to use the free fallback chain (tried in
# order on 429): nvidia/nemotron-3-super-120b-a12b:free, google/gemma-4-31b-it:free,
# minimax/minimax-m2.5:free, openai/gpt-oss-120b:free
CORTEX_ANALYSIS_MODEL=
CORTEX_ANALYSIS_PROVIDER=openrouter   # openrouter | anthropic
# CORTEX_ANALYSIS_FALLBACK_MODELS=    # comma-separated override of the chain

# ── Database (defaults are in-network service names — keep them) ─────────
CORTEX_PG_DSN=postgresql://postgres:postgres@cortex-pg:5432/platform_agent_memory
CORTEX_PG_DSN_APP=postgresql://cortex_app:cortex_app@cortex-pg:5432/platform_agent_memory
CORTEX_PG_DSN_ADMIN=postgresql://postgres:postgres@cortex-pg:5432/platform_agent_memory

# ── Usually leave alone ──────────────────────────────────────────────────
CORTEX_EVENT_BACKEND=postgres         # the only valid value
CORTEX_MIGRATIONS_DIR=/app/migrations
CORTEX_VECTOR_PRECISION=float32       # float32 | halfvec (~60% smaller index, <1% recall delta)
CORTEX_LOCAL_STATE_PROJECT=_local_state
CORTEX_SHARED_KNOWLEDGE_PROJECT=_global
# CORTEX_GRAPH_LLM_ENABLED=true       # LLM-assisted entity extraction (off by default)
# CORTEX_REQUIRE_RLS=true             # fail boot unless the cortex_app RLS pool exists
# CORTEX_ADMIN_TOKEN=                 # generate a secret; empty fails closed
# CORTEX_JWT_SECRET=                  # + CORTEX_AUTH_REQUIRE_JWT=true for bearer-token auth
```

Notes on the important ones:

- **Provider keys are read from process env only** — `OPENROUTER_API_KEY`,
  `NVIDIA_API_KEY` (or `NVIDIA_NIM_API_KEY`), `COHERE_API_KEY`, `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`. They are deployment secrets, never stored in Cortex memory.
- **Model ids are exact strings.** The `:free` suffix on OpenRouter free-tier ids is
  part of the id; without it every call fails.
- **`CORTEX_EMBED_DIMS` must match the model's output.** A dimension mismatch is
  detected per call and the row is counted as `dimension_mismatch` in metrics — check
  before a backfill, not after.
- The compose file deliberately does **not** put keys in `environment:` — setting a
  key there would override `env_file` with an empty string when the host shell has not
  sourced it.

#### Step 3 — Bring up the stack

```bash
podman compose -f docker-compose.cortex.yml up -d   # db → migrate → api → workers
curl -s http://localhost:8501/health | python3 -m json.tool
```

A healthy response shows `postgres: connected`, `event_backend: postgres`, and echoes
your `embed_provider`/`embed_model`. Fresh deploys bootstrap the schema explicitly —
the initdb script applies the complete captured schema and creates the `cortex_app`
role once, on an empty volume, and the Postgres healthcheck gates the API until that
finishes.

#### Step 4 — Point your shell at it

```bash
export CORTEX_URL=http://127.0.0.1:8501     # where the API answers
export CORTEX_PROJECT=my-project            # which project this shell acts in
export CORTEX_TOKEN=<bearer>                # stored hashed server-side
```

The CLI resolves these; nothing else is required for a tool to become Cortex-aware.
Fail closed on identity: no `CORTEX_PROJECT` → stop, never guess a default.

#### Step 5 — Verify the effect

```bash
cortex-doctor    # schema receipt matches, search answers a row just written,
                 # enrichment queues drain, oldest row within retention policy
```

A configured-looking env file that nothing consumes is the most expensive failure
mode we know — the doctor exists to catch it in the first five minutes.

### Path B — With KOS (Kaidera OS, UI access)

KOS runs Cortex as a module: each Cortex release ships a versioned, hash-pinned
artifact the KOS appliance vendors at image build (no network at build time,
provenance receiptable), and the console gives you the operator UI.

- **Console UI at `http://127.0.0.1:8765`** — served by the console service, loopback
  only. It talks to Cortex over the internal network (`CORTEX_API_URL=http://cortex-api:8501`).
- **Settings page** — the provider plane lives here. Per the E020 directive
  (*"the provider implementation of OpenKai wins, there should be no other provider
  implementation at all in the KOS"*), the console renders a UI over OpenKai's
  provider registry and stores nothing of its own: one settings file, one author.
- **FileVault / Workspace** — the console's workspace browser reads project files
  host-side (the containerised console is opt-in via
  `docker compose --profile containerized up console`; the redistributable runs the
  console natively so two consoles never collide on :8765).
- **The KOS appliance owns host lifecycle** — volumes, backup, upgrade. One owner per
  fact: Cortex owns memory and coordination, the deployment owns the substrate it
  lives in.

Nothing in this path changes how agents use Cortex: same API, same CLI, same
`worker@project` identity.

### Path C — With OpenKai (TUI access)

OpenKai is the terminal-first agent harness, and the provider authority:

1. **OpenKai authors the central provider settings file.** Cortex (and KOS, and any
   other reader) reads that one file. If you install OpenKai after a standalone
   Cortex, it *adopts* the existing file and becomes its author — nothing else
   changes.
2. **Agent identity and boot context are native.** OpenKai workers boot with
   `cortex-boot`, act as `worker@project`, and their skills manifest rides the
   `cortex.persona.v2` boot contract — the skill registry (`cortex-skill`,
   `POST /skills`, `/skills/{slug}/bind`) is served by Cortex and selected on demand.
3. **The CLI is the integration.** OpenKai calls the same 72 `cortex-*` commands over
   `CORTEX_URL` + `CORTEX_TOKEN`; there is no private channel.

The seam is designed so readers never care who wrote the settings — OpenKai present
or absent, the file at the path is byte-schema-identical.

---

## 4. Providers configuration

Cortex calls model APIs itself in three places. All three resolve their **provider
and model from the `cortex_platform_config` row** (patched centrally via
`PATCH /admin/cortex/config`, cached 30 s), falling back to env when the table is
absent or unreachable. **Keys always come from env.**

### Embedding providers

Used twice: per row at write time (embed-worker, async) and per query at search time.

| Provider | `CORTEX_EMBED_PROVIDER` | Endpoint | Key env var | Notes |
|---|---|---|---|---|
| OpenRouter | `openrouter` (default) | `https://openrouter.ai/api/v1/embeddings` | `OPENROUTER_API_KEY` | One key, many models; `:free` suffix on free tiers |
| NVIDIA NIM | `nvidia` | `https://integrate.api.nvidia.com/v1/embeddings` | `NVIDIA_API_KEY` | Strong free tier (e.g. `nv-embedqa` family) |
| OpenAI | `openai` | `https://api.openai.com/v1/embeddings` | `OPENAI_API_KEY` | |
| Local ONNX | — (container) | `embed-worker :9005/embed` | none | `sentence-transformers/all-mpnet-base-v2`, ~200 MB, offline; compose profile `local-embed` |

Ollama and other OpenAI-compatible servers work the same way: base URL + model id +
empty key. Right when data cannot leave the machine; the trade-off is that backfills
are CPU/GPU-bound on your hardware. Embedding compatibility is dimensional — the
store is 768-d, so switching models means matching `CORTEX_EMBED_DIMS` and a backfill
(`cortex-embed`), whose *effect* is verified: a `--dry-run` proves nothing.

### Rerank providers

Re-orders vector candidates by actual relevance — the difference between "similar
words" and "the right answer".

| Provider | `CORTEX_RERANK_PROVIDER` | Endpoint | Key env var | Default model |
|---|---|---|---|---|
| NVIDIA | `nvidia` (default) | `https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking` | `NVIDIA_API_KEY` | `nv-rerank-qa-mistral-4b:1` |
| Cohere | `cohere` | `https://api.cohere.com/v2/rerank` | `COHERE_API_KEY` | |
| OpenRouter | `openrouter` | `https://openrouter.ai/api/v1/rerank` | `OPENROUTER_API_KEY` | free-tier ids need `:free` |

Disable only knowingly: `CORTEX_RERANK_ENABLED=false`. Search still answers, but the
`/degradation` endpoint and `degraded[]` flags will (honestly) tell you quality is
down.

### Analysis LLM providers

Session analysis (patterns used/failed, quality scoring) and optional LLM-assisted
graph entity extraction (`CORTEX_GRAPH_LLM_ENABLED=true`).

| Provider | `CORTEX_ANALYSIS_PROVIDER` | Key env var | Notes |
|---|---|---|---|
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` | Default; free-model **fallback chain** tried in order on 429 — override with `CORTEX_ANALYSIS_FALLBACK_MODELS` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | Auto-selected when only `ANTHROPIC_API_KEY` is set |

Set `CORTEX_ANALYSIS_MODEL` for a single pinned model (no fallback); leave it empty
for the chain. With neither key configured, analysis endpoints fail loud with a 502
naming the missing key — never silent degradation.

### The platform config row

Operators change ingestion models centrally — no container rebuild, no env drift:

```bash
curl -s http://localhost:8501/admin/cortex/config -H "X-Cortex-Admin-Token: $CORTEX_ADMIN_TOKEN"
curl -s -X PATCH http://localhost:8501/admin/cortex/config \
    -H "X-Cortex-Admin-Token: $CORTEX_ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d '{"embedding_model": "nvidia/llama-nemotron-embed-vl-1b-v2:free", "rerank_enabled": true}'
```

Patchable keys: `embedding_provider`, `embedding_model`, `embedding_dims`,
`rerank_enabled`, `rerank_provider`, `rerank_model`, `analysis_provider`,
`analysis_model`, plus the tuning knobs `search_confidence_threshold`, `rrf_k`,
`embed_input_max_chars`, `rerank_input_max_chars`, and per-purpose timeouts. The
selection lives here; the secret never does.

---

## 5. Workflow examples

### A — Create a project, register agents, start a handoff

```bash
export CORTEX_PROJECT=my-product

# 1. Register the project (isolation boundary: own memory, roster, handoffs)
cortex-init-project my-product --workspace-root ~/work/my-product

# 2. Register workers — identity is name@project, no restart needed
cortex-add-agent kai --role lead
cortex-add-agent ren --role reviewer

# 3. Boot — confirms identity and empty queues
cortex-boot kai

# 4. Create a handoff with acceptance gates
cortex-handoff --create --confirm --from kai --from-role lead --to reviewer --to-agent ren \
    --priority high \
    --summary "Review the queue design" \
    --context "Postgres-only queues, see docs/design/04" \
    --acceptance '{"gate":"ratify the boundary or return with named defects"}'

# 5. The reviewer picks it up
cortex-handoff --mine ren
cortex-handoff --claim <id>          # BEFORE touching any code
# ... work ...
cortex-handoff --complete <id> --evidence '{"tests":"668 passed","grep":"0 direct DB sites"}'
```

### B — Ingest documents into memory, search them

```bash
# Point ingestion at an explicit corpus directory — never a repo root or $HOME
cortex-ingest-artifact --project my-product --dir ~/work/my-product/specs

# Ingest is idempotent by content; re-runs do not duplicate
# Enrichment (embed + graph) lands asynchronously in the workers

# Verify the effect: an ingest is done when search RETURNS the content
cortex-search "retention policy for lessons"
cortex-search "585fd83f"                    # exact-ID fast path
cortex-search "queue design" --type decisions --limit 5
```

### C — A multi-agent team with different roles

```bash
cortex-add-agent kai  --role lead           # owns the user conversation, integration
cortex-add-agent ren  --role reviewer       # adversarial: tries to break claims
cortex-add-agent qa   --role specialist     # QA/docs/whatever the project needs
cortex-add-agent beat --role orchestrator   # routes, watches, never owns deliverables

cortex-roster                                # registry data; add/remove any time
```

Conventions to adopt from day one: every routing responsibility resolves to exactly
one worker; the orchestrator is not a handoff target; memory rows state identity and
evidence in the text itself (`kai@my-product chose X; evidence: ...`) because rows
outlive their context.

### D — Migrate from a dev to a production Cortex instance

The contract is: **one Postgres database, dumpable; migrations forward-only and
receipted; the host owns backup.**

```bash
# 1. On the dev host — dump the single database (verify by content, not exit code:
#    an exit-0 backup once produced 0 bytes)
pg_dump "$CORTEX_PG_DSN" --format=custom --file=cortex-dev.pgdump
pg_restore --list cortex-dev.pgdump | head     # prove it has content

# 2. On the production host — bring up the stack (schema bootstraps on the empty
#    volume), then restore
pg_restore --dbname="$PROD_CORTEX_PG_DSN" cortex-dev.pgdump

# 3. Apply any newer migrations through the API-owned boundary
cortex-apply-migrations
curl -s http://localhost:8501/admin/migrations -H "X-Cortex-Admin-Token: $CORTEX_ADMIN_TOKEN"

# 4. Re-embed only if you changed provider/model; then verify the effect
cortex-embed --project my-product
cortex-doctor
```

Skipping intermediate patch versions is supported — a newer migrator adopts exactly
the inventory the old receipt represented, then applies only genuinely newer files.
Downgrades are not supported: data rollback means restoring a dump, an explicit
operator act.

---

## 6. Limits (honest)

- **Search quality tracks your provider.** The free tiers are real but rate-limited;
  large backfills feel it. The local ONNX path trades quality for privacy and zero
  cost. A rerank provider is not optional if you want search that works — and the
  store is 768-d, so embedding models must match `CORTEX_EMBED_DIMS`.
- **Static analysis is a floor.** The code graph cannot see dynamic dispatch,
  reflection, or string-built imports; a blast radius is a lower bound. A stale graph
  lies — check `cortex-graph-stats` build age before trusting a radius.
- **Enrichment is eventually consistent.** Writes return immediately; embeddings and
  graph edges land in workers after. Search right after a write may miss the row
  until the queue drains — `cortex-doctor` shows the backlog.
- **The standalone packaging is the v0.1.0 target.** The commands and routes here run
  in production inside Kaidera OS today; the standalone compose + wheel land with the
  extraction. This guide describes the contract they land under.
- **Cortex does not spawn agents.** It is the memory and coordination plane; the
  harness (OpenKai, KOS, Claude Code, Codex) owns the process. Backups, volumes and
  host lifecycle belong to the deployment, not to Cortex.
- **Identity is convention-plus-enforcement, not SSO.** Bearer tokens (hashed
  server-side), optional JWT (`CORTEX_AUTH_REQUIRE_JWT`), and loopback-by-default.
  TLS off-loopback and login on human surfaces are deployment concerns — see
  [SECURITY.md](../../SECURITY.md).

## 7. Sources

- E020 epic — *v0.2.003 OS-Independent Appliance*: `Program/Release_v0.1.0/E020_V02003_OS_INDEPENDENT_APPLIANCE/EPIC_SPEC.md` (kaidera-os, range `468b53f7..97f856ea`); the CTO directives of 2026-08-19/20 quoted there.
- Handoff `585fd83f` (kai→ren, 2026-08-31) — E020 full adversarial + functional review: findings F38 (`systemctl is-enabled` cannot express failure; measured on kos26, systemd 259) and F39/F40 (prune orphaned networks; networks as a third resource kind). Live Cortex, project `kaidera-os`.
- Handoff `cfa51cc9` (kai→ren, 2026-08-31) — E021 P0: the Cortex split boundary; CTO directive 2026-08-31 to split Cortex out as an independent MIT product consumed as a hash-pinned artifact, the OpenKai pattern. Live Cortex, project `kaidera-os`.
- API surface: `.agents/api/main.py` (≈20.7k lines, 123 routes), `.agents/api/README.md`, `.agents/scripts/` (72 executables; 81 command/support files), `.agents/docker-compose.cortex.yml` in kaidera-os.
- The 27× boot compression figure: `local-cortex/README.md`, `local-cortex/ARCHITECTURE.md` (L0+L1+L2 boot stack), and the E65 tuning-knobs spec.
- Companion docs: [architecture](../architecture.md) · [discovery](../discovery.md) · [models](../models.md) · [providers-standalone](../providers-standalone.md) · [deployment](../deployment.md) · [functionality index](../functionality/README.md).
