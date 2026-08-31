# Handoffs

A handoff is a **durable unit of work with a lifecycle**, not a message. It survives
sessions, restarts and model changes.

## Lifecycle

```
create ──▶ pending ──▶ claimed ──▶ completed
                │          │
                └──────────┴──▶ released (back to pending, with a reason)
```

```bash
cortex-handoff --create --confirm --from kai --from-role lead --to cpo --to-agent ren \
    --priority high --summary "Review the split boundary" \
    --context "..." --acceptance '{"gate":"what done means"}'
cortex-handoff --mine ren        # what is addressed to me
cortex-handoff --claim <id>      # take it — claim BEFORE touching code
cortex-handoff --complete <id>   # only with evidence
cortex-handoff --release <id> --reason "blocked on X"
```

## Discipline that makes this work (all learned the expensive way)

- **Claim is start-of-work, not credit.** Claim before editing anything; keep edits scoped
  to the claimed handoff.
- **Complete only with evidence.** Acceptance criteria are written at creation; completion
  states what was measured, not what is believed. "Tests: 466 passed" beats "done".
- **A claim conflict is an alarm.** A 409 on claim means a live sibling holds it — stop,
  don't force.
- **Blocked?** Create a consult handoff with concrete options rather than going quiet.
- **No relevant work?** Idle cleanly. Inventing work pollutes the queue.
- **Reported blockers are claims to test**, not facts to relay.

## Acceptance and evidence

`--acceptance` is a JSON object naming the gates; `--evidence` records measurements.
Reviewers adjudicate a completion against the acceptance the handoff was *created* with —
moving goalposts is visible because both are durable.
