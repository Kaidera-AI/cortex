# ship/cli contract (option A, .004 day 2)

Owner bob@helix. Accepted plan: DAY-1 + INDEX v2 (J06, J11), DAY-2 (H-D622). Joined to Core's
`runtime.operator config client` (cox 090698743d9c), finn's helper (491d795c13) and cox's ruling
`review/cortex/design/J06-process-transfer-v1.md` Q4. Changes are a consult.

## Launcher

One template, `launcher.py.in`, rendered once per versioned command (`cortex-v2-boot`,
`cortex-v2-log`, `cortex-v2-search`; Kai Option A, cox ruling
`docs/handoffs/2026-10-17_cox_a-day2-versioned-cli-ruling-v1.md`). The clients are finn's v2 module
commands (`next/modules/cli-v2`). Live v1 `cortex-boot`/`log`/`search` and `packages/cli` are never
written, linked, shadowed or changed by this module. Placeholders: `@CORE_PYTHON@` (the installed Core interpreter; also the
launcher's own `-IS` interpreter), `@CONFIG@` (the ONE vault config), `@CLIENT@` (the frozen
client), `@EMPTY_AGENTS@` (an empty private folder). Standard library only.

In order, the launcher:
1. Reads the explicit unlock selectors `CORTEX_UNLOCK_FD` (an inherited open descriptor number) and
   `CORTEX_KEY_ID`, then drops every `CORTEX_*` variable from the environment. No default, key path
   or key search: missing or unusable selectors refuse `unlock_unconfigured`.
2. Creates ONE anonymous pipe and runs `@CORE_PYTHON@ -I -B -m cortex_core.runtime.operator config
   client --config @CONFIG@ --unlock-fd N --key-id ID --projection-fd W`, passing only the write end
   and the unlock descriptor; closes its own write end; waits (15 s bound) for the actual exit.
   The projector's stdout and stderr are never forwarded.
3. On success closes the unlock descriptor and `exec`s the client with the read end inherited, its
   number in `CORTEX_CLIENT_PROJECTION_FD`, plus `CORTEX_AGENTS_DIR=@EMPTY_AGENTS@`,
   `CORTEX_RUNTIME_CONFIG=/dev/null`, `CORTEX_WORKSPACE_CONFIG=/dev/null`. The client (finn's
   helper) reads and validates the seven-field frame itself; the launcher never reads it.

It never retries and never calls another URL. No secret is in argv, environment, a file or a log.

## Typed refusals

Exit 2, stderr one JSON line `{"reason": ..., "unit": "ship/cli"}`, the client never runs:
`unlock_unconfigured`, `projection_failed:<Core reason>` (Core's one typed refusal, reduced to its
reason), `projection_failed:<exit code>` (anything else), `projection_failed:interrupted`
(timeout or interrupt), `projection_failed:unavailable` (the interpreter could not start).

## Install

    install_cli.sh --prefix P --client-dir DIR --core-python FILE --config FILE --link-dir DIR [--no-link]

Renders `P/bin/<command>` from the template, makes the empty `P/cli/agents-empty`, then links
`<link-dir>/<command>` to `P/bin/<command>`. All checks run before the first write. If a command name
already resolves on `PATH`, or exists in the link folder, as anything but our own link, it refuses
`path_collision:<path>` and writes nothing. `--no-link` skips the PATH check and keeps the launchers
in `P/bin` only.

Refusals (exit 2, same JSON line): `input_missing:<flag>`, `bad_argument:<arg>`,
`unsafe_path:<path>` (relative, or contains a quote, `$`, backquote, backslash, `|`, `&` or newline),
`client_missing:<command>`, `core_python_missing:<path>`, `link_dir_missing:<dir>`,
`path_collision:<path>`, `write_failed:<path>`.

## Open joins

- finn: the `next/modules/cli-v2` helper and `cortex-v2-*` commands (and the exact v1
  `packages/cli` restore); until they are published the launcher's end-to-end control runs only
  against finn's 491d795c helper (scratch probe, not this tree).
- nix: the installed Core interpreter with its qualified cryptography closure on both hosts, and the
  operator-facing way to supply the unlock descriptor.
- day 2, real hosts: a shebang path within the kernel limit, and the actual client commands end to end.
