# E0 acceptance gates

Evidence home: output/Cortex/nemo-e0-policy-oracle-20261014 outside the Git tree.
All author checks ran from this worktree with Python 3.14.0 on macOS. Shell: zsh.
No product source, accepted RC0 file or inherited test was edited. The transport
checks below close against the final pushed head in the external delivery receipt;
no future push/return is pre-certified by this file.

- [x] G1 inherited product, tests and RC0 bytes unchanged; new paths only.
  CHECK: check_boundary.py in evidence, reviewed author tooling.
  EXPECT: E0_BOUNDARY_OK
  EVIDENCE: exit 0; 319 inherited files including 236 test files byte-equal;
  one in-memory hash tamper detected; E0 frozen test files unchanged after freeze.
- [x] G2 tests-first freeze precedes policy/proposal commit; valid classified RED.
  CHECK: run_red.py invokes frozen effect guard and existing test_receipts.py.
  EXPECT: E0_RED_VALID
  EVIDENCE: exit 0 for audit; actual unittest exit 1, 39 tests = 27 surface absence,
  7 semantic RED, 5 PASS, 0 errors; every failure test-body attributed; effect
  attempts 0. Three deliberate receipt controls (fixture/import/IP socket) are
  inconclusive as required. Initial inherited launcher argv error is retained,
  excluded from test evidence, then corrected without changing test source.
- [x] G3 exact unapplied RC0 diff applies only to scratch copies; shape oracles decide.
  CHECK: verify_proposal.py uses existing strict decode and Draft202012Validator.
  EXPECT: E0_PROPOSAL_OK
  EVIDENCE: exit 0; 14 positive + 18 negative shape cases, 3 in-memory omission
  controls; accepted production pin rejects candidate operations_mismatch.
  No full semantic/runtime/native qualification claimed.
- [x] G4 policy covers E1/E2 and bounded owner/dependency gates.
  MANUAL: author re-read handoff, sizing, current admission, provider, cache,
  read adapter and proposed diff; checked OFF no-fake dependency and one-switch
  retained metadata, non-reusable revision, late cache publication, request
  deadline, typed unsupported, graph and closed C11 response ratification.
  Independent Cox/trust/mechanics acceptance remains outside this author gate.
  EVIDENCE: inherited 148/148 PASS, 0 errors/effect attempts; scoped secrets scan
  exit 0, 0 findings. All earlier tests unchanged. DEP-W remains separate.
- [x] G5 three named author-consult completions accepted.
  CHECK: fresh Cortex --show plus saved API accepted:true receipts.
  EXPECT: all three completed and accepted:true
  EVIDENCE: 88e8b7db, c3d048ba, bb2177a8 completed; receipts in consults/.
- [ ] G6 own branch pushed normally; exact remote head matches final seal.
  CHECK: git ls-remote origin refs/heads/nemo/e0-policy-oracle-20261014
  EXPECT: final sealed local HEAD
  DELIVERY: close in external PUSH_RECEIPT.json; own branch only, no PR/main/force.
- [ ] G7 same handoff returned with exact artifact, then end turn.
  CHECK: cortex-return 10eadb99-d485-4004-9b9d-505bd5c66f14 --agent nemo ...
  EXPECT: returned:true with completion handback identifier
  DELIVERY: close in external tmp/nemo-e0-policy-oracle-return-20261014.json.

G5's original combined consult/return gate was split into G5 and G7 so delivery
is not silently checked before it happens. No acceptance condition was dropped.
