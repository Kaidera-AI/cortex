# ship/cli upstream

- Clients: `packages/cli/cortex-boot`, `cortex-log`, `cortex-search` and `_cortex_api.sh` (ours,
  MIT), taken unchanged at the release commit by nix's build; the wrapper only pins their inputs.
- bash 3.2+ and POSIX tools (`sed`, `stat`, `ln`, `tr`), OS-provided. No Python package, uv or
  interpreter bootstrap is added.
