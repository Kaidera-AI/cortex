# ship/cli

Puts the frozen Cortex clients (`cortex-boot`, `cortex-log`, `cortex-search` from
`packages/cli/`, unchanged) on PATH behind one launcher that always and only reaches this host's
v2 through Core's private client projection.

    install_cli.sh --prefix ~/.local/share/kaidera/cortex-v2 --client-dir <P>/libexec/cli \
        --core-python <installed Core interpreter> --config <vault>/config/cortex.json \
        --link-dir ~/.local/bin [--no-link]

Each run needs an explicit unlock: `CORTEX_UNLOCK_FD=<fd> CORTEX_KEY_ID=<id> cortex-boot ... <fd><key`.
Tests: `python3 -m pytest next/release/ship/cli/tests` (actual Core operator and custody with public
fixture bytes, fake client; no network). Contract and refusals: `contract.md`.
