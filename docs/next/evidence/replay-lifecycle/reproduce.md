# Held replay producers: accepted-pattern repair

Authority: Kai H-D45501:32; Vera accepted Mike PR35 at `4bd7717366c2a6c188805a4b4a5d0fabba1a7769`. `accepted-pattern-binding.json` binds the exact upstream pattern. Scope is these own manual producers and their evidence. The original `old-run-*` controllers and historical failed attempts are custody only and remain forbidden entry points.

```text
prepare output + acquire own local lock
create -> pending -> fresh owner/lifecycle labels -> external effect -> inspect exact name/nonce/ID
close -> inspect again -> delete own immutable IDs -> retry failed removals
      -> discard password/release lock -> inventory -> preserve receipt -> reject if unverified
```

Each producer has its own `c03-red.json` / `graph-red.json` and final GREEN extract, with hashes linking to full aggregate raw output. The corrected foreign fixture introduces a same-owner/different-lifecycle winner during CREATE after an absent precheck. Earlier `red.json` had the foreign object present before that precheck and is diagnostic history, not the intended RED. Frozen tests stay unchanged across repairs. Controller finally-block fixtures execute actual AST tails with declared state; they never synthesize an application suite success.

`green-attempt-005.json` has 21 clean controls, four actual semantic faults (pending after acknowledgement, omitted nonce check, remove by name, missing output preparation), intended3/2/1/1 body AssertionErrors/no errors, then restored 21. `red-controller-guards.json` preserves 4 tests/seven body assertions; `red-final-inventory.json` preserves 3 timeout-retention assertions. `green-attempt-004.json` preserves the NameError path caught by the frozen fixture; its passed=false is retained. `integrity-final.json` reconstructs fault patches and frozen source hashes. Later target-integration proof has its own unique attempt.

Native fault probes deliberately raise immediately AFTER a successful actual Podman create return and before ownership acknowledgement. They qualify completed effects/lost acknowledgement, not arbitrary delayed daemon commit or cancellation. Container/pod immutable deletion plus exact owner+nonce inspection preserves foreign lifecycles. Pods refuse deletion while any tracked container remains or their inventory includes anything beyond their own infra container. The lock serializes Cox producers that use it; it is not an engine-wide lock. These recipes create no password; the synthetic module control additionally proves password discard on cleanup failure.

Frozen verification: parse the receipt JSON and SHA256 index using stdlib only. Compare every recorded source hash with the published branch, every expected mutant ID with the raw `CORTEX_TEST_RESULT`, and every removal ID with its preceding fresh owner+nonce inspection. Do not execute old controllers as part of packet verification.

Fresh reproduction requires the already qualified local Podman/image environment and an owned checkout under the project `.worktrees/`. No host dependency install or product test/import is allowed. Only one own stack may run at a time, at most 2 CPU/1GiB, read-only root filesystem, no published ports or SSD binds. Use a new receipt phase on every invocation; never overwrite a prior attempt. Preserve all failures and verify owned cleanup before the next stack.

- Safety: `PYTHONDONTWRITEBYTECODE=1 python3 <canonical-evidence>/run-safety.py green-FRESH-UNIQUE`.
- C03: own detached checkout at `565227e57724468382bace3d559bdaa85310432d`, with reviewed DB wheels, legacy SQL and map mutation fixture under its ignored `tmp/`; invoke `safe-run-adoption.py merge-FRESH-UNIQUE` with that checkout as cwd. Source/schema gates execute ONLY inside the fresh Podman pod. The copied-source check first asserts all input files unchanged, retains generated-cache hashes, removes only the two known mutate_schema/helper bytecode files and then compares the entire file manifest.
- Graph: own detached checkout at published PR38 `e9a19f1476d1f6f3be14d7df6ccb3fb7eddd9e7e` in `.worktrees/cox-r426-graph-cleanup-20261010`; invoke `safe-run-graph.py green-FRESH-UNIQUE`. Root only prepares the root-owned fixture and creating-user cleanup; the regression suite runs as actual UID 10001 against a 0700 system-cache parent. No full graph/model/inference/native release proof is implied.
- Faults: set `REPLAY_POST_CREATE_FAULT=pod` or `container` for a separate unique native-fault phase. Expected application status is failed; owned cleanup must independently be verified. Leave this environment variable unset for normal GREEN reproduction.

The product inputs for the C03 merged proof and graph proof remain separately pinned. `tool_input_sha256` captures the externally executed controller and shared lifecycle helper BEFORE launch. Current-main incorporation changes the publication base, not those pinned product proofs. Core/schema/runtime API files are unchanged by this tooling PR. Global dist/packaging folder-fitness RED is retained separately. Source review, main merge, native admission, deployment and release remain their owners' gates.
