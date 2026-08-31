# Functionality reference

One document per functionality, each built from the real history of the systems Cortex was
extracted from: **what it is, why it exists** (the incident or need that forced it, cited),
**how it works** (a mermaid diagram of the actual flow), **how to use it**, **what to set
up**, and **honest limits**.

The measured surface being documented: **79 CLI commands, 121 API routes.** The workstream
is done when that inventory and this index agree — zero undocumented commands.

| Area | Doc | Status |
|---|---|---|
| Code graph + blast radius | [blast-radius.md](blast-radius.md) | ✅ exemplar |
| Memory (decisions/lessons/diary/lineage) | ../guides/memory.md → deepens here | mining |
| Search (semantic + rerank + budget) | search.md | mining |
| Embeddings + backfill | embeddings.md | mining |
| Knowledge graph (entities/relations) | knowledge-graph.md | mining |
| Handoffs (state-aware lifecycle) | ../guides/handoffs.md → deepens here | mining |
| Registry (projects/roster/identity/export/import/merge) | registry.md | mining |
| Boot + onboarding | boot.md | mining |
| Harness generation + cutover | harness-integration.md | mining |
| Ingest (sessions/codex/claude-state/artifacts/audio) | ../guides/ingest.md → deepens here | mining |
| Dashboards + reporting | dashboards.md | mining |
| Doctor, degradation, recall checks | ../guides/operations.md → deepens here | mining |
| Retention + maintenance | operations | mining |
| Backup + migrations | operations | mining |
| Skills registry | skills.md | mining |
| Beat/orchestrator feed | orchestrator-feed.md | mining |
| Epics + work products | program-tracking.md | mining |

**The bar every doc must clear:** every "why" cites a commit, a memory row, or a measured
run. A doc that can't say why something exists goes back to mining, not to publication.
