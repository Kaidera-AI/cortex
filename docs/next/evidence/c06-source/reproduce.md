# C06 copied PostgreSQL source proof

## PR #51 writer-inventory rework r3 — current proof

Mike's `match ...: case query:` capture and a harmless no-op inside
`records._private` both passed the old allowlist. The two committed controls
fail in the test body before repair (`pr51-002-r3-red.json`). Every known
indirect SQL exception is now pinned to the normalized AST hash of its entire
enclosing function. Any function change fails closed; this covers binding
forms that do not use `ast.Name(Store)`. The normalization includes empty
AST fields under Python 3.14 to match the pinned Python 3.12 runtime.
The 11 earlier inventory methods are AST-byte-identical; the imported manifest
test is also unchanged. All 14 inventory controls pass; removing the hash
comparison yields four BODY failures (`pr51-002-r3-mutant.json`).

`mutation-pr51-002-r3-full-002.json` at tested source
`e749432d390860ced54805662f430ece8b108f7e` records 26 clean suites/320
tests, all 154 primary faults killed with 367 expected BODY assertions, zero
errors/inconclusive, 426 copied NEXT files restored and the three owned
resources removed. `verify-pr51-002-r3.py` checks raw source and classifier
receipts against Git. `publication-index-005.json` and the independent
Git-bound `publication-expected.json` cover the exact published source and
evidence inventory. The first native r3 attempt is retained as
`mutation-pr51-002-r3-full-001.json`: it exposed the Python AST empty-field
format difference, failed inventory, and removed its owned stack.

## PR #51 writer-inventory rework r2 — historical proof

Mike's walrus override of `records._private(query)` and an augmented
assignment control failed in the test body before repair
(`pr51-002-r2-red.json`). The scanner now proves that the only binding of the
allowlisted private query is its parameter and rejects every local `Store` or
`Del` use, plus `global` and `nonlocal` declarations. The same exhaustive
binding check closes the other known indirect SQL exceptions. The twelve
inventory tests and original examples pass (`pr51-002-r2-green.json`); removing
the local-binding guard makes exactly those two new tests fail in their bodies
(`pr51-002-r2-mutant.json`).

`mutation-pr51-002-r2-full-001.json` at tested source
`6e8d22de80dbb994d049034a355b49aa5959a897` records 26 clean suites/318
tests, all 154 primary faults killed with 367 expected body assertions, zero
errors/inconclusive, 426 copied NEXT files restored and the three owned
resources removed. `verify-pr51-002-r2.py` checks the raw receipt and source
against Git. `publication-index-004.json` and the independent Git-bound
`publication-expected.json` cover the exact published source and evidence
inventory. Older receipts below remain historical.

## PR #51 writer-inventory delta — prior proof

Mike closed C06-PR51-001/003/004 and reopened 002. The new committed
`test_assigned_variable_dynamic_writer_fails_closed` and
`test_helper_return_dynamic_writer_fails_closed` each failed in the test body
before scanner repair (`pr51-002-red.json`). The scanner now fails closed on
unresolved `execute`/`executemany` arguments while recognizing only its
source-proved existing indirect SQL inputs. Ten inventory tests and 62 declared
writer sites pass (`pr51-002-green.json`); removing the fail-closed branch
causes three expected BODY failures (`pr51-002-mutant.json`).

The complete fixed-classifier native replay is `mutation-pr51-002-full-001.json`
at tested source `1b675b3fc834745544f311b8811909bb20cfb9ae`: 26 clean
suites/316 tests, 154/154 primary faults killed, 367 expected BODY assertion
records all with active phase attribution, zero inconclusive/errors, 426 copied
NEXT files restored, and exact-owned stack removed. Run
`python3 docs/next/evidence/c06-source/verify-pr51-002.py .` from this PR head;
`pr51-002-proof.json` binds the raw proof inputs by SHA-256. Publication
`publication-index-003.json` is compared with the independent Git-bound
`publication-expected.json` and the complete current source/evidence trees.
The next section is the prior four-P2 proof, retained unchanged as history.

## PR #51 functional rework — current proof

