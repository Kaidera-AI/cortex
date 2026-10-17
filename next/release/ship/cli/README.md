# ship/cli

Puts the versioned v2 clients (`cortex-v2-boot`, `cortex-v2-log`, `cortex-v2-search` from finn's
`next/modules/cli-v2`) on PATH behind one launcher that always and only reaches this host's v2
through Core's private client projection. Live v1 `cortex-*` commands are never touched.

    install_cli.sh --prefix ~/.local/share/kaidera/cortex-v2 --client-dir <P>/libexec/cli \
        --core-python <installed Core interpreter> --config <vault>/config/cortex.json \
        --link-dir ~/.local/bin [--no-link]

Each run needs an explicit unlock: `CORTEX_UNLOCK_FD=<fd> CORTEX_KEY_ID=<id> cortex-v2-boot ... <fd><key`.
Tests: `python3 -m pytest next/release/ship/cli/tests` (actual Core operator and custody with public
fixture bytes, fake client; no network). Contract and refusals: `contract.md`.
