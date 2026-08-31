# Development & ways of working

## Setup (lands with v0.1.0)

```bash
git clone https://github.com/Kaidera-AI/cortex && cd cortex
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                    # unit tier
./scripts/deploy-smoke.sh    # real-engine tier: fresh compose host, full verb cycle
```

## The two test tiers

**Unit/structural** proves shape. **The deploy smoke proves behaviour** — on a real engine,
from a fresh state: up → bootstrap → project → boot → handoff lifecycle → log → search
returns the row. A change that only keeps the first tier green is not done.

Why so blunt: the source deployment once had 191 tests and a full QA script pass **on a
tree that could not build, start, back up or restore**. Structure said yes; the engine said
no. We do not repeat that here.

## Non-negotiables

1. **Prove a test by breaking the code** — revert the fix, watch it fail, restore.
2. **Verify the effect, never the declaration** — config present ≠ config consumed;
   `rm` exit 0 ≠ thing gone; "enabled" ≠ "ran".
3. **Fitting evidence is not a proven mechanism** — measure the mechanism in isolation
   before writing the diagnosis down.
4. **One concern per commit**, conventional commits, tests ride with the change.
5. **A repair path reports whether it repaired**, not that it ran.
6. **No secrets anywhere**, including fixtures and history.

## Review

Every substantive change gets an adversarial review — a reviewer trying to refute the
claim, not confirm it. Findings come back as: the claim, the failure scenario, the
measurement that settles it.

## Releases

Semver `0.1.x`. A release = tag + wheel + changelog entry + the deploy smoke green on a
fresh host. Consumers (including Kaidera OS) pin the exact artifact by hash.
