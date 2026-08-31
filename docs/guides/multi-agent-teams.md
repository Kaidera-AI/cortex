# Multi-agent teams

Cortex's model is a **team of named workers inside a project**, not a swarm of anonymous
processes.

## Identity

Every worker is `name@project` — `kai@kaidera-os`, `ren@kaidera-os`. That identity appears
on every memory row, every handoff, every log line. Accountability is a feature: six months
later you can ask *who decided this and why* and get an answer.

## Roles and the roster

The roster is registry data (add a worker any time, no restart). Typical shape:

| Role | Job |
|---|---|
| `lead` | owns the user conversation, integration, final calls |
| reviewer / CPO | adversarial review — tries to break claims before they ship |
| specialists | full-stack, QA, docs — whatever the project needs |
| `orchestrator` | continuous custody: watches the event stream, routes handoffs, nudges stalled work, runs scheduled platform-health tasks |

Two conventions that keep routing honest, learned in production:

- **Every routing responsibility resolves to exactly one worker.** Ambiguous ownership is
  an audit failure, not a flexibility feature.
- **The orchestrator is not a handoff target.** It routes and watches; it does not own
  deliverables. Give it responsibilities and the routing audit goes ambiguous.

## How work moves

Work moves by [handoffs](handoffs.md), not by chat. Memory moves by
[decisions and lessons](memory.md), not by tribal knowledge. The orchestrator keeps both
flowing when no human is watching.

## Why this shape

A single agent forgets; a swarm can't be held to account. Named workers + durable memory +
explicit handoffs give you the properties of a good human team: continuity, accountability,
and the ability to disagree productively (a reviewer role exists to refute, not confirm).
