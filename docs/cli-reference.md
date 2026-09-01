# Cortex CLI reference

> **v0.1.0 extraction status:** this is the measured command-file surface in Kaidera OS
> today. Extraction into this standalone repository is in progress. The names and
> invocations below describe the v0.1.0 target contract; they do **not** claim that these
> files are already installed from this repository.

## Scope and counting

The measured inventory is **81 files**:

- **72 executable `cortex-*` commands** intended as the supported user, agent or operator
  surface;
- **seven internal support libraries/workers**, which are implementation details and must
  not be presented as commands; and
- **two retired, fail-closed shims**, retained only to reject obsolete direct-worker paths.

The 2026-08-31 census table listed 78 files. The live `cortex-state` command reconciled
that snapshot to 79; multimodal productionisation then added `cortex-ingest-image` and
`cortex-ingest-video`. Current measured source therefore contains 81 files. The two new
commands share the container-backed media dispatcher and do not add another extraction
implementation.

Core invocations show the shortest useful form, not every option. Run `--help` after the
v0.1.0 extraction lands for the complete parser contract.

## Public executable commands (72)

### Handoffs and work-product lookup

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-handoff` | Create, list, show, claim, return, release, fail or abandon a project-scoped handoff. | `cortex-handoff --mine <agent>`; `cortex-handoff --claim <id>`; `cortex-handoff --return <id>` |
| `cortex-evidence` | Create and validate a seven-gate handoff evidence bundle with residual risk recorded. | `cortex-evidence --agent <name> --summary <text> --residual-risk <text> [--gate implementation=complete]` |
| `cortex-brief` | Find existing work-product memory before rediscovering files, symbols or handoff context. | `cortex-brief <query>` or `cortex-brief --file <path>` |

### Beat feed and dashboards

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-tail` | Read recent team events once or follow the admin-gated event feed. | `cortex-tail --once` or `cortex-tail --follow` |
| `cortex-progress-dashboard` | Compatibility entry point that renders the current Markdown dashboard, optionally on a watch interval. | `cortex-progress-dashboard [--watch <seconds>]` |
| `cortex-dashboard-md` | Render the fail-loud Cortex Markdown dashboard through the private Python renderer. | `cortex-dashboard-md` |

### Boot, persona and onboarding

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-boot` | Print token-budgeted boot context for an agent in the current project. | `cortex-boot <agent> [--budget <n>]` |
| `cortex-bootstrap` | Fetch the fuller project bootstrap context for an agent. | `cortex-bootstrap <agent> [--budget <n>]` |
| `cortex-onboard` | Onboard an agent or inspect onboarding and handoff-closure diagnostics. | `cortex-onboard <agent>`; `cortex-onboard --verify-closure` |
| `cortex-discipline` | Print the project-isolation protocol and optionally inject its daily decision into bootstrap context. | `cortex-discipline [--daily-inject]` |
| `cortex-deciption` | Backwards-compatible typo alias that delegates to the discipline command. | `cortex-deciption [--daily-inject]` |

### Search and retrieval

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-search` | Run project-scoped hybrid search across durable memory, with optional type, rerank and graph controls. | `cortex-search <query> [--type <type>] [--limit <n>]` |
| `cortex-search-soak` | Run the incident-912073e2 concurrent search/disconnect acceptance gate and inspect service logs for crash signatures. | `cortex-search-soak --project <key> --duration 600` |