The [PR51 rework proof](pr51-rework-proof.json) binds the current tested NEXT source
`a8d8876e769ec77c5164490a79055948db123aaa` to the exact fixed classifier
blob from PR #55 head `2b9ed6e98c27b087e2778571f0ea66212fb27609` (also present at
its merged commit `fdd04d7b8b8f977e3c3b94f7badc9a17c87d787f`). Its canonical
raw receipts cover the four Mike P2 repairs: per-project retention, composed
writer SQL, independent complete publication inventory, and Git-bound replay
inputs. The full copied native proof has 26 clean suites/314 tests and 154 of
154 expected BODY fault kills, zero errors, all 367 failure records bearing
`traceback+active-unittest-v2`, exact copied-source restoration and owned stack
absence. The first full rework attempt retained one INCONCLUSIVE dense-cursor
fault because an unrelated retention test raised a typed expiry error. The
targeted exact-test repair and the later old-classifier full pass are retained
as diagnostic generations; only the fixed-classifier full pass closes this
evidence gate. The final publication inventory is `publication-expected.json`,
checked independently of `publication-index-002.json`, which excludes only
its own self-referential index file.

The following `e8dc899c` proof is historical before Mike's PR51 review.

Tested Git `e8dc899c06b38739144d10227916aadf792922ef`. Actual final native-arm64 receipt is
`mutation-outbox-forward-final-005.json`, SHA256 `06727591a6158c3561eeee489e316b293c8873776e98ee274c89fd763c0699ac`.
All311 baseline tests pass (239 frozen predecessors plus72 C06), followed by
42 C06,32 identity,30 adapter,11 C05 private,3 write-only ACK,1 reserved Core
fact,26 contract and8 shared faults. All153 fail in
their declared BODY assertions, with zero errors, survivors or inconclusive.
Every composed recipe first qualifies its auxiliary-only control; original
predecessor named suites must be entirely GREEN. C06 history control records
unrelated BODY failures explicitly and keeps its original boundary GREEN.
All425 copied NEXT bytes and named baselines are restored.

Before repair, retain missing ports/caller RED, raw history/capture/receipt
RED, publication/retention/inventory RED and authentic actor/TTL RED. All
failed/interrupted qualifications remain retained and are not recast as
semantic kills. Original two new exception assertions required Kai05:16
ratification, fixture-contract RED first, and actual guard-removal kills.
The later unrelated-UUID mutation required ONLY canonical publication-ID
quarantine setup; all32 methods and92 assertions remain unchanged thereafter.
Failed full002, repair001 and attribution001 remain explicit. Attribution001
rejects the broad owner bypass because it alone breaks registration. Final
recipes narrow owners to triggers and bypass only the actual request-kind
predicate; unknown Records writer is one reachable classifier branch fault.

`pre-edit.json` binds393 unchanged predecessor files. Original auth.py,
auth001/002/003, old mutators, all existing predecessor fixtures and the six original
manifest entries stay byte-exact. Additive outbox-0002 creates no29th business
table. The source inventory covers62 writer sites with zero unclassified;
inventory is an artifact guard, not a substitute for PostgreSQL behavior.

Host Python performs stdlib source/AST/hash/JSON/orchestration only. Actual
product imports/tests run inside one copied native-arm64 Podman PostgreSQL18.4
and Python3.12 fixture, total2CPU/1GiB, offline pinned wheels, UID70/10001,
read-only/tmpfs/cap-drop/no-new-privileges, no ports or binds. Fresh lifecycle,
pending-before-create and immutable-ID cleanup are recorded. Final cleanup
proves owned absence, password discard and local lock release. The exact
tested lifecycle helper is local beside the published controller as
`replay_lifecycle.py` and archived as `replay_lifecycle-tested.py`; the
separate PR40 lock-failure finding remains held and follows this source proof.
Manual sequential C06 qualification does not qualify concurrent failed cleanup.

Replay is not executed by reading these receipts. The committed controller
requires its fixture-contract preflight FIRST and all copied product/controller
inputs committed before resources. Do not invoke retired original C03/graph
entry points or their held replacement producers. Fresh replays require current
Kai/Vera producer authority and owned resource absence.

Original accepted plan/gate remain immutable; appended build amendments and
current plan digest are separate in integrity-final. Publication validation
checks every member size/hash against its canonical root or tested Git source.
The C05 private mutation repair was merged forward without rebase. C06
write-only ACK has two frozen RED cases and three actual guard removals.
Two additional frozen reserved Core fact cases prove job/identity rollback
under write-only mutation authority. A private scoped reserved-kind predicate
keeps that decision independent of the public read boundary; its real removal
reproduces both BODY assertions. Original C05 and C04 fixtures/SQL remain unchanged.
The pre-C05 proof and the earlier forward qualification are historical only.

Official Vera/Kai disposition remains independent of the internal verifier.

Door: one-way for applied forward migration, events and pruning. Blast radius:
outbox. Security main merge H-D471, live HTTP ACK/writers, issuer/adoption,
released donor, native x86/install/signing/release and C07 remain HELD.
