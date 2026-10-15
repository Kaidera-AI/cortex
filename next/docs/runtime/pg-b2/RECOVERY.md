# Finite-owner recovery measurement input

The protected owner calls the existing `_Primitive.measure(original_effect_context)`.
The context retains the original consumed P1 event and first five-preimage bundle.
Current authority comes only from `_installed_owner_inputs`, through the existing
common owner gate. An admitted `recover` request must name that original attempt
as parent and carry fresh request/attempt, epoch, session, nonce, host fence and
scope, with its own deadline. No public function or caller-selected provider,
channel, phase or history root is introduced. Normal dispatch retains its exact
historical `_continuity` and reservation-deadline checks.

Recovery measurement returns an immutable pair `(answer, original_result_bytes)`:
`APPLIED` or `NOT_APPLIED` carries the exact existing finite-owner P1 result bytes;
`UNKNOWN` carries `None` and means HOLD. Acceptance comes from the original
attempt's existing `result_ref` or `reconciliation_ref`, measured through the
same P1 reader and existing result shape. The original identity/effect/request,
five observations and independently current live identity/bytes must agree.
Definitive NOT_APPLIED additionally agrees with the five original preimages.
No acceptance reference means UNKNOWN even when resources match or are absent.
NULL/unmeasurable identity and byte drift cannot become an APPLIED answer.

The measurement emits only existing snapshot SELECTs/material reads. It does not
retain/append facts, reserve/consume, restore, dispatch or alter SQL state. The
fixed TXN-001 idle/autocommit capability still precedes the snapshot. Current
authority, its first fresh deadline and the unchanged history head are rechecked
after IO. Stale/foreign/already-used authority or corrupt custody refuses.
Repeated read-only observation itself is non-consuming; P1-recorded used
identity/authority is rejected without another replay store.

Finn owns acquiring/persisting independent original-owner reconciliation truth
in the existing P1 history and any later fresh child reserve/consume flow. This
input neither fabricates that truth from a snapshot nor supplies an installed
owner. Hale reviews the source contract; PG-F owns native/material/tuple
qualification, and PG-C owns final manifest/integration accounting.

Inside the eventual fixed lifecycle composition, under its existing operation
exclusion, the consuming flow is:

```python
answer, original_result_raw = primitive.measure(original_effect_context)
```

The primitive is the existing bound finite owner for the original attempt. Its
public shape is unchanged. `original_effect_context` holds the original consumed
journal reference and preimages reference; it is not renewed authority. Current
protected inputs supply the fresh recover request. The complete original request
body stays exact except identity, operation and journal_ref; installation,
operation, transaction, caller, roots, target, release and effect/idempotency
subject remain bound. The incoming parent must be the original attempt.

For APPLIED, consume the returned original bytes and their existing P1 reference
without re-dispatch or a duplicate result. For NOT_APPLIED, Finn's normal renewed
child path may later reserve/consume once under its own authorized context;
measurement creates no such permit. For UNKNOWN, HOLD without a new create or
inverse. A repeated read-only observation is not a consumed authority replay;
any request/attempt or nonce already represented in P1 history is rejected.

Source verification includes the two Finn refusal diagnostics promoted to BODY
requirements, preserved original-path refusals, durable APPLIED and NOT_APPLIED,
consumed/unknown reconciliation refs, absent truth, NULL/OID/missing-role/material
and database drift, foreign/stale/reused authority, new-vs-old deadlines, history
advance during IO, original normal-result bytes and TXN-001 compatibility. Actual
P1 test-owned durability and original byte refs are used; SQL/catalog/material
recorders and the nonauthoritative signed-member fixture do not qualify native
authority. Final counts and exact published SHA are in the named handoff return.
