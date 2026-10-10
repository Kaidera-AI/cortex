# PR40 REPLAY-LOCK-001 narrow repair plan

Owner Cox; Kai's H-D455 DO NOW and Vera's PR40 Medium are the accepted repair boundary. Pinned PR40 base/head `67265f371a8829d12511c082b7792a1441ddcd77` / `0028cc418d7e2920763e1f48ba98d803fd5370b5`. No source outside the published replacement replay helper and its controller proof. Producer reuse stays held pending Vera.

## Invariant and implementation

The project flock guards admission and cleanup. `create` durably records the fresh owner/nonce/name as pending **before** the external create; after exact inspection it records the immutable ID. `close` retries exact-name/owner/nonce/ID cleanup and verified absence. If cleanup fails, it durably marks that lifecycle dirty before releasing the lock. The next `acquire`, while holding the lock, reconciles the prior exact lifecycle and refuses admission until absence is verified. A malformed/foreign marker, uncertain inspection, failed marker write, or failed cleanup is fail-closed. Marker contains no password. Atomic `0600` replace and fsync bind its bytes; only this helper's marker path is touched. This chooses Vera's dirty-barrier option, preserving the frozen 21-test lock-release-on-error expectation.

## RED -> GREEN gates

1. Freeze a new competing-producer control using the contained fake Engine: A creates with fresh owner/nonce, cleanup removal fails, A unlocks, B acquires. Before repair B wrongly acquires while A's exact resource remains. Add an uncertain inspection and a second failed-reconcile control, plus strict marker rejection and bounded retry; no live host target.
2. Run that frozen control against the old published helper as actual expected BODY AssertionError, retain raw output. Keep all four original test files byte-identical.
3. Repair only helper admission/cleanup. Run the new control and unchanged 21 tests inside the copied arm64 2CPU/1GiB native fixture, then actual guard-removal fault showing the new control fails at its test body. Restore all copied bytes and verify owned cleanup/password/lock.
4. Publish raw results and exact source hashes into PR40, run index tamper RED/GREEN and independent read-only audit, push feature head, verify CI/origin, hand to Vera. No main merge or adoption. Mike's separate B01 same-path read-only note is already returned; do not edit his code.
