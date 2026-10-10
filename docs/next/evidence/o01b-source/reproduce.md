# O01b complete-data and corruption validation — synthetic proof

Base: O01a PR #71 `8c2bcd93dd747b93d4ec15bb8380f89db4b73856`.
Source-tested commit: `1b0ee6a699e96e72abdff9dc560a90f3b0d95790`.

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

The local checks are:

```sh
PYTHONPATH=next/src python3.12 next/tests/test_receipts.py next/tests/backup_validation
PYTHONPATH=next/src python3.14 next/tests/test_receipts.py next/tests/backup_validation
python3.12 next/scripts/mutate_backup_validation.py
python3.12 docs/next/evidence/o01b-source/verify-o01b.py "$PWD"
```

The committed `run-native-pg.py` source creates one exact-owned, network-none
arm64 PostgreSQL 18.4 container, two CPUs/1 GiB, no ports or host bind mounts.
It applies the nine migrations to synthetic data, begins a rate-limited base
backup, then commits record, vector, blob and checkpoint changes while that
backup is still running. It archives WAL, recovers a second cluster inside the
same disposable container to a paused LSN, and reads all four after values in
one MVCC statement. It seals two synthetic blob objects using a disposable
age identity and refuses mixed metadata, missing blob, altered vector/schema/
WAL files, wrong key and wrong manifest digest. PostgreSQL tools on the test
host are 18.6, the same major as the pinned server image; edition packaging
must pin the client-tool build. `native-pg-final-004.json` is the final raw
receipt. The initial recovery-socket failure and the earlier boundary-mismatch
proof remain visible but are not counted as the final pass. The controller
removes the exact-owned container and temporary plaintext stage in `finally`.

Only synthetic data and synthetic encryption material were used. This verifies
the backup set and a disposable same-host recovery boundary; X01 still owns
primary/storage/logical failure scenarios and measured Production and
Standalone RPO/RTO. No live backup, restore or adoption was performed.

PostgreSQL's [backup manifest format](https://www.postgresql.org/docs/18/backup-manifest-format.html)
and [pg_verifybackup behavior](https://www.postgresql.org/docs/18/app-pgverifybackup.html)
define the native checksum and file-validation inputs used here.
