# PG-O oracle repair for Hale delta review

Scope: Kai f6fbbfc9, offline author repair only. Base30338b51; no product or
migration SQL. All105 old IDs remain;70 new IDs make175. Cox must run ALL175 in
his NEXT PG-A lab session on the ratified18.6/vector0.8.7/CPython3.14.8, pure
psycopg3.2.9 + image-owned libpq18.6 tuple. No separate lab/PG run in this row.
The original42 PASS /46 surface-absence /17 semantic RED receipt is historical,
not renewed. Every one of its17 semantic IDs is retained and its underlying
product source is unchanged; none is xfailed, skipped, weakened or fabricated.

| Finding | Disposition and exact source | Evidence scope |
|---|---|---|
| F01 P2 | FIXED: existing Grants default canary checks each unauthorized recipient for function/type and grant option, both actual creators; finite superusers and current object creator explicit exceptions |26 wrong-recipient static controls, each baseline/kill/exact restore; no PG qualification |
| F02 P2 | FIXED: health and observe VOLATILE; every middle search_path entry belongs to the closed app-schema set AND has actual migrator owner; duplicate/untrusted entries refuse | STABLE-health and untrusted-middle controls killed |
| F03 P2 | FIXED: observer/health revision-only two-session barriers keep active=true and OID/name/install fixed; separate revoked health entry; both unsupported snapshot modes for both read ports | frozen candidate-PG bodies; observer + health stale-output static kills |
| F04 P2 | FIXED: all4 map-before-lease variants; queued release/assert + old renew; expired matching release; assert itself locks before any later write; positive1ms | frozen real-PG barrier bodies; queued release/assert static success-after-expiry killed |
| F05 P2 | FIXED: valid-bound aggregate runtime fault and timeout separate from retained missing-kind test; no successful zero; restored schema fault and bounded lock timeout | both returned-success substitutions killed; unexpected SQL states remain inconclusive |
| F06 P2 | FIXED: M07 includes actual function ACL nodes, M16 actual other_objects; separate existing PUBLIC function, wrong private recipient, request CONNECT, service CREATE and TEMP controls | each distinct catalog-row omission killed |
| F07 P2 | FIXED: unknown applied ledger ID and changed applied checksum each refuse, preserve altered ledger/business and install no new objects | both static boundary acceptance substitutions killed |
| F08 P2 | FIXED:44 per-role unsafe preimages, post-prepare flags for9 roles, unrecorded finite-session sentinel + wrong-operation/unknown-state HOLD | frozen owner-client bodies; independent controller witnesses forbid broad termination; drain/reentry/restore static controls retained |
| F09 P2 | FIXED: shared seed adds same-install embedding/nonembedding running, failed, canceled, unresolved; expected pending stays3/1 | pending-only omission returns11 and is killed |
| F10 P3 | FIXED: type(value) is int and nonnegative counts; strict positive live/expired fence int; absent fence remains exactNone | bool/float count and fence controls killed |

The25 specifically named missing groups and exact IDs are
next/tests/runtime_grants/REPAIR_CONTROLS.json. Those are groups, not a claim that
25 methods alone decide all subvariants. New tests reuse the existing fixture,
private ports, owner client, creator canary and U13 helpers. No new test framework,
authority verifier, schema parser, role, producer kind or runtime path is added.

The40-row bidirectional trace is REPAIR_TRACE.json:36 deciding frozen obligations,
0 partial,4 explicit owner deferrals. Those are assertion-coverage dispositions,
not native PASS or production acceptance. T05 stays original installer/R01/HBA /
resolver/custody owner; T18 stays PG-D SET LOCAL/check-out/reset/discard/cancel
(the fixture still uses SET ROLE); T34 stays Cox/actual embedding producer kind;
T40 stays PG-C/D/E/F and R01/native/CI owners. The pgo.fixture.embedding value is
still explicitly synthetic. No extra login/grant fills missing producer authority.

Only five earlier source definitions change, each required by a named finding:
Grants.test_public_function_and_type_defaults_closed_for_both_creators (F01),
signature_case (F02), Ports.seed_jobs (F09),
Ports.test_release_retains_row_and_next_acquire_increases_fence (F10), and
Ports.test_observer_pending_embedding_jobs_is_not_total_jobs_or_foreign (F10).
Exact before/after spans and hashes are in evidence OLD_DEFINITIONS.json. Every
other earlier definition span, support/worker/test_receipts, original19 SQL and
ratified matrix byte remains unchanged. FROZEN_NODES and ORACLE metadata are
intentionally re-frozen, not represented as byte-identical old manifests.

## Offline mutation protocol and limits

`offline_controls.py <owned-output-dir>` runs the actual imported frozen method /
helper callables without the DB fixture setUp. It extends Hale's static catalog
row technique, using existing shared test_receipts phase attribution. Synthetic
catalog, port return/refusal and owner-client result substitutions stand in for
one omitted future rule. No PostgreSQL statements, sessions, waits, DDL, client
implementation or product mutants execute; real connections/sockets/processes
are denied. The row/port adapter is test-only, never runtime admission.

There are21 named family faults plus39 focused variants =60 static assertion
mutants. Each uses fresh baseline/wrong-row/restored state. Only the expected
actual test-body AssertionError counts; raw errors/import/fixture assertions
remain inconclusive. Both baseline and restored query traces and all observed
state are byte-equal, and every oracle file hash stays unchanged. These180 method
executions establish assertion adequacy; they do NOT renew all175 PG nodes or
prove that the real barriers/SQL/client implementation work. M19 substitutes the
migration callable's refusal/result boundary because candidate20 is not present;
it does not count an actual migration-source mutation. M03/M04/M05/M11/M14/M15 /
M17/M18/M20 include static port/admission results as well as catalog rows. This
scope is explicit rather than relabelled product/PG mutation qualification.

Preflight attempts are retained separately: one unsupported lock-query route and
quoted SET LOCAL creator identifier handling were repaired only in the row adapter.
No such error/survivor is credited. Before assertion repair, Hale's exact5 controls
were freshly replayed:3 kills and the expected2 survivors. Final controls use the
new source; tests were not weakened to obtain kills.

CONNECT / pg_database_size remains Cox's ruling. The ratified matrix is unchanged;
no permission, membership or alternate size path was selected by this repair.
Keep that owner gate when the next PG-A lab runs; no standalone renewal session.

LEAN: reused existing PG-O/C04/U13 fixtures, creator/matrix assertions, lock
barriers, private owner client and shared test_receipts; did NOT add product SQL,
parser, verifier, role, port, host or live-PG run; could merge repeated barrier
setup within this existing fixture after qualification, without merging controls.

Author gates: G1 all old IDs and exact allowed span changes PASS; G2 ten findings /
25 groups / trace / owner deferrals PASS; G3 static21 families and60 baseline /
wrong-row / exact-restore controls PASS; G4 syntax/discovery/custody and scope
sealed in external evidence; G5 own branch push/same-row delivery closes only
from external API/remote receipts. Independent Hale delta acceptance is pending.
