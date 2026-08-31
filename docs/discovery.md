# Discovery: how a project finds Cortex and learns what it can do

A project — a human, a harness, an agent team — must be able to answer three questions
without reading source code:

1. **Is there a Cortex here?**
2. **What can it do?**
3. **How do I, an agent, use it?**

This is the v0.1.0 discovery contract. Layers 1–2 are new surface; layers 3–4 exist in
production today and are carried over.

## 1. Liveness — `GET /health`

Answers "is there a Cortex here" with a plain 200 and nothing else. Load balancers and
installers use this; agents use the next layer.

## 2. Capability manifest — `GET /.well-known/cortex`

One JSON document that makes a running Cortex self-describing:

```json
{
  "product": "cortex",
  "version": "0.1.0",
  "api_revision": 1,
  "openapi_url": "/openapi.json",
  "auth": "bearer-token",
  "features": {
    "memory": true, "handoffs": true, "registry": true,
    "search": {"semantic": true, "rerank": true, "graph": true},
    "ingest": {"documents": true, "pdf": true, "sessions": true}
  },
  "endpoints": {
    "projects": "/projects", "boot": "/boot/{worker}",
    "handoffs": "/handoffs", "search": "/search", "doctor": "/doctor"
  }
}
```

Rules: the manifest never lies — a feature is listed only if its check passes at startup;
consumers pin `api_revision` and refuse a major they do not understand; the full surface is
the OpenAPI document, the manifest is the index to it.

## 3. Agent boot context — `cortex-boot <worker>`

The agent-facing answer to "how does this work". Boot returns who the worker is
(`worker@project`), the project's rules, the roster, claimed and pending handoffs, and
recent decisions — everything needed to act, nothing that requires archaeology.

## 4. Generated harness instructions

Cortex generates the instruction files coding harnesses already read (`CLAUDE.md`,
`GEMINI.md`, agent rule files) from the registry — identity, commands, project rules. The
project directory tells the agent how to reach Cortex because **Cortex wrote that file**,
regenerated whenever the registry changes. No hand-maintained agent docs.

## Environment convention

```bash
CORTEX_URL=http://127.0.0.1:8000    # where the API answers
CORTEX_PROJECT=<project-key>        # which project this shell acts in
CORTEX_TOKEN=<bearer>               # stored hashed server-side
```

The CLI resolves these; nothing else is required for a tool to become Cortex-aware.
