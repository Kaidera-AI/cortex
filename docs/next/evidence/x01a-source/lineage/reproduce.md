# X01a source and evidence

Source commit `27867d8552018e2b3dbfdcf87aa6c5cb64b25351` is stacked on
O01b PR #72 head `1e44f64d373806019e98fe02265e3368b301b940`.
`next/src/cortex_core/recovery_fixture.py` is an inert controller module:
two-tenant synthetic fixture, external fsynced ACK/ambiguous ledgers,
exact-prefix reconciliation and edition-specific dry-run plans. Its
`native_restore_plan` does not stage or execute a restore. X01b must bind an
actual signed edition image, verified O01b bundle, isolated owned volumes,
recovery configuration, executor and Kai's host GO. No RPO/RTO feasibility
or edition qualification is claimed here.

Reproduce source checks in the X01a branch:

```sh
PYTHONPATH=next/src python3.12 next/tests/test_receipts.py next/tests/recovery_fixture
PYTHONPATH=next/src python3.14 next/tests/test_receipts.py next/tests/recovery_fixture
PYTHONPATH=next/src python3.12 next/tests/test_receipts.py next/tests/backup_producer
PYTHONPATH=next/src python3.12 next/tests/test_receipts.py next/tests/backup_validation
PYTHONDONTWRITEBYTECODE=1 python3.12 next/scripts/mutate_recovery_fixture.py
ruff check next/src/cortex_core/recovery_fixture.py next/tests/recovery_fixture/test_fixture.py next/scripts/mutate_recovery_fixture.py
```

The final raw green receipts are `green-py312.txt`, `green-py314.txt`,
`inherited-o01a-py312.txt`, and `inherited-o01b-py312.txt`. RED-first test-body
receipts are `red-003.txt`, `fault-red-001.txt`, `controller-red-001.txt`,
`fixture-membership-red-001.txt`, and `detach-red-001.txt`. The first
`red-001.txt` and `red-002.txt` and earlier mutation runs are retained as
nonfinal development evidence; the final source-copy run is
`mutation-final-004.jsonl`. `dry-run.json` exercises only local pure functions
and temporary synthetic ledger files. It executed zero host commands; its
`passed` field demonstrates the arithmetic path, **not** a native recovery.

Run `python3.12 verify-x01a.py` from anywhere to bind final receipts to the
committed source and prior stacked base. The file is a verifier, not the
implementation's own success flag.
