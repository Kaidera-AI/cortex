# PR #83 review repair plan

Authority: Kai's 2026-10-10 21:28 DO NOW; Vera's PR #83 findings
UPG-KEY-001, UPG-ROLL-001, UPG-ACCEPT-001, UPG-BACKUP-001. The source
unit remains synthetic-pair only; production v0.1.003 trust pins and
installer-owned backup/fence/restore are separate gates.

1. Forward-merge repaired PR #80. Freeze Vera's key-swap and three
   coordinator/journal controls as repo-local source tests, then show four
   expected test-body failures before changing upgrade source.
2. Key admission reads the public key once, hashes those bytes, writes them
   to a private 0600 file in a 0700 temporary directory, and gives that stable
   path to both minisign verifications. No policy fallback or live trust root.
3. Journal operations use an exclusive file lock and compare-and-set state
   transitions. Only prepared can become accepted or rolling_back;
   rolling_back is durable before fencing/restoring, blocks acceptance and
   can be resumed after a crash. A rollback from any prepared step prefix can
   complete; accepted is always fix-forward-only. Harden coordinator tests
   and wire the actual AtomicUpgradeJournal in the PG rehearsal.
4. Fence old writers before taking and verifying the rollback backup. Prove
   an acknowledged writer at the boundary survives rollback. Run the controls
   GREEN, named mutants for key, ordering, rollback and acceptance, and exact
   host plus disposable-PG qualifications. Retain raw outputs and cleanup.

The frozen Vera acceptance interleaving expected an acceptance during
`fence_new()`. That schedule is forbidden by Kai's newer requirement to
durably mark `rolling_back` *before* any rollback side effect. Preserve the
original RED commit as evidence; replace its one race expectation with two
strict orderings: acceptance before the marker blocks restore, and marker
before acceptance blocks acceptance. Add crash/re-entry in the marked state.

Risks: the acceptance/rollback race can cause data loss; the backup boundary
can omit an acknowledged write; a key path race can subvert admission. No
host upgrade, released artifact execution, or mainline merge is authorized.

## Round 3: UPG-ADV-001

Kai's 21:56 DO NOW rules the whole-operation lock. Freeze Vera's paused
`advance()`/rollback schedule as a test-body RED before source changes. Add
an installer-supplied `operation_lock_path` distinct from the journal `.lock`
file, and hold one exclusive `flock` across every complete prepare, advance,
accept and rollback call. The lock covers `apply()` and `restore()`, not only
the journal save. Prove both terminal orders, crash re-entry and the native
0/1/2-step rollback rehearsal. A mutant deleting the advance operation lock
must be a body-assertion kill. Every GREEN command explicitly unsets the
`CORE_UPGRADE_TEST_SOURCE` test override. Installer-owned restore idempotence
remains an external production gate.
