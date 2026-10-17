# Retained regression floor

`release_profile.profile(root, baseline_manifest)` returns the retained 107 proposed
floor identities, 32 additional generated crypto cases, 81 real-PG requirements,
and current prior RUN/exclusion inventory. Its result always says
`qualification=inventory_only`, `passed=0`. It never runs PostgreSQL or credits a
skip as an executed assertion. The 22 MOVE rows remain required in A.

The profile is pinned to the accepted source map and exact retained test-file bytes.
The original source bodies remain at their existing locations. All prior manifest
RUN entries, exclusions and control metadata must remain accounted for; a changed
admission/control needs an explicit reviewed profile update. Nothing is retired,
physically moved, duplicated or reduced to the proposed 107.

`oracle_controls(completed_process, expected_nodes)` wraps the existing classifier
with strict JSON, exact unique selected-node/count checks and required skip/xfail/
unexpected-success rejection. Unrelated or duplicate failures, non-BODY assertions,
errors, timeouts, crashes and absent/malformed reports are inconclusive.

```sh
python -m unittest discover -s next/tests/floor/tests -v
```

Pinned map evidence:
[accepted floor map](/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/mike-tests-design-20261017/FLOOR-MAP.json).
Real-PG floor execution, crypto-provider qualification, installed smoke and paired
host release admission remain day 2. This metadata is test tooling, excluded from
product images. Shared manifest admission belongs to the integration owner.
