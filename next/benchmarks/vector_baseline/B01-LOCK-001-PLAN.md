# B01-LOCK-001 bounded repair

Authority: Mike DO NOW H-D455, 2026-10-10 10:08 local names this Medium repair, SMALL PR, RED-first, Vera reviews. Accepted alternative: persistent dirty marker plus reconcile-on-acquire.

Intent/spec: after removal/inspection failure, a second differently named local B01 lifecycle must not create resources while the prior owned effects remain or inventory is uncertain. The existing local fcntl mutex is process-scoped; a durable identity-only pending marker must preserve admission exclusion after unlock/owner exit. This is the existing per-user boundary, no global-engine lock claim.

Paths: next/src/vector_baseline/postgres.py; new next/tests/benchmarks/test_postgres_lock.py; this next/benchmarks/vector_baseline/B01-LOCK-001-PLAN.md. Existing tests and assertions unchanged. No change to SQL/search/oracle/load/report/resource settings or foreign cleanup.

Order: commit frozen public fake-engine failed-cleanup/competing-acquire controls, confirm named body REDs; then fix without modifying them; run full benchmark consumer suites and focused actual Podman creation/cleanup faults under fresh memory/team-slot admission; root-owned named mutations prove controls can fail; exact-head source/CI/custody return and Vera review. No self-acceptance/merge/native B02 execution.

Implementation: write/fsync an identity-only pending marker under the acquired local mutex before first create. On acquire, inspect every resource of any valid prior marker by its stored name+nonce; refuse startup on owned residuals/uncertain/malformed identity, preserve foreign same-name effects, clear marker only after verified prior absence. Current close clears only its own exact marker after verified cleanup; failed cleanup discards password and closes the process mutex while keeping marker. Failed competing admission never removes another lifecycle's marker. Old owner can retry close; a later lifecycle can reconcile externally verified absence.

Risks/proof: malformed/ambiguous marker deliberately blocks admission; filesystem/inspection errors fail closed. Marker contains no password. Preserve immutable-ID removal and single-use lifecycle. Fault matrix covers volume/secret/container removal, inspection uncertainty, corrupt marker, foreign collisions, retry/unblock and admission after apparent owner exit. Both normal cleanup and six native lost-create-acknowledgement faults must remove exact resources. Reviewed source only; deployment/release/benchmark acceptance gates remain external.
