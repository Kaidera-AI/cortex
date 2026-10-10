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
