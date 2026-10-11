# PR72 refresh gates — Kai23:15 accepted scope

Depth tree: rebase custody → O01b inherited/fault/native qualification → exact-source receipt/push/CI → Mike/Kai review gate.

- [x] G1: rebase owned PR72 delta from1119689 onto named9f5cbd3; preserve accepted O01b product and frozen test bytes, retained main/C07 paths; record conflicts explicitly.
  CHECK: Git ancestry, mode/blob table and accepted c3cad3ed owned-source hash comparison.
  EXPECT: all protected bytes equal; no unexplained path deletion.
- [x] G2: rerun seven inherited O01a and16 O01b controls; eight original O01b,seven inherited O01a and postcheck-digest fault all kill by exact expected BODY assertions; restore originals.
  CHECK: typed test receipts and source hashes.
  EXPECT: no errors/survivors/inconclusive; exact restoration.
- [x] G3: rerun actual native PG18.4 concurrent backup/paused recovery, full snapshot/blob/native member checks and ten refusal controls under actual ≥35% Mac+VM pre-create gate; same cached image/lifecycle/networknone/2CPU1GiB/no ports/binds; exact-owned cleanup.
  CHECK: current-source native receipt, admission ordering, snapshot ledger against current manifest, member inventory and cleanup identity.
  EXPECT: passed true and empty errors; no real credentials/data/adoption.
- [ ] G4: inherited C07 manifest gate at integrated candidate under owned PG; bind full current source/controller/inputs, separate native source from proof publication; verifier must refuse altered receipt and redirected source.
  CHECK: native gate result; committed source/receipt digests, tamper controls, full path table.
  EXPECT: source/receipt binding passes after restoration.
- [ ] G5: push reviewed refresh with exact old-head lease, all4 exact-head CI SUCCESS, formal Kai return and remove own clean worktree/wheel copies.
  CHECK: origin refs/PR/CI, Cortex roundtrip, cleanup.
  EXPECT: same exact publication head; source-only; no main merge. Mike rework pre-empts.

Old native/verifiers remain historical at original commits. Necessary controller fixes bind fresh admission and current schema ledger; do not change product/test assertions to clear refresh failures. No Gavel/Jev or host install. Named base is pinned9f5cbd3 by Kai23:15.

Measured G1: clean9-commit rebase,23 O01b paths,accepted product/tests equal c3cad3ed. G2: stale14-test driver RED reports8 INCONCLUSIVE with16 actual tests/0errors; corrected16 baseline/restored +postcheck fault →9/9 O01b kills/12 BODY assertions; unchanged inherited7/7 kills/12 BODY assertions. No product or frozen test change.

Native inherited attempt001:40 bodies clean and all485 copied sources restored; rejected by existing frozen-HEAD assertion because author committed controller-only change during run (de86be9 →56fc876). No qualification credit. Owned3 resources removed. Current source/controllers are frozen before sequential native refresh and attempt002; no commits until both jobs complete.

G3 native PASS at2d4da9d:23 focused tests,1112 base/4 WAL/2blob/1119 encrypted members,10 refusal controls,actual37%Mac/70.369%VM,owned1 cleanup. Inherited attempt002 PASS at same frozen2d4da9d:40 clean module tests including both manifest controls,485 exact restored inputs,owned3 cleanup. G4 independent binding follows in this publication; G5 final CI/cleanup completion is an external exact-head receipt to avoid changing the head while it is being qualified.

## Kai00:13 re-pin (12 Oct)

Previous local07bc66d was unpushed and preempted. Clean rebase onto435da3df accepted now. Re-run9+7 faults,backup23,current44 module (including scoped-exhaustion and manifest),486 inputs,same native boundaries; freeze all source/controllers and HEAD before either native job. Do not credit old source-bound native receipts at this new source. Current source/proof/native output names are fresh; push with exact old remote c3cad3ed lease,CI4,formal return,own cleanup. PR72 held; Mike PR71 rework preempts. G1/G2/G3/G4 completion resets for this new pin; final completion is exact-head external receipt.
