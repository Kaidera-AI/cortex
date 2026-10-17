# ship/cli contract (option A, .004 day 1)

Owner bob@helix. Accepted plan: DAY-1 + INDEX v2 (J06, J11). Interface receipt; changes are a consult.

## Wrapper

Status: the argv and the three-line client wire below are PROPOSED day-2 seams (cox, rework of
row e8798066). Core's `client` mode and the projector are not implemented here; no installed credit.

One template, `wrapper.sh.in`, rendered once per command (`cortex-boot`, `cortex-log`,
`cortex-search`). Placeholders: `@CLIENT@` (the frozen client), `@PROJECTOR@` (the client
projection command), `@EMPTY_AGENTS@` (an empty private folder).

In order, the wrapper:
1. Unsets every exported `CORTEX_*` variable (admin token, CTO override, v1 URL, project, ...).
2. Runs the projector (cox's `cortex config client --config FILE` on host python3) and reads its
   stdout through a pipe. Its stderr is discarded, never forwarded: a nonzero exit gives the one
   refusal line `projection_failed:<rc>` and nothing else. It must print exactly these three keys,
   once each, nothing else:

       CORTEX_API_URL=http://127.0.0.1:<port>
       CORTEX_PROJECT=<key>
       CORTEX_CREDENTIAL_FILE=<absolute path>

3. Pins `CORTEX_AGENTS_DIR=@EMPTY_AGENTS@`, `CORTEX_RUNTIME_CONFIG=/dev/null`,
   `CORTEX_WORKSPACE_CONFIG=/dev/null`, so the helper's runtime.yaml walk-up finds nothing.
4. `exec`s the frozen client with `"$@"`.

It never retries and never calls another URL.

## Typed refusals

Exit 2, stderr one JSON line `{"reason": ..., "unit": "ship/cli"}`, the client never runs:
`projection_failed:<rc>`, `projection_field_missing:<k>`, `projection_field_unknown:<k>`,
`projection_duplicate:<k>`, `projection_field_invalid:CORTEX_PROJECT` (not `[A-Za-z0-9_-]+`),
`url_not_v2:<url>` (not `http://127.0.0.1:<port>`, or port 8501, the v1 fallback),
`credential_missing:<path>`, `credential_unsafe:<path>` (relative, symlink, not regular, not owned
by the caller, or not mode 0600).

## Install

    install_cli.sh --prefix P --client-dir DIR --projector FILE --link-dir DIR [--no-link]

Renders `P/bin/<command>` from the template, makes the empty `P/cli/agents-empty`, then links
`<link-dir>/<command>` to `P/bin/<command>`. All checks run before the first write. If a command name
already resolves on `PATH`, or exists in the link folder, as anything but our own link, it refuses
`path_collision:<path>` and writes nothing. `--no-link` skips the PATH check and keeps the wrappers
in `P/bin` only.

Refusals (exit 2, same JSON line): `input_missing:<flag>`, `bad_argument:<arg>`,
`unsafe_path:<path>` (relative, or contains a quote, `$`, backquote, backslash, `|`, `&` or newline),
`client_missing:<command>`, `projector_missing:<path>`, `link_dir_missing:<dir>`,
`path_collision:<path>`, `write_failed:<path>`.

## Open joins

- finn: the credential variable name (`CORTEX_CREDENTIAL_FILE` is provisional) and the transport
  that sends it.
- cox: the projector command and its three-line output above.
