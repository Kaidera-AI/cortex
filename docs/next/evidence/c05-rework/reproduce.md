# C05 repaired-source proof

Run from an isolated checkout of the returned PR44 head. Host Python executes
only stdlib custody/orchestration; product tests execute only in the copied
native arm64 Podman fixture. Prepare the exact eight wheels described in
offline-wheel-inputs.json at tmp/wheels; never install host dependencies.

```sh
python3 docs/next/evidence/c05-source/run-core-pg.py --preflight-only
python3 docs/next/evidence/c05-source/verify-final.py .
python3 docs/next/evidence/c05-rework/verify-repair.py .
python3 docs/next/evidence/c05-source/run-core-pg.py mutation-reviewer-fresh
```

The historical verifier checks the original tested Git tree and archived
controller, not a false claim that current repaired source equals that old tree.
The current controller locates its pinned helper in the same publication folder,
validates all fault anchors and hashes actual wheel bytes before admission;
inside the copied fixture it re-hashes every wheel before pip. Use a fresh phase
name. Existing evidence is immutable. Aggregate resource budget is2CPU/1GiB;
no ports, bind mounts or live engines. Cleanup must verify all three owned
immutable IDs absent, discard the password and release the lock. Producer reuse
and PR40 lock repair remain held separately; this is a sequential manual proof.

Two RED receipts retain Mike's6 BODY failures and the private4 BODY failures;
repair-green-001 retains the first clean186 cases. Failed mutation-final-001 is
kept as29/30 qualified, with bool ABI error reported INCONCLUSIVE. Final002
must independently qualify all75 BODY faults and5 auxiliary-only GREEN controls.
No original173, Mike8 or private5 assertions are edited.

The corrected auth-0003 planning metadata names each file's exact main or C04
commit. It does not claim released issuer/adoption admission. Security review,
main merge, live installation, source admission, x86, donor/signing/release and
external event acknowledgement remain separately held.

Classifier requalification: after merging PR55 from main, the receipt suite has 29 tests (the prior 20 plus nine classifier controls). `mutation-reclassified-001.json` is retained FAILED because the old controller expected 20. Use a new phase after updating the controller count; all remaining frozen C05 suites and fault recipes stay unchanged.

The complete fixed-classifier replay is `mutation-reclassified-002.json` at
`16677ed2f1c72ff18f5a838ebdf0ca1fd67f1fba`; PR55 merged via main
`fdd04d7b8b8f977e3c3b94f7badc9a17c87d787f`, and the classifier SHA-256 is
`aee2b3a3efc0a33fb7efc60a38a65ca3c624f5bc208db29ceebe4eae62242b13`.
Run `python3 docs/next/evidence/c05-rework/verify-reclassified.py .` from the
reclassified PR44 worktree. It checks 195 clean tests, the 30+11+26+8 actual
fault groups, 234 expected test-body failures with active phase attribution,
406 source files, frozen controller/helper/wheels and exact-owned cleanup.
The first replay is retained FAILED (old receipt-suite count 20 versus 29).
The final two commits after the native proof carry docs/evidence only, so NEXT
bytes remain those bound to the tested tree. Publication custody is in
`publication-index-reclassified-001.json` with external expected inventory
`publication-expected-reclassified.json`.
