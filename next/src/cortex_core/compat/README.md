# Compat specification

This folder contains the H-D614 option-A mapping contract, not a runtime module.
Read contract.md; Finn owns the daily adapter and Cox owns canonical API/records.
The normative JSON is testable documentation, never a second runtime registry.

Run specification assertions through the existing strict unittest receipt helper
from a Cortex checkout:

    python3 next/tests/benchmarks/mutation_receipts.py next/src/cortex_core/compat/tests

These document assertions do not qualify HTTP behavior, installed operation,
provider isolation, migration safety, or release. Mike's bound OpenAPI/live and
paired-host gates remain required. No new parser, transport, store or provider.
