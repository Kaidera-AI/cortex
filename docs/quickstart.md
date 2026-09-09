# Quickstart

> **v0.1.002 (partial).** These commands run in production inside Kaidera OS today. The
> standalone stack cannot be started yet — see
> [Installing the launcher](../README.md#installing-the-launcher) for what does work now.
> This page is the contract the complete release lands under.

## 0. Get the launcher

```bash
npx  @kaidera/cortex preflight --json          # npm (Node >= 18)
bunx @kaidera/cortex preflight --json          # bun
brew install kaidera-ai/kaidera/cortex && cortex preflight --json
```

## 1. Bring up the stack

```bash
# v0.1.002: cortex install still refuses (exit 2) — no published payload yet (roadmap
# item 3). This step becomes real with that release.
cortex-doctor               # verifies effects: schema receipt, search answers, queues drain
```

## 2. Create a project and a worker

```bash
export CORTEX_PROJECT=my-project
cortex-init-project my-project --workspace-root ~/work/my-project
cortex-add-agent alice --role lead
```

## 3. Boot, remember, coordinate, find

```bash
cortex-boot alice                                  # who am I, what is in flight
cortex-log alice decision "alice@my-project chose Postgres-only queues; evidence: load test"
cortex-handoff --create --confirm --from alice --from-role lead --to lead \
    --summary "Review the queue design"
cortex-handoff --mine alice                        # claim/complete lifecycle from here
cortex-search "queue design"                       # returns the decision just logged
```

Identity is always `worker@project`. Every command is an HTTP client of `cortex-api` —
if you can't do something through the API, that's a bug to file, not a reason to touch
Postgres.
