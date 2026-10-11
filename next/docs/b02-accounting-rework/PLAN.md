# PR #68 — warmup accounting and queue-time repair

Authority: Kai DO NOW 2026-10-11 03:42, reaffirming 00:23; Mike B02-PR68-1/2 at f6bf608. Scope: Nemo@helix lane F source/synthetic controls, no native benchmark or measurements. Stage: accepted bounded Plan -> Build -> Verify -> Mike review; no author merge.

## Order and files

1. Merge current target main into owned feature worktree before RED (completed; no NEXT conflicts).
2. Add `next/tests/benchmarks/test_b02_offer_accounting.py`: positive full warmup and configured-zero controls; missing/duplicate/empty/wrong-phase/wrong-offset/client/ID/count/config negatives; coherent legacy timestamps; missing/invalid warmup lag; actual controlled-clock queue timing. Commit these tests before production changes and freeze bytes.
3. Existing `test_b02_report.py` has two warmup fixtures copied from measured rows while config says zero; give only those fixtures configured warmup=1 and phase=warmup. Likewise the existing late-warmup prerequisite fixture. Preserve every original assertion/body scenario; AST assertion audit against old head before handback.
4. `next/src/vector_baseline/report.py`: validate BOTH phase inventories against configured durations, integral offered counts, complete unique integer IDs, phase/client/offset positions and legal statuses; combine with unchanged dispatcher-lag policy before decision. Count/IDs/phase/positions must be independently mutant-killed. Valid zero warmup accepted. No engine/GTM qualification.
5. `next/src/vector_baseline/load.py`: only queue formula uses dispatched_at; scheduled end-to-end latency/deadline unchanged. A controlled clock proves dispatcher lag stays outside queue wait.
6. Add literal source mutants for every new guard and queue regression; preserve old recipes. Run focused/full existing benchmark unit suite, old source/parent mutants, body phase/custody checks, Ruff/diff checks; rerun after final target sync. Archive source and exact results, push same PR68 branch, update PR body, return for Mike via Kai. Own WT removed after archive and push.

## Risks

Malformed warmup receipts that previously qualified now INVALID_HARNESS with all components NOT_RUN. Tests-only fixture context repairs need explicit assertion preservation audit. No source/native acceptance beyond these synthetic controls. Native skips retained, not counted as PASS.

Final defect hunt: add frozen `test_b02_offer_positions.py` RED before extending the position check to coherent absolute scheduled timestamps within each phase (relative to the first recorded ID); keep the original 16 frozen controls unchanged. A reversed complete inventory stays valid. Add an absolute-position literal mutant and rerun the full suite and inherited proofs on this final head.