### Knowledge graph (Layer 4 entities)

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-entities` | List, search and traverse typed knowledge-graph entities. | `cortex-entities --search <name>` or `cortex-entities --graph <name>` |
| `cortex-extract-entities` | Extract entities from durable memory through the API; dry-run unless apply is selected. | `cortex-extract-entities --source <source> --dry-run` |
| `cortex-graph-extract` | Public alias for entity extraction. | `cortex-graph-extract [cortex-extract-entities options]` |
| `cortex-graph-search` | Query dual-level Layer 4 retrieval, with high/low and relationship expansion controls. | `cortex-graph-search <query> [--expand] [--high|--low]` |

### Code graph (repository AST and call graph)

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-graph-build` | Build or import a repository graph, synchronously or as a pollable job. | `cortex-graph-build --repo <path> [--full]` |
| `cortex-graph-blast` | Compute transitive blast radius from changed files or named targets. | `cortex-graph-blast <file> [--depth <n>] [--max-results <n>]` |
| `cortex-graph-callers` | Find callers of a function or class. | `cortex-graph-callers <symbol> [--repo <path>]` |
| `cortex-graph-impact` | Produce risk-scored impact for a commit or working diff. | `cortex-graph-impact [<commit>] [--repo <path>]` |
| `cortex-graph-large-fn` | Find oversized function/class hotspots. | `cortex-graph-large-fn [--min <n>] [--kind Function|Class]` |
| `cortex-graph-stats` | Read project-scoped managed graph node and edge statistics. | `cortex-graph-stats [--json]` |
| `cortex-graph-prune` | Report stale graph-volume project directories, then remove them only with explicit apply. | `cortex-graph-prune [--keep <project>] [--apply]` |

### Embeddings

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-embed` | Inspect coverage and backfill missing embeddings by table, synchronously or as a job. | `cortex-embed --stats`; `cortex-embed --table all [--limit <n>]` |

### Session, memory and artifact ingestion

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-ingest-all` | Sweep new local harness sessions through typed ingest helpers with bounded error handling. | `cortex-ingest-all [--limit <n>]` |
| `cortex-ingest-session` | Ingest one Claude-style JSONL session. | `cortex-ingest-session <jsonl-file> [agent] [project]` |
| `cortex-ingest-codex` | Ingest one Codex JSONL session. | `cortex-ingest-codex <jsonl-file> [agent] [project]` |
| `cortex-ingest-beat-sessions` | Ingest Beat PI and harness session logs through the sessions API. | `cortex-ingest-beat-sessions` |
| `cortex-ingest-claude-local-state` | Import Claude plans, todos and IndexedDB cache state into global knowledge. | `cortex-ingest-claude-local-state [--dry-run]` |
| `cortex-ingest-memories` | Ingest a bounded Markdown memory corpus through typed endpoints. | `cortex-ingest-memories [--path <dir>] [--limit <n>]` |
| `cortex-ingest-artifact` | Persist a non-chat artifact with explicit source type and optional tenancy metadata. | `cortex-ingest-artifact <path> [agent] [project]` |
| `cortex-ingest-audio` | Transcribe audio, or import a supplied transcript, as a durable artifact. | `cortex-ingest-audio <audio-path> [agent] [project]` |
| `cortex-ingest-image` | Describe an image through the internal vision worker and persist it as a durable artifact. | `cortex-ingest-image <image-path> [agent] [project] [--model <name>]` |
| `cortex-ingest-video` | Extract and transcribe a video's audio track through the internal audio worker, then persist it. | `cortex-ingest-video <video-path> [agent] [project] [--model <name>]` |
| `cortex-save-chat` | Save an atomic session/message/event/knowledge checkpoint summary. | `cortex-save-chat <agent> <topic> <summary>` |
| `cortex-rebuild-history` | Replace project chat history from local Claude and Codex transcript sources. | `cortex-rebuild-history [--dry-run]` |
| `cortex-history` | Display recent project message history with agent, count and date filters. | `cortex-history [--agent <name>] [--last <n>]` |

### Durable memory writes

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-log` | Write a typed team event and, by default, read it back for exact confirmation. | `cortex-log <agent> <event-type> <summary> [files...]` |
| `cortex-memory` | Create or update a durable project knowledge section. | `cortex-memory --agent <name> --section <title> --content <text>` |
| `cortex-diary` | Read, write and summarise an agent diary. | `cortex-diary <agent>`; `cortex-diary <agent> write --summary <text>` |
| `cortex-work-product` | Write or list work-product receipts, files and symbols. | `cortex-work-product --write --agent <name> --title <text> --summary <text>` |
| `cortex-invalidate` | Supersede a decision, lesson or handoff, or undo that invalidation. | `cortex-invalidate <id> [--superseded-by <id>]`; `cortex-invalidate <id> --undo` |

### Verification and memory quality

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-verify` | Check a claim against local files/environment, the code graph, decisions or table counts with verified/contradicted/unverifiable exits. | `cortex-verify --file-exists <path>`; `cortex-verify --callers <symbol> --min <n>` |
| `cortex-memory-audit` | Hash an agent profile and audit stored memory for profile drift. | `cortex-memory-audit hash --agent <agent>`; `cortex-memory-audit audit [--strict] [--notify]` |

