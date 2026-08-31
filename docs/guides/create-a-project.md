# Create a project

A **project** is the isolation boundary: its own memory, roster, handoffs and rules. Nothing
crosses projects except by explicit, audited exception.

```bash
export CORTEX_PROJECT=my-product
cortex-init-project my-product --workspace-root ~/work/my-product
cortex-add-agent lead --role lead          # first worker; more any time, no restart
cortex-boot lead                           # confirms identity + empty queues
```

What just happened:

- The registry row for `my-product` exists — key, workspace root, scope.
- `lead@my-product` is a real identity: boot context, handoff address, memory author.
- Harness instruction files can now be generated into the workspace, so any coding agent
  opening that directory knows Cortex is there and how to use it (see
  [discovery](../discovery.md)).

Rules worth adopting from day one:

- **Fail closed on identity.** No `CORTEX_PROJECT` set → stop, never guess a default.
- **One project per product.** Do not ingest a whole machine into one project's memory.
- Names are data: workers are added via the registry, never baked into config files.
