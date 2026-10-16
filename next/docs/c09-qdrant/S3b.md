# S3b admission oracle — partial, product unchanged

S3a source remains at the reviewed `656000f77b35d71d08fbf41eb1ce8e62b8d793db`
behavior. This slice adds verification controls. It does not admit or execute the
C09 SQL draft, renew a schema seal, or demonstrate durable Core outcomes. The
full chain does **not** yet hold. Capability stays UNAVAILABLE.

The executable admission check fails in its test body because the migration
manifest has no `coordination/c09-external-v2.sql` entry. The existing 19-step
prefix remains unchanged. Cox owns the concurrent PG-C 20-step/44-table prefix
and its compatibility seals. Appending C09 independently as another step 20 would
collide with that work; extending it adds two tables and needs the owner-selected
composed schema/contract. A source branch is not admission authority.

`tests/module_consumer/test_c09_external.py` contains nine prepared real-PostgreSQL
controls, **not executed**: Python/SQL point addressing; fresh-connection commit
visibility and replay; actual published successor/stale receipt with contiguous
progress; vector-head failure rolling back C07 outcome and prefix; poison and
fresh-client continuation; direct-v1/foreign-receipt refusal; ownership/ACL/RLS;
immutable binding and snapshot rebuild refusal; retained-floor expiry. Synthetic
destination receipts can qualify Core only after actual database execution;
they cannot qualify Qdrant effects. No test applies the raw SQL draft directly.
The admission and copy-isolation checks run without a database.

Before any inherited fixture can reset schemas, the real-PG controls require an
already authorized, separately owned copy using the existing TEST_DATABASE_URL:
postgres URL, task-prefixed database, and a Unix socket strictly beneath this
slice's dated temporary directory. Missing/shared/traversing/hostaddr targets
refuse before connection. This test guard grants no execution authority. The
existing C04 fixture and C07 seed helper supply the database setup; no second
migration runner or receipt/checkpoint mechanism exists.

The stale test deliberately requires the read boundary to refuse when a newer
actual vector head has been recorded for an earlier event but the successor's
Core outcome is still missing. Current draft inspection shows no explicit check
for scoped published events beyond `applied_cursor` in `c09_state`. That is a
proposed deciding negative, not an executed database finding or a claimed fix.
Other missing concurrency and v1 regression schedules still belong in the
owner's database qualification; these nine prepared methods are not exhaustive.

Seven controlled receipt-boundary preservation methods retain the accepted
Cox/Hale error precedence: Core/database failure remains `core_unavailable`,
feed authority refusal retains its existing type, rejected receipts retain
`invalid_target_receipt`, and engine/adapter failure maps to scrubbed
`capability_unavailable`. They show a failed record context cannot return success
or invoke the post-checkpoint callback, and a successful source recorder exits
before its callback. They confer no commit/durability credit.

An initial broad Core-to-capability mapping experiment was withdrawn when Hale's
S3a return arrived and confirmed Cox's explicit precedence requirement. Its
incorrect RED/GREEN expectations and mutants receive **zero correctness credit**.
The full product source was restored byte-for-byte to S3a; original S2/S3a tests
were not changed. Raw attempts and withdrawn bytes remain in the evidence pack.
Hale reported zero scoped source gaps on S3a and left the same durable Core gate
open. Cox has meanwhile published a proposed C1 driver correction; independent
review/adjudication of that head is not inferred here.

Run source controls with the canonical receipt runner. Run admission/isolation
separately; admission is deliberately RED until PG-C admits the append:

```
PYTHONPATH=next/src:next/tests python -B next/tests/test_receipts.py next/tests/receipt
PYTHONPATH=next/src:next/tests:next/tests/module_consumer python -B -m unittest test_c09_external.AdmissionTests test_c09_external.IsolationTests
```

Kai must name the owner-selected PG-C composition/admission scope and clarify
whether the no-native instruction permits an isolated copied PostgreSQL proof.
Until then no PostgreSQL/Qdrant process or connection, schema/seal edit, native
qualification, B02/S4, merge, deployment or release action is authorized by this
packet. The return carries exact source/evidence hashes and every open gate.

LEAN: reused C04/C07 fixtures, the sole migration port, existing checkpoint and
canonical receipt runner; did NOT add product code, parser, verifier, checkpoint,
transport, config path or runtime mechanism; could merge the prepared C09 tests
into the owner's admitted migration qualification once its scope is named.
