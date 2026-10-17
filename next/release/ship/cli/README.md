# ship/cli

Puts the frozen Cortex clients (`cortex-boot`, `cortex-log`, `cortex-search` from
`packages/cli/`, unchanged) on PATH behind one wrapper that always and only reaches this host's v2.

    install_cli.sh --prefix ~/.local/share/kaidera/cortex-v2 --client-dir <P>/libexec/cli \
        --projector <cox config-client launcher> --link-dir ~/.local/bin [--no-link]

Tests: `python3 -m pytest next/release/ship/cli/tests` (fake client and projector; no network).
Contract and refusals: `contract.md`.
