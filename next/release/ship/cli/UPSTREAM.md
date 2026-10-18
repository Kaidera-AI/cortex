# ship/cli upstream

- Clients: finn's `next/modules/cli-v2` commands `cortex-v2-boot`, `cortex-v2-log`,
  `cortex-v2-search` and their `_cortex_api.sh` (ours, MIT), which reads the seven-field frame from
  `CORTEX_CLIENT_PROJECTION_FD`; v1 `packages/cli` stays at its main bytes and is not used.
- Projector: Core's `python -m cortex_core.runtime.operator config client` (cox 090698743d9c, ours,
  MIT) on the installed Core interpreter and its qualified cryptography closure (nix).
- The launcher is Python standard library only (`-IS`) on that same interpreter; bash 3.2+ and
  POSIX tools (`sed`, `ln`) for the installer. No Python package, uv or interpreter bootstrap is added.
