# Current PR44 repair

Use [the repaired-source recipe](../c05-rework/reproduce.md) and its
`publication-index-repair-001.json`. The local entry point redirects to that
controller, whose lifecycle helper is pinned beside it. The current proof is
186 clean cases and75 qualified BODY faults. Mike's8 and the separate private5
remain unchanged. `verify-final.py` verifies the historical tested Git tree;
`verify-repair.py` in c05-rework verifies the current source and final002.
Historical publication indexes refer to their recorded old tree.

## Historical proof description

The following text describes the pre-repair C05 proof and its then-current gates.

# C05 records and coordination proof

Current entry is `run-core-pg.py` from this family. Run it from a dedicated Cox product worktree, with the exact eight pinned offline wheels under `tmp/wheels`, and a NEW `mutation-<attempt>` phase name. Host Python is stdlib orchestration only. It copies NEXT and wheels into one owned disposable nativearm64 PostgreSQL/Python Podman stack, maximum2CPU/1GiB, no ports or host binds. It uses the shared replay lifecycle family for pending-before-create, fresh name/nonce ownership and immutable-ID cleanup. Never use frozen old replay producers.

The controller saves the complete raw command stream and source/tool hashes. Qualification requires schema30, auth40, records14, jobs23, conformance6, acceptance guards7, contract33 and shared20; actual C05/C01/shared mutations30/26/8 must each fail only by their declared expected BODY assertions, without errors. All source bytes must be restored, the entire copied input manifest must match, and cleanup/absence/password-discard/lock-release must be verified. A receipt is failed if any condition is unproven.

Historical records/jobs/conformance RED and GREEN receipts preserve the build order. Original14/23/6 tests and added7 acceptance guards are frozen by `frozen-inputs.json`. Guards RED has24 expected BODY assertion records and no errors; it led to the final original-deadline acceptance predicate, early typed fence/UTF8 checks and shared16MiB byte limit. The final check defines acceptance inside the transaction, not physical commit scheduling.

Mutation attempt001 remains FAILED with23/26 killed and3 inconclusive recipes. Its disposition retains the exact causes. Attempt002 remains FAILED with29/30 killed and one survivor: removing the early expiry check leaves the final deadline predicate intact. Attempt003 removes both predicates for the end-to-end expiry fault; the separate delayed-result fault still checks the late guard. No frozen assertion is weakened.

Internal results have no event_id and are not the C01 external canonical write ACK. C06 capture and C11 admission are held. Full released HTTP/legacy compatibility and registered role/human governance are held for C02/C04a. Budget status remains `not_enforced`. Auth-0003 is the accepted separate next slice. No source receipt approves its own merge, live writer or release.
