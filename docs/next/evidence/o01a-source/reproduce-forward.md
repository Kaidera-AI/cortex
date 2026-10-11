# PR71 main-forward qualification

Accepted starting tip: 3438b9e689aa210bd64e822fca2a4598f3afac64. Main incorporated by forward merge: 903fd729c56cf719bbb15e9f986a70b9160497c1. The integrated branch includes inherited C07 PR69 source; the path table explicitly retains that scope for Mike's delta review. A passing qualification is not an acceptance or merge GO for that ancestry.

`native-pg-forward-001.json` is the fresh O01a native run at its recorded source commit. It preserves O01a's published contract: host Python staging/encryption and seven synthetic-age tests with already installed Python3.12/age; an owned network-none PG18.4 container supplies the real base backup and archived WAL. No host dependencies, live data, real key custody or binds are used. Historical native-pg-final-003 remains historical.

`inherited-pg-forward-001.json` records 368 clean Core/C07 tests in the copied restricted pod plus the three intentional frozen old-table guard BODY refusals. `inherited-c07-faults-forward-001.json` records the additional C07 semantic fault matrix. Both use the recorded committed controller and restored NEXT input bytes. Stacks ran sequentially, total at most 2CPU/1GiB with verified owned cleanup. Historical ≥35%-free admission is unverified: these original controllers recorded no measurement or gate. The current C07 repair controllers add measured admission; see `../c07-source/reproduce.md`.

`backup-forward-mutants-attempt-001.jsonl` is retained as INCONCLUSIVE for encryption bypass because the newer caller-metadata test then reads a nonexistent output file. The qualified `backup-forward-mutants-002.jsonl` isolates only that fault's declared expected encryption test and runs all seven frozen tests clean before/after the seven faults. All other faults retain the full seven-test suite; no assertion or product source is changed to manufacture a kill.

The publication successor adds proof documents only. `forward-qualification-binding.json` binds each raw receipt and controller. Verify from an isolated publication checkout, without executing product code:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/next/evidence/o01a-source/verify-forward-qualification.py .
```

The verifier distinguishes the native tested commit from inherited-suite and publication commits, compares actual Git/input hashes, checks every expected BODY attribution, verifies restored sources/cleanup and refuses missing proof. The original canonical filename refuses at publication because it exists. The repaired controller accepts a guarded fresh basename: `python3.12 docs/next/evidence/o01a-source/run-native-forward.py native-pg-repair-fresh-002.json`; it refuses an existing file or a path outside this evidence directory. The current repair verifier is `../c07-source/verify-pr71-c07-repair.py`; this forward verifier is historical at be52491. No live backup/restore, adoption, migration or main merge is authorized.
