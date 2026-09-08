# Quickstart

> **v0.1.001 (partial).** These commands run in production inside Kaidera OS today. The
> standalone stack cannot be started from this checkout yet — see
> [Running it](../README.md#running-it--not-yet-from-this-checkout). This page is the contract
> the complete release lands under.

## 1. Bring up the stack

```bash
# v0.1.001: no supported start from this checkout yet (no images, no install manifest);
# the runnable payload is v0.1.002 item 3. This step becomes real with that release.
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
