# ship/run

Renders how Cortex v2 option A runs on a host: the serve pod (`core` + `api`, nothing else) and
the finite bootstrap plan that nix's `install_main` runs once before the first serve.

    python3 -I render.py --config <vault>/config/cortex.json \
        --operations next/contracts/runtime/runtime-operations-v1.json \
        --release <release>/target.json --core-conf <rendered core conf> --out <empty dir>

Tests: `python3 -m pytest next/release/ship/run/tests` (source fixtures only; no podman call).
Contract, field map and refusals: `contract.md`.
