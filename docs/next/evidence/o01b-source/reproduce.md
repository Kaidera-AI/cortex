# O01b complete-data and corruption validation — synthetic proof

Current base: O01a PR #71 `111968971dd46459d098aeb9bca97fd2b09a6d08`.
Original native source-tested commit: `1b0ee6a699e96e72abdff9dc560a90f3b0d95790`.
PR72-1 native source-tested commit: `aed5a1045aa3134fe274cf17840b2c40e8b949c0`.

## PR #72 post-validation drift repair

`seal_complete_bundle` now passes the manifest that survived native PostgreSQL
validation directly to the private sealer. It never rebuilds a manifest from
later staged bytes. The sealer compares every streamed base, WAL and blob
member to that manifest and removes temporary ciphertext without publishing
when a digest changes. Two RED controls mutate a base or WAL file after the
native-check seam; the digest-guard-removal mutant fails both in their test
bodies. The renewed native proof repeats those two drift cases on real staged
PostgreSQL files and records that no ciphertext was published.

O01b extends O01a's encrypted bundle with actual content-addressed blob bytes.
Before sealing, it verifies PostgreSQL's native backup-manifest checksum and
every SHA-256-listed base file, then runs `pg_verifybackup` and `pg_waldump`
through the full archived interval. The final manifest binds a single
PostgreSQL statement's record heads, active vector revisions, blob inventory,
schema ledger, model identities and consumer checkpoints from a **paused,
isolated recovery**. The manifest endpoint must equal that recovery's actual
`pg_last_wal_replay_lsn()`, which may be later than the requested target LSN.

`verify_complete_bundle` decrypts the bundle into an owned temporary area,
checks the independently supplied manifest SHA-256, exact member set, sizes and
digests, PostgreSQL native manifest and WAL validity, and the restored snapshot
before it returns `recoverable: true`. Missing or bad age identity refuses.
The independent manifest digest is a custody input, not a signature. Release
trust and key custody remain Ren-TK/integration work.

The current local checks are:

```sh
PYTHONPATH=next/src python3.12 next/tests/test_receipts.py next/tests/backup_producer
PYTHONPATH=next/src python3.12 next/tests/test_receipts.py next/tests/backup_validation
PYTHONPATH=next/src python3.14 next/tests/test_receipts.py next/tests/backup_validation
python3 docs/next/evidence/o01b-source/verify-pr72-1.py "$PWD"
```

The original eight-fault matrix and `verify-o01b.py` remain historical at
their source-tested commit. The current RED, GREEN, mutant and native receipts
are `postcheck-drift-*.json` and `native-pg-pr72-1-001.json`.

The committed `run-native-pg.py` source creates one exact-owned, network-none
arm64 PostgreSQL 18.4 container, two CPUs/1 GiB, no ports or host bind mounts.
It applies the nine migrations to synthetic data, begins a rate-limited base
backup, then commits record, vector, blob and checkpoint changes while that
backup is still running. It archives WAL, recovers a second cluster inside the
same disposable container to a paused LSN, and reads all four after values in
one MVCC statement. It seals two synthetic blob objects using a disposable
age identity and refuses mixed metadata, missing blob, altered vector/schema/
WAL files, post-validation base/WAL drift, wrong key and wrong manifest digest. PostgreSQL tools on the test
host are 18.6, the same major as the pinned server image; edition packaging
must pin the client-tool build. `native-pg-pr72-1-001.json` is the current raw
receipt; `native-pg-final-004.json` is historical. The initial recovery-socket failure and the earlier boundary-mismatch
proof remain visible but are not counted as the final pass. The controller
removes the exact-owned container and temporary plaintext stage in `finally`.

Only synthetic data and synthetic encryption material were used. This verifies
the backup set and a disposable same-host recovery boundary; X01 still owns
primary/storage/logical failure scenarios and measured Production and
Standalone RPO/RTO. No live backup, restore or adoption was performed.

PostgreSQL's [backup manifest format](https://www.postgresql.org/docs/18/backup-manifest-format.html)
and [pg_verifybackup behavior](https://www.postgresql.org/docs/18/app-pgverifybackup.html)
define the native checksum and file-validation inputs used here.


## Current re-pin onto PR71 scoped exhaustion (Kai00:13,12 Oct)

Current predecessor is `435da3df7a253b3e0f5a41dd0d188320a40f882b`. Current native controller is `run-native-refresh.py`, with fresh `native-pg-refresh-002.json`; inherited module proof is `c07-review-pr72-refresh-003.json`. `verify-pr72-refresh.py` and `refresh-binding.json` bind the current publication. Original source-tested receipts/verifiers above remain historical; do not treat them as native execution at a later source.

The accepted O01b product and frozen tests remain byte-exact at c3cad3ed. Current proof reruns16 O01b/seven inherited O01a tests,9+7 exact BODY-killed faults and both clean/restored baselines. Fresh inherited44 C07 controls include the manifest gate and005 scoped-exhaustion witnesses. All486 NEXT inputs and controllers are bound to the frozen native source; publication changes proof docs only. Actual35% Mac+activeVM admission precedes logged resource creation,networknone,2CPU1GiB,no ports/binds,exact identity cleanup. No commit may change HEAD while the inherited controller is running.

```sh
PYTHONDONTWRITEBYTECODE=1 python3.12 next/scripts/mutate_backup_validation.py
PYTHONDONTWRITEBYTECODE=1 python3.12 docs/next/evidence/o01a-source/mutate-backup-forward.py
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/o01b-source/verify-pr72-refresh.py .
```

Native outputs must be fresh names; controllers refuse overwriting an existing receipt. All resource jobs run sequentially through the inherited lifecycle. Source-only re-pin does not grant reviewer acceptance,main merge,backup adoption,client-tool/key custody or X01 numeric recovery credit.

Kai00:24b then required main re-pin after the namedPR71 merge. Current target9309ea77 has exactly435da3df tree0bf129ef; rebase will compare every486 tested NEXT input and each native controller/input digest at983b466. Native receipts identify983b466, not the later publication. Current independent verifier reruns the complete digest comparison at publication; no source-change or new native-execution claim is inferred from new commit ancestry.