### Projects, state and portability

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-init-project` | Register a project and optional initial roster. | `cortex-init-project <project-key> [--root <path>] [--agent <name:role>]` |
| `cortex-project` | Set a registered project’s canonical repository root. | `cortex-project --set-repo-root <absolute-path> [--project <key>]` |
| `cortex-projects` | List projects visible through the typed project API. | `cortex-projects` |
| `cortex-export-project` | Export one project’s named-field data to a private JSON file. | `cortex-export-project <project-key> [--output <file>]` |
| `cortex-import-project` | Import an export into a pre-registered target, refusing existing data unless explicitly allowed. | `cortex-import-project <target-key> <export.json> [--allow-existing]` |
| `cortex-merge-projects` | Merge one or more source projects into a target; dry-run is available. | `cortex-merge-projects [--dry-run] <target> <source> [source...]` |
| `cortex-state` | Print current project state from project-scoped `GET /state`. | `cortex-state` |

### Agent roster and identity

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-add-agent` | Register or reactivate an agent in the current project’s roster. | `cortex-add-agent <name> <role> [--model <model>]` |
| `cortex-remove-agent` | Deactivate an agent from one roster while preserving its history. | `cortex-remove-agent <agent> [--project <key>]` |
| `cortex-roster` | Show the current project’s registered agents. | `cortex-roster` |
| `cortex-roster-cleanup` | Inspect and demote phantom roster rows or stale project mirrors without deleting history. | `cortex-roster-cleanup [--project <key>|--all] [--dry-run]` |
| `cortex-maintain-agents` | Align visible roster entries with canonical profiles for one or all projects. | `cortex-maintain-agents [--project <key>|--all] [--dry-run]` |
| `cortex-reconcile-identities` | Collapse alias identities into canonical agent names. | `cortex-reconcile-identities [--project <key>|--all] [--dry-run]` |
| `cortex-registry-doctor` | Inspect registry-health diagnostics and apply only doctor-listed cleanup with confirmation. | `cortex-registry-doctor [--clean --confirm] [--json]` |

### Task board

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-board` | List, add, update or complete project task-board items. | `cortex-board list`; `cortex-board add --title <title> [--agent <name>]` |

### Skills

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-skill` | Install, register, list, bind and deprecate skills by scope. | `cortex-skill install <url-or-path> --scope <scope>`; `cortex-skill list` |

