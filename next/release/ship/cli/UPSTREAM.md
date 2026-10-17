# ship/cli upstream

- Clients: `packages/cli/cortex-boot`, `cortex-log`, `cortex-search` and `_cortex_api.sh` (ours,
  MIT), taken unchanged at the release commit by nix's build; `_cortex_api.sh` (finn 491d795c13)
  reads the seven-field frame from `CORTEX_CLIENT_PROJECTION_FD`.
- Projector: Core's `python -m cortex_core.runtime.operator config client` (cox 090698743d9c, ours,
  MIT) on the installed Core interpreter and its qualified cryptography closure (nix).
- The launcher is Python standard library only (`-IS`) on that same interpreter; bash 3.2+ and
  POSIX tools (`sed`, `ln`) for the installer. No Python package, uv or interpreter bootstrap is added.
