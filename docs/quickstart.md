# Quickstart

> **v0.1.0 target.** These commands run in production inside Kaidera OS today; the
> standalone packaging (compose + wheel) lands with the extraction. This page is the
> contract it lands under — if v0.1.0 cannot do this, v0.1.0 is not done.

## 1. Bring up the stack

```bash
podman compose up -d        # db → migrations → api → workers
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
