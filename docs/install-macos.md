# Install on macOS (rootless Podman machine)

> v0.1.001 (partial). There is no one-command installer channel yet: the stack is built
> from source by the lifecycle launcher — see [Run from source](../README.md#run-from-source).
> Apple Container was removed on 2026-09-01 and is not supported.

One containerisation technology per machine: on macOS that is **rootless Podman** running
in a Podman machine, at the **latest stable version**. The `>= 5.0` floor from
[install-linux.md](install-linux.md) applies equally here.

## Requirements (measured in production, not assumed)

Rootless Podman on macOS was proven by the production Apple Container → Podman migration of
2026-09-01 (Podman 6.0.2, `applehv`, 6 CPU / 10 GiB / 100 GiB — full data fidelity).

- **Init the machine explicitly** — `podman machine init --provider applehv --memory
  10240 …`; the defaults (libkrun, 2 GiB) hung silently on first boot, and a never-booted
  machine can fail Ignition outright.
- **Set the default connection before starting** — `podman system connection default
  <name>`, or `podman machine start` blocks forever on an interactive prompt.
- **One VM at a time** — Podman runs a single machine per host.
- **Boot persistence is launchd, not systemd** — on macOS a launchd agent reconciles the
  stack; systemd units are Linux-only.
- **No privilege, ever.** Nothing in install or repair may require sudo or a password; every
  path is user-owned. A repair loop that needs root is a design bug here.
- `podman-compose` and Python `>= 3.10` on the host, for the launcher.

## What the launcher will do (not available in v0.1.001)

`packages/deploy/cortex-runtime` is a prebuilt-runtime launcher: it acquires verified images
from an image lock, reads an install manifest, and refuses to build source images by design.
v0.1.001 ships neither the lock nor the manifest and publishes no images, so **there is no
supported way to start the stack from this checkout yet** — an earlier recipe on this page
was wrong and is withdrawn (see the README). When the payload ships (v0.1.002), the launcher:

1. **Acquires the images** named by the image lock and verifies them against
   `packages/deploy/release.json`.
2. **Deploys the appliance**: TLS init → `db` → `migrate` → `cortex-api` → `provider` →
   `embed-worker` · `graph-worker` · `pdf-worker`, health-gated in order.
3. **Checks effects**: schema receipt written, API healthy with Postgres connected. Stdout
   is one receipt; diagnostics go to stderr.
4. **Refuses rather than degrades** when a prerequisite is missing.

The Node launcher in `packages/installer` (`cortex preflight`, `cortex install --payload …`)
wraps the same lifecycle; `install` needs a published release manifest, which v0.1.001 does
not ship — it refuses by design.

## After install

```bash
cortex-doctor           # re-verify any time
cortex-init-project …   # docs/guides/create-a-project.md
```

Migrating an existing Apple Container deployment:
[guides/migration-apple-to-podman.md](guides/migration-apple-to-podman.md).
