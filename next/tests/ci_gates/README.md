# Day-1 CI evidence controls

Owner: Mike; accountable lead: Cox. Scope is source/unit evidence admission for Option A
v0.1.004. This module cannot issue release GO. It adds no workflow, service or dependency.

Run from the repository root with Python 3.13.7 and Python 3.14.0:

```sh
python -m unittest discover -s next/tests/ci_gates/tests -v
python -m next.tests.ci_gates.receipt composition COMPOSITION_PACKET.json
python -m next.tests.ci_gates.receipt collect COLLECTION_PACKET.json
python -m next.tests.ci_gates.receipt history HISTORY_PACKET.json
python -m next.tests.ci_gates.pilot mutate --root REPOSITORY --spec SPEC.json --output FRESH_OUTPUT --scratch FRESH_SSD_SCRATCH
python -m next.tests.ci_gates.receipt mutation MUTATION_PACKET.json
```

Repeat `--spec` for every changed source. Output and scratch must be fresh directories
outside the repository; use the project SSD output/tmp homes. Scratch may be removed
after the lead accepts the immutable logs and source copies, or by the dated project
cleanup process. The author tree is never mutated.

The module reuses the existing phase-attributing unittest result/classifier and strict
JSON/refusal helpers. Read [the contract](/Users/amadmalik/DevVault/helix/cortex/next/tests/ci_gates/contract.md).
Raw provider collection uses read-only `gh api` and inherits its existing authentication;
the causal children receive a minimal environment without credentials.

Integration owner must admit these three files into the existing strict manifest:
`next/tests/ci_gates/tests/test_receipt.py`,
`next/tests/floor/tests/test_admission.py`,
`next/tests/live_contract/tests/test_live_contract.py`.
The shared manifest/workflows are outside this dispatch. Unit success is not hosted
green, real-PG execution, native installation, paired identity, smoke or release credit.
The other authors still owe their own per-source causal proofs.

Day-2 SharedAdmission checks all three Mike suites and Nemo compatibility contract are RUN and preserves the independently pinned prior admissions/controls. Strict inventory still discovers every next test. Native PostgreSQL execution remains separate from offline passes.

Day-2 native installer-role join: `pytest-pg` runs `pytest_pg_worker.py --mode
installer-roles --pg-bin EXPLICIT_BIN --site EXPLICIT_TEST_SITE --output FRESH_DIR`.
The nine source-pinned role methods have their own exact setup/call/teardown gate,
180-second worker ceiling and private cluster scratch. Missing tools, skips,
deselection, phase errors, source drift and failed cleanup refuse. Synthetic
receipt controls are unit evidence only; the hosted native packet is still required.

Day-2 admission continuation: the three actual Core runtime suites are ordinary
RUN, alongside the Nix signature/spine suite and all earlier admissions. Finn's
five `packages/cli/tests/test_transport*.py` suites and the actual seed contract
suite have an explicit `loopback` home in the same inventory. The existing runner
`--loopback` uses the unchanged strict per-file worker and phase classifier, an
isolated Linux network namespace with only `lo`, no-new-privileges, the same
resource limits, both required Python versions and the unchanged180s file cap.
The required workflow step still runs after an earlier failure and uploads all
receipts. It grants source-contract credit only. A missing seed module remains
an executed ModuleNotFoundError and a RED gate; it is never skipped, declared
unbuilt, counted as a BODY kill or accepted as installed proof. The transport
family outside next/ is inventoried explicitly; unrelated retained CLI history
suites keep their existing homes. Symlinks, missing, stale and duplicate entries
refuse, and ordinary RUN cannot migrate into loopback as a coverage reduction.
