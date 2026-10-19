# CI evidence controls

Owner: Mike; accountable lead: Cox. Scope is source/unit evidence admission for Option A v0.1.004. These controls cannot issue release GO.

Use both existing Python 3.13.7 and 3.14.0 runtimes. The unchanged strict per-file worker records every collected setup/call/teardown phase. Missing dependencies, skips, deselection, phase errors and changed sources refuse. Keep complete raw attempts and exact source hashes.

```sh
python .github/scripts/pytest_next_offline.py --inventory-only --output FRESH_OUTPUT
python -m unittest discover -s next/tests/ci_gates/tests -v
python -m next.tests.ci_gates.receipt composition COMPOSITION_PACKET.json
python -m next.tests.ci_gates.receipt collect COLLECTION_PACKET.json
python -m next.tests.ci_gates.receipt history HISTORY_PACKET.json
python -m next.tests.ci_gates.pilot mutate --root REPOSITORY --spec SPEC.json --output FRESH_OUTPUT --scratch FRESH_SCRATCH
python -m next.tests.ci_gates.receipt mutation MUTATION_PACKET.json
```

Repeat `--spec` for every changed source. Output and scratch must be fresh project output/tmp homes outside the repository. Causal children use a minimal environment without credentials and never mutate the author tree. A source comparator with peer bytes composed is not an accepted policy base or an installed candidate.

Current Day-2 admission includes the actual runtime catalog/lifecycle/status suites, actual seed-owner contract and Nix pure catalog/package/provision suites as RUN. The actual seed suite, seed/operator join and eight versioned CLI transport/namespace/health suites have explicit loopback homes. The v2 source is `next/modules/cli-v2`; the original v1 helper and `packages/api/tests/test_mcp_live.py` remain unchanged. No projection injection supplies v1 authority. Loopback execution uses the required Linux namespace with only lo, no-new-privileges, both runtimes and the unchanged 180-second file ceiling. Local loopback fixtures provide source evidence only.

The owned runner reuses Nix's actual `next/release/ship/install/package_closure.py` to expose `cortex_ship.install.roles` to source-only child interpreters. It checks the exact copied file set, receipt digests and each policy payload against the current source bytes. A modified policy with a rewritten copied receipt still refuses. This copied namespace is labelled SOURCE_COPIED_POLICY_ONLY; it supplies no native or installed authority and introduces no alternate policy implementation.

`pytest-pg` retains separate required installer-role and installer-ledger steps after earlier failures unless cancelled. Their source-pinned exact method sets are nine role methods and eleven ledger/provisioning methods. The existing private native-cluster worker keeps the 180-second cap, strict phases and owned cleanup. The closed worker environment selects only `api-replay-0004` (ten real migrations); full21/pgvector remains a separate gate. Credentials and caller migration selectors are not inherited. Both ledger suite and its imported Cluster are pinned. Synthetic receipt controls grant source/unit admission evidence only.

Writer controls retain precise per-site classification as well as whole-file pins: actual seed has four bootstrap writes following private PREPARED custody, and three closed identity reads. Changed query bytes, source aliases and added writes must refuse even when a whole-file checksum is renewed. No generic SELECT/getattr bypass is permitted.

Read-only CI collection preserves every failed run/attempt and does not close history_snapshot_drift. The unavailable real OpenAPI/runtime export remains bound_spec_complete=spec_incomplete with zero mutation credit. Source success is distinct from hosted green, native PostgreSQL, image installation, paired identity, smoke and release acceptance. Current owner source REDs must remain visible.
