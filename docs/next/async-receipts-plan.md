# B01-PR34-1: reject async fixture reuse

Authority: user asks for latest cox DO NOW; Kai H-D455 2026-10-09 23:47 orders one small behavior PR after the accepted PR34 pure move. Cox authors; Mike reviews; Kai adjudicates. New source only under `cortex/next`; no v0.1.003, live service, DB, installation or release changes.

Re-grill: the inherited helper correctly rejects ordinary async fixtures but mistakes assertions in a reused target method for body failures when the framework fixture wrapper comes from `async_case.py`. Mike's unchanged reproducer is the experiment and required RED. Scope is classification provenance only: keep `suite`, `report`, `classify`, marker format and normalized expected IDs unchanged. Genuine sync/async body assertions remain kills; setup, teardown and cleanup assertions stay inconclusive even when they call the expected method.

Pre-edit source: clean owned `cox-async-receipts-20261009` worktree from main merge `8f08b5b4a94819c122f2f13cffe9fb833915c3dd`; source callers are C01 mutate_contracts, receipt-shape and runner-phases tests plus shared tests. C03 and Nemo have not yet adopted this merged path; their adoption is separate. No definition deletion or migration modification.

Files and order:

1. Preserve Mike's `fixture-reuse-probe.py` bytes as `next/tests/receipt/fixture-reuse-probe.py`. A discovered wrapper executes it in copied temporary layout, preserving all five cases and assertions. Add async teardown reuse and both synchronous/asynchronous cleanup reuse controls, plus a genuine async body assertion through a helper. Commit tests and this plan; run RED on the unchanged helper and capture raw child/outer output.
2. Change only fixture-frame recognition in `next/tests/test_receipts.py`: match the actual setup/teardown/cleanup wrapper code objects on both `unittest.TestCase` and `unittest.IsolatedAsyncioTestCase`. Code identity avoids file-name guessing and preserves body attribution. Keep RED tests byte-identical after this point.
3. Add a reproducible helper mutation runner under `next/scripts/mutate_test_receipts.py`: retain the seven existing semantic mutations and add removal of async fixture wrappers. Each mutant must fail its declared outer test-body AssertionError with no errors; raw outputs and source/output hashes retained. Restore source and rerun clean baseline.
4. Run all 33 C01 tests and 26 C01 mutants, all shared tests and helper mutants, in one bounded copied native arm64/Python3.12 container (<=2CPU/1GiB, nonroot/read-only, no SSD binds or ports; disconnect network before checks; remove own container). Fresh read-only verifier, origin source hash check, one PR on current main, Mike review. If target main changes, integrate and rerun affected gates.

Risks: incorrectly rejecting a genuine body assertion; allowing a fixture that reuses its method; hidden subclass wrapper behavior. Controls exercise real unittest execution rather than synthetic fixture labels. Support is bounded to tested Python3.12; arbitrary custom framework provenance is not claimed.

Proof commands: copied `python next/tests/test_receipts.py next/tests/receipt`, `python next/scripts/mutate_test_receipts.py`, complete C01 suite/mutator. RED must be actual outer AssertionErrors, including Mike's unchanged child exiting1; GREEN must retain child output showing every fixture inconclusive and body killed. Import/setup/runner errors are never kills.

Rollback: two-way source revert of this isolated helper change, retains the known inherited finding. No data or infrastructure effect. Review acceptance and any later merge remain separate gates. Kai owns merge authority; Mike alone closes B01-PR34-1. C04 drafts/25-test missing-port RED are preserved while this higher-priority item runs. Nemo adopts only after this behavior PR merges.
