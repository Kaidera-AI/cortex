# Install on macOS (Apple Container)

> v0.1.0 target contract. The stack runs in production on macOS under Apple Container
> today; the one-command installer packages that setup.

One containerisation technology per machine: on macOS that is **Apple Container**, at
the **latest stable version** — the installer never mixes engines on a host.

## What the installer does

```bash
curl -fsSL https://raw.githubusercontent.com/Kaidera-AI/cortex/main/install.sh | sh
```

1. **Installs Apple Container if missing** (Apple's open-source `container` tool), and
   starts its services.
2. **Deploys the six-layer Cortex appliance**: `db` → `migrate` → `cortex-api` →
   `embed-worker` · `graph-worker` · `pdf-worker`.
3. **Soft-prompts for OpenKai** (skippable). Installed → it authors the central provider
   settings file. Skipped → Cortex materialises the identical file at the identical path
   from its pinned template. Either way every reader sees one file, one schema.
4. **Runs the doctor** and refuses to call the install done until effects verify: schema
   receipt written, search answers, queues drain.
5. **Prints the discovery URL** — `http://127.0.0.1:<port>/.well-known/cortex` — which is
   the machine-readable "what can this Cortex do".

## Platform notes (measured, not assumed)

Apple Container differs from Compose-on-Linux in ways the installer must own, all hit in
production:

- **No Compose restart policies** — the installer wires restart supervision itself.
- **Named volumes are exclusive** to one container at a time.
- **ext4 volumes contain `lost+found`** — content checks must expect it.
- **Peer DNS is not active from configuration alone** — services address each other by
  resolved address, and the doctor *verifies resolution answers* rather than trusting that
  a domain is registered.
- **No privilege, ever.** Nothing in install or repair may require sudo or a password; every
  path is user-owned. A repair loop that needs root is a design bug here.

## After install

```bash
cortex-doctor           # re-verify any time
cortex-init-project …   # docs/guides/create-a-project.md
```

## Alternative engine: rootless Podman on macOS (measured)

Rootless Podman is a working macOS engine, proven by the production Apple Container →
Podman migration of 2026-09-01 (Podman 6.0.2, `applehv`, 6 CPU / 10 GiB / 100 GiB —
full data fidelity). In the kaidera-os installer it is selected explicitly with
`KAIDERA_CONTAINER_ENGINE=podman`; auto-selection is unchanged (macOS auto still
resolves to the default engine). The Podman ≥ 5.0 floor from
[install-linux.md](install-linux.md) applies equally on macOS.

Measured macOS-specific requirements, all hit in production:

- **Init the machine explicitly** — `podman machine init --provider applehv --memory
  10240 …`; the defaults (libkrun, 2 GiB) hung silently on first boot, and a
  never-booted machine can fail Ignition outright.
- **Set the default connection before starting** — `podman system connection default
  <name>`, or `podman machine start` blocks forever on an interactive prompt.
- **One VM at a time** — Podman runs a single machine per host.
- **Boot persistence is launchd, not systemd** — the installer-generated systemd unit
  is Linux-only; on macOS a launchd agent reconciles the stack.

Migrating an existing Apple Container deployment:
[guides/migration-apple-to-podman.md](guides/migration-apple-to-podman.md).
