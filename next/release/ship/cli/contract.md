# ship/cli contract (option A, .004 day 1)

Owner bob@helix. Accepted plan: DAY-1 + INDEX v2 (J06, J11). Interface receipt; changes are a consult.

## Wrapper

One template, `wrapper.sh.in`, rendered once per command (`cortex-boot`, `cortex-log`,
`cortex-search`). Placeholders: `@CLIENT@` (the frozen client), `@PROJECTOR@` (the client
projection command), `@EMPTY_AGENTS@` (an empty private folder).

In order, the wrapper:
1. Unsets `CORTEX_ADMIN_TOKEN CORTEX_CTO_OVERRIDE CORTEX_API_WITH_ADMIN CORTEX_API_URL CORTEX_API
   CORTEX_PROJECT CORTEX_PROJECT_ID CORTEX_PROJECT_ROOT CORTEX_WORKSPACE_PROJECT CORTEX_CREDENTIAL_FILE`.
2. Runs the projector (cox's `cortex config client --config FILE` on host python3) and reads its
   stdout through a pipe. It must print exactly these three lines, in this order:

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
`url_not_v2:<url>` (not `http://127.0.0.1:<port>`, or port 8501, the v1 fallback),
`credential_missing:<path>`, `credential_unsafe:<path>` (symlink, not regular, or not mode 0600).

## Install

    install_cli.sh --prefix P --client-dir DIR --projector CMD --link-dir DIR

Renders `P/bin/<command>` from the template. If any command name already resolves on `PATH` to a file
that is not our link, it refuses `path_collision:<path>` and links nothing. `--no-link` keeps the
wrappers in `P/bin` only.

## Open joins

- finn: the credential variable name (`CORTEX_CREDENTIAL_FILE` is provisional) and the transport
  that sends it.
- cox: the projector command and its three-line output above.
