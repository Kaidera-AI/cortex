# @kaidera-ai/cortex — the installer/launcher

One dependency-free launcher behind every public install channel, so channel behaviour can
never diverge:

```bash
brew install kaidera-ai/kaidera/cortex     # Homebrew (formula lands with v0.1.0)
npx  @kaidera-ai/cortex preflight          # npm      (published with v0.1.0)
bunx @kaidera-ai/cortex preflight          # bun      (same package; CI-gated)
```

## Commands

- `cortex preflight [--json]` — **works today.** Checks this machine against the deployment
  contract and reports honestly: macOS wants Apple Container and refuses a second engine;
  Linux wants rootless podman >= 5.0, cgroup `systemd`, linger — named refusals, never a
  degraded install.
- `cortex install` — **refuses loudly until the v0.1.0 payload exists.** A published
  launcher never pretends, and it will never deploy a stack it cannot verify by digest.
- `cortex version` / `cortex help`

## Publish gates

npm publish and the brew formula land only WITH the v0.1.0 release, after maintainer go.
Every channel must resolve to the same payload digest; a channel that cannot prove the
digest refuses to install.
