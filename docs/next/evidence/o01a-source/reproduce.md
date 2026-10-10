# O01a backup producer — synthetic source proof

Base: C07 PR #69 `e2e5a889a47c7ee538dfead08f44c7773fd72fe0`.
Source-tested commit: `7eb8eae10a00e5ef778267dcd6c44be51c538d39`.

The producer stages a PostgreSQL plain base backup with `-X none` and SHA-256
native manifest checksums. A caller provides an owned, completed continuous WAL
archive and obtains `archive_end_lsn` from PostgreSQL before switching that
segment out to the archive. `build_manifest` checks PostgreSQL's backup range,
backup label, full-size contiguous archived segments through that endpoint,
and hashes every staged base and required WAL file. It binds supplied
installation, schema-ledger, consumer-generation, model-identity and blob
inventory metadata by canonical JSON hash. `seal_encrypted_bundle` rehashes
each member while streaming an age-encrypted tar and atomically publishes only
the complete ciphertext. Credentials are not command arguments; the eventual
runtime caller uses an owned PostgreSQL credential file. No plaintext tar is
created; the caller owns and removes its plaintext staging directories.

The standalone source checks are:

```sh
PYTHONPATH=next/src python3.12 next/tests/test_receipts.py next/tests/backup_producer
python3.12 next/scripts/mutate_backup.py
python3.12 docs/next/evidence/o01a-source/verify-o01a.py "$PWD"
```

The committed native controller `run-native-pg.py` creates one restricted,
network-none PostgreSQL 18.4 container on the local Podman engine, with two CPUs,
1 GiB, no published ports or host binds. It writes synthetic rows, makes a real
base backup and four archived WAL segments, verifies the base with
`pg_verifybackup -n`, checks base-only/gap/unbound refusals, and decrypts the
bundle with a disposable synthetic age identity. Its try/finally lifecycle
removes its exact-owned container. Run only on a host where the pinned PG image,
Podman and age v1.3.1 are already available; no host install or live data is
required. The raw final receipt is `native-pg-final-003.json`; failed attempt
and earlier successful proof are retained separately. The verifier binds the
final receipt, RED controls, mutation output, Git bytes and cleanup.

O01a binds the supplied metadata to an encrypted complete artifact. O01b must
prove that the metadata and authoritative vectors/blobs/schema were captured
at one consistent database boundary, verify corrupt/missing members, and define
the installer-managed archive and encryption-key custody. X01 separately owns
disposable restore, external ACK ledger and timed dual-edition RPO/RTO. This
slice is no live backup or production recovery certification.
