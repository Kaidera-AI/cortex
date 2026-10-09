# C01 PR30 rework — Kai 21:58

Owner cox@helix; Mike re-reviews; Kai adjudicates. The latest DO NOW names PR30-1/2 as real findings. Reopen only the own C01 branch/SSD worktree; publish one corrected head with receipts, no self-merge. C03 is already published separately, and must absorb this correction and the team mutation rule before continuing the ordered queue.

Keep both trailing-newline RED probes. Make envelope patterns use a JSON-Schema-compatible strict end-of-input assertion; enforce exact digest length 64. Preserve accepted OpenAPI/matrix bytes. Original tests remain unchanged; add a focused version-index negative to make version-schema admission mutations causal.

Replace nonzero-exit mutation counting with a stdlib unittest receipt runner: preserve literal stdout/stderr, assertion failures with test IDs, and errors separately. Each mutant declares its expected affected test. Count killed only on exit 1 with that test's assertion failure and no errors; successful runs survive; missing/invalid receipts, wrong-test failures, imports/setup/errors/signals are inconclusive. Survivors or inconclusive results fail the mutation command. Keep raw output for every mutant.

Replace mutations that merely crash configuration/setup with semantic admission mutations. Manifest-index mutants select temporary valid-but-relaxed schemas; the expected behavioral assertions must detect the resulting admission. Add mutation coverage for the classifier itself. Do not rewrite behavioral tests to count exceptions as assertions. Run native-arm64 disposable Podman RED/GREEN/mutations with copied inputs, limits and cleanup. Retain old evidence as historical; publish the revised causal results and ask Mike for re-review through Kai's return.

Pre-publication amendment: preserve setup/teardown assertion RED at c0b80df. Attribute failure phase from the actual unittest traceback and test method; fixture/cleanup assertions are inconclusive even when unittest reports the same test ID. Add subprocess probes for fixture failures and causal body assertions.