### Harness generation and cutover

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-sync-generate-harness` | Deterministically generate a project’s `.agents/` mirror from Cortex; diff is the safe default. | `cortex-sync-generate-harness <project-key> --diff` |
| `cortex-sync-workspace` | Ingest on-disk workspace projects, profiles and sessions into Cortex. | `cortex-sync-workspace [--profiles-only|--sessions-only]` |
| `cortex-harness-cutover` | Drive foundation or full preflight → backup → generate → apply → verify cutover. | `cortex-harness-cutover foundation`; `cortex-harness-cutover <project-key>` |
| `cortex-harness-doctor` | Detect stale or mismatched generated harness mirrors. | `cortex-harness-doctor [--root <path>] [--expect-project <key>]` |
| `cortex-harness-rollback` | Restore a timestamped harness backup created by apply. | `cortex-harness-rollback <project-key> [<backup-timestamp>]` |

### Operations, migrations, backup and retention

| File | Purpose | Core invocation |
|---|---|---|
| `cortex-diagnose` | Inspect whether the local environment can reach the configured Cortex data layer. | `cortex-diagnose` |
| `cortex-backup` | Create an engine-agnostic deployment backup of databases, files and configuration, or a selected subset. | `cortex-backup [--full|--db-only|--files-only]` |
| `cortex-retain` | Run, preview or inspect project retention and safe archival. | `cortex-retain --dry-run`; `cortex-retain --status` |
| `cortex-maintain` | Run daily ingest, embedding, entity extraction and freshness maintenance, or one selected phase. | `cortex-maintain`; `cortex-maintain --stats` |
| `cortex-migrate` | Perform the one-time migration of file-based memory into Postgres. | `cortex-migrate [--dry-run]` |
| `cortex-apply-migrations` | List, preview or apply checked-in SQL migrations through the admin API. | `cortex-apply-migrations --list`; `cortex-apply-migrations --apply` |

## Internal support files: libraries and workers (7)

These files are not public executables. Public commands source or invoke them; external
callers should use the corresponding `cortex-*` command or typed API instead.

| File | Internal role |
|---|---|
| `_cortex_api.sh` | Shared typed HTTP client, project/agent headers, URL encoding and separately gated admin-token calls; ordinary calls do not send admin credentials. |
| `_cortex_env.sh` | Resolves runtime configuration and the active project from `runtime.yaml`, then loads the local Cortex environment when present. |
| `_cortex_lib.sh` | Shared workspace resolution, project-scope guards and legacy/common shell helpers used by command wrappers. It is sourced, not invoked. |
| `_cortex_claude_local_state.py` | Private worker used by the Claude-local-state wrapper to collect plans, todos and IndexedDB state for ingestion. |
| `_cortex_beat_session_ingest.py` | Private Beat PI/harness session parser and sessions-API ingest worker. |
| `_cortex_entity_extract.py` | Private legacy entity-extraction implementation module. Its direct database entry point now refuses; public extraction goes through the API-backed entity commands. |
| `cortex_dashboard_md.py` | Dependency-light Markdown dashboard renderer invoked by the public dashboard wrappers; preserves last-known-good output when live data is unavailable. |

## Retired fail-closed shims (2)

These are deliberately non-functional compatibility sentinels. Both print the supported
replacement and exit 2; neither calls a provider or database.

| File | Why retained | Replacement |
|---|---|---|
| `_cortex_embed_batch.py` | Prevents use of the retired direct embedding-backfill worker. | `cortex-embed --table all --limit 100` |
| `_cortex_graph_search.py` | Prevents host-side direct graph search from bypassing the API boundary. | `cortex-graph-search <query>` |

## Boundaries and honest limitations

- The 72 executables mix agent-facing, user-facing and operator/admin commands. “Public”
  means supported command surface, not that every command is safe without the role and
  confirmation documented by its parser.
- The seven support files are discoverable here so packagers can account for the complete
  payload. They are not a second command surface and must not be placed on a user’s PATH as
  documented entry points.
- The two retired shims are part of the measured file count but not capabilities.
- Several mutating commands are dry-run by default or require `--confirm`, `--apply` or an
  admin token. The concise examples above do not weaken those fail-closed controls.
- This reference inventories the measured production source. Until v0.1.0 extraction is
  complete, standalone installers and packages must not claim these commands are present.

## Sources

- Complete 2026-08-31 surface census and its 2026-09-01 multimodal correction:
  `Program/Release_v0.1.0/E021_CORTEX_INDEPENDENT_PRODUCT/FUNCTIONALITY_CENSUS.md`,
  Appendix 2.
- Current measured command files and parser/header contracts: Kaidera OS
  `.agents/scripts/cortex-*` (72 files), plus the nine separately classified files named
  above.
- Follow-on command evidence: `.agents/scripts/cortex-state` (project-scoped `GET /state`)
  and the container-backed `.agents/scripts/cortex-ingest-{image,video}` wrappers.
- API-only coordination boundary and state route: functionality census Appendix 3,
  **API-only coordination boundary**; `.agents/api/main.py`.
