# PR40 REPLAY-LOCK-001 narrow repair plan

Owner Cox; Kai's H-D455 DO NOW and Vera's PR40 Medium are the accepted repair boundary. Pinned PR40 base/head `67265f371a8829d12511c082b7792a1441ddcd77` / `0028cc418d7e2920763e1f48ba98d803fd5370b5`. No source outside the published replacement replay helper and its controller proof. Producer reuse stays held pending Vera.

## Invariant and implementation

The project flock guards admission and cleanup. `create` durably records the fresh owner/nonce/name as pending **before** the external create; after exact inspection it records the immutable ID. `close` retries exact-name/owner/nonce/ID cleanup and verified absence. If cleanup fails, it durably marks that lifecycle dirty before releasing the lock. The next `acquire`, while holding the lock, reconciles the prior exact lifecycle and refuses admission until absence is verified. A malformed/foreign marker, uncertain inspection, failed marker write, or failed cleanup is fail-closed. Marker contains no password. Atomic `0600` replace and fsync bind its bytes; only this helper's marker path is touched. This chooses Vera's dirty-barrier option, preserving the frozen 21-test lock-release-on-error expectation.

## RED -> GREEN gates

1. Freeze a new competing-producer control using the contained fake Engine: A creates with fresh owner/nonce, cleanup removal fails, A unlocks, B acquires. Before repair B wrongly acquires while A's exact resource remains. Add an uncertain inspection and a second failed-reconcile control, plus strict marker rejection and bounded retry; no live host target.
2. Run that frozen control against the old published helper as actual expected BODY AssertionError, retain raw output. Keep all four original test files byte-identical.
3. Repair only helper admission/cleanup. Run the new control and unchanged 21 tests inside the copied arm64 2CPU/1GiB native fixture, then actual guard-removal fault showing the new control fails at its test body. Restore all copied bytes and verify owned cleanup/password/lock.
4. Publish raw results and exact source hashes into PR40, run index tamper RED/GREEN and independent read-only audit, push feature head, verify CI/origin, hand to Vera. No main merge or adoption. Mike's separate B01 same-path read-only note is already returned; do not edit his code.

## Independent verifier amendment

At tested source `de6dce4`, the independent reader found two omitted cases: an owned dirty-marker entry without an immutable binding, and a known immutable ID surviving under a changed name while the old name is absent. `internal-verifier-lock-rework.json` preserves the findings. Added two frozen cases in `test_lock_barrier.py` before source repair; `lock-red-003` has exactly those two expected BODY AssertionErrors among 24 tests and no errors. Repair requires owned marker entries to have bindings, and a missing name to trigger exact known-ID absence verification before clearing the marker. `lock-green-002` is 24 clean. `lock-mutant-002` separately removes each new guard, yielding its one expected BODY assertion, then restores 24 clean. Preserve the earlier 22-case receipts as historical, not final acceptance.

## REPLAY-LOCK-002 amendment, Kai 12:51

Vera found the remaining branch at PR40 head `4f32c2c2`: if a bound owned ID survives under a changed name and a foreign resource takes the old name, `inspect` returns on the foreign name before querying the bound ID. Keep the prior 24 controls and four original test files byte-exact. Add one `test_lock_barrier.py` case that persists a failed-cleanup marker, renames the owned fake-engine row, places a foreign row at the original name, and requires B's acquire to refuse with the binding retained while the foreign row remains. Show expected BODY RED on the current source. Change only `replay_lifecycle.py` so the immutable-ID absence check runs independently of every name outcome, including a foreign label. A bound ID still present, or an uncertain ID query, refuses admission; no foreign removal. Then show 25 GREEN in the same copied arm64 2CPU/1GiB native fixture, a mutation that skips the ID query on a foreign-name mismatch and yields only the new expected BODY assertion, restored 25 GREEN, unchanged prior test hashes, exact-owned cleanup and refreshed byte publication index. Push PR40 for Vera's new exact-head review; no producer reuse or main merge.
