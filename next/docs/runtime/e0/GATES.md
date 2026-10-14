# E0 acceptance gates

- [ ] G1 inherited product, tests and RC0 bytes unchanged; new paths only.
  CHECK: evidence boundary verifier, with positive tamper control.
  EXPECT: E0_BOUNDARY_OK
- [ ] G2 tests-first frozen commit precedes policy/proposal; RED classified by shared receipts, zero errors/fixture failures/effect attempts.
  CHECK: frozen runner + shared test_receipts report; classify SurfaceAbsent/SemanticRed only after shared body attribution.
  EXPECT: E0_RED_VALID
- [ ] G3 exact unapplied RC0 diff applies only to scratch copies and proposed shape positives/negatives decide as specified.
  CHECK: proposal verifier using existing strict decode and Draft202012Validator.
  EXPECT: E0_PROPOSAL_OK
- [ ] G4 contract covers policy, E1/E2 dependencies and tests; no implementation/ratification claim.
  MANUAL: source-linked self-review against exact handoff and sizing; no independent review claimed.
- [ ] G5 three author-consult completions accepted and same-row return delivered.
  CHECK: fresh Cortex completion readback and return API receipt.
  EXPECT: accepted completion receipts and returned main row
- [ ] G6 own branch pushed normally and remote SHA equals sealed local head.
  CHECK: git ls-remote origin refs/heads/nemo/e0-policy-oracle-20261014
  EXPECT: exact sealed local HEAD
