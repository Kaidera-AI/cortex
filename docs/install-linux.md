# Install on Linux (rootless podman)

> v0.1.0 target contract. This mirrors the hardened Linux appliance path Cortex ships
> inside today.

One containerisation technology per machine: on Linux that is **rootless podman**,
at the **latest stable version** (the `>= 5.0` floor below is a refusal line, not a
target — install current stable).

## Requirements (fail loud, checked up front)

- `podman >= 5.0` — `podman update --restart` does not exist before it; Ubuntu 24.04 ships
  4.9.3, which is **too old**. The installer refuses rather than degrading. (Measured
  good: 6.0.2 rootless, crun/overlay — the version the 2026-09-01 macOS migration
  validated end to end; the same floor now applies to the supported
  [Podman-on-macOS path](install-macos.md#alternative-engine-rootless-podman-on-macos-measured).)
- cgroup manager **`systemd`** — without it healthcheck timers never schedule and every
  `service_healthy` condition waits forever.
- Rootless, with **linger enabled** and `podman-restart.service` enabled, so the stack
  survives reboot without a login.

## What the installer does

```bash
curl -fsSL https://raw.githubusercontent.com/Kaidera-AI/cortex/main/install.sh | sh
```

1. Verifies every requirement above and **names the missing piece exactly** on failure.
2. Deploys the six layers: `db` → `migrate` → `cortex-api` → `embed-worker` ·
   `graph-worker` · `pdf-worker`, health-gated in order.
3. OpenKai soft-prompt / identical-file fallback (same contract as macOS).
4. Arms reboot recovery, then **verifies the arming took effect** — reading not just
   "enabled" (a unit that ran and failed still reports enabled) but the last run's actual
   result. Reboot recovery that is broken must be loud *now*, not at the next boot.
5. Doctor + discovery URL, as on macOS.

## After install

Same as macOS: `cortex-doctor`, then [create a project](guides/create-a-project.md).
