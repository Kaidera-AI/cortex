# RC1 acceptance gates / depth tree

- [x] G1 Oracle: all seven donor methods retained/adapted plus real role isolation, privacy, reconnect/query and explicit shutdown cases. Missing APIs fail BODY assertions, not imports. Frozen tests committed before product.
- [x] G2 RED: actual focused L1 receipt contains deciding BODY failures, zero import/setup errors; donors separately exhibit privacy/shutdown deciding failures before adaptation.
- [x] G3 Scope: exactly two selected product definitions plus imports/globals; all preexisting top-level definitions unchanged by AST; all 19 manifest-bound SQL bytes and manifest unchanged; no migration.
- [x] G4 GREEN: real healthy query, complete current Core-down route matrix with zero request/handler effects, twenty coalesced callers, connect/query/fetch/close/stalled-handshake bounds, fresh reconnect, privacy and no surviving owned tasks after successful explicit shutdown.
- [x] G5 Regressions: current D1–D4, first identity/enrollment/session and inherited source suites pass on L1; raw test receipt records methods/failures/errors/skips.
- [x] G6 Causality: each named source-copy mutation produces a deciding test BODY kill, followed by exact restore and full focused suite PASS; no fixture/import errors counted.
- [ ] G7 Delivery: clean normal-pushed owned branch at exact reviewed candidate SHA; remote/source receipts; Vera and Hale review lists; G_OLD UNRESOLVED.
- [x] G8 Custody: only admitted L1 disposable labeled PG resources used; source hashes measured on host; owned resources absent after cleanup; lab-host stop receipt shows deallocated.

CHECK/EXPECT: The reviewed run_l1.py invokes test_receipts.py in an isolated runner and preserves its machine-readable BODY classifications. validate_scope.py (evidence-only) compares AST and Git/source hashes. The author never self-accepts these gates; unmet gates are explicit in the return.

## Receipt status

G1–G6 author measurements are met: 17-method/19-BODY absence RED; five exact-donor privacy/shutdown BODY failures; source/SQL/old-AST preservation; 17 focused L1 GREEN; 344 inherited GREEN across valid initial and corrected affected-suite receipts; nine faults killed by 11 BODY assertions, each restored and full17 GREEN. Mac has a separate six-method pure source witness. Exact receipts and early inconclusive attempts are in the external sealed return packet.

G7 is recorded after this documentation commit by the destination-readback in the external return: normal push of cox/rc1-probe-20261014 only, no main merge and no PR needed for these admitted-host tests. G8’s owned container/pod/volume absence is measured; the external return carries the subsequent lab-host stop/deallocated receipt. Neither independent source/security acceptance nor G_OLD is checked by this author.

L1 stop receipt: 2026-10-14T13:47:47.864675+00:00, exit 0, VM state: VM deallocated. G8 is met. G7 remains a destination-readback item in the final external gate receipt; source review/merge/native/G_OLD are still outside author acceptance.
