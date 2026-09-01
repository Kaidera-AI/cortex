# Functionality reference

One document per functionality, each built from the real history of the systems Cortex was
extracted from: **what it is, why it exists** (the incident or need that forced it, cited),
**how it works** (a mermaid diagram of the actual flow), **how to use it**, **what to set
up**, and **honest limits**.

The measured surface being documented: **79 command/support files (70 executable commands),
121 API routes.** The workstream is done when that inventory and this index agree — zero
undocumented commands.

`shipped` below is the **reference document's** status, not a claim that every capability
is installed from this standalone checkout. The production lineage is shipped; v0.1.0 OSS
extraction remains in progress, and discovery plus the published payload are authoritative
for standalone availability.

| # | Doc | Covers | Reference status |
|---|---|---|---|
| 1 | [memory.md](memory.md) | verbatim store; decisions/lessons/knowledge; diary; chat→LTM; lineage; invalidation; audit | shipped |
| 2 | [handoffs.md](handoffs.md) | **atomic claim (CAS)**; informative 403/409 naming the claimer; **release/handback**; atomic completion handback; retry/requeue; create-dedupe; evidence bundles + quality classes; cross-project relay | shipped |
| 3 | [search.md](search.md) | hybrid BM25+trigram+vector+graph+RRF; rerank; exact-ID fast path; request budget + honest `degraded[]`; bulkhead; soak gate | shipped |
| 4 | [embeddings.md](embeddings.md) | write-path enrichment; degraded mode; backfill; halfvec; recall gate; keyless local search | shipped |
| 5 | [knowledge-graph.md](knowledge-graph.md) | entities/relationships (dual-level); LLM extraction + dedup; 2-hop; A/B-validated hybrid rule; salvage cleanup | shipped |
| 6 | [blast-radius.md](blast-radius.md) | code graph, blast radius, callers, impact, hotspots | shipped |
| 7 | [work-products.md](work-products.md) | completion receipts; `cortex-brief` no-re-discovery contract; freshness + supersession | shipped |
| 8 | [registry.md](registry.md) | projects; identity `agent@project`; roster; runtime authority; export/import/merge | shipped |
| 9 | [boot.md](boot.md) | tiered boot context (27× token cut); persona contract + provenance; imperative next-action | shipped |
| 10 | [harness-integration.md](harness-integration.md) | generated harness mirrors; cutover/rollback/doctor; skills registry + on-demand selection; MCP | shipped |
| 11 | [ingest.md](ingest.md) | sessions/transcripts (idempotent, fail-closed); artifacts + edges; multimodal workers | shipped |
| 12 | [operations.md](operations.md) | effect-verifying doctor; storage profiler; retention; migrations; backup; bounded admin SQL | shipped |
| 13 | [orchestrator-feed.md](orchestrator-feed.md) | dispatchable/stale queues; SSE events; fail-loud dashboards | shipped |
| 14 | [autonomy.md](autonomy.md) | dispatch funnel; watchdog with escalation backoff; propose mode; **responsibility routing**; worktree isolation — how handoff+handback becomes full autonomy | shipped |
| 15 | [security.md](security.md) | hashed tokens; RLS + two-pool split; provider-key custody; fitness gates | shipped |
| 16 | [verification.md](verification.md) | claim checks against code/knowledge/runtime; recall gates; read-back-confirmed writes | shipped |

Mining is complete: five parallel passes over six months of both source projects (git
history, documentation tree, the full CLI+API surface, the platform lineage, and agent
memory) produced ~390 cited evidence rows. Each doc above is written from that census —
which is why every "why it exists" names a real commit, incident, or measured run.

**The bar every doc must clear:** every "why" cites a commit, a memory row, or a measured
run. A doc that can't say why something exists goes back to mining, not to publication.
