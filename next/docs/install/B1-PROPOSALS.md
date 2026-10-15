# B1 Path A: pre-consume data

Authority: Kai's `caeaf4ae-72ec-4d7c-9f56-b880018267ff` ruling of 15 October 2026.
This implements the source data slice before P2a. C0/R0/B0 execution requirements
and the original full F2 assertions stay intact.

## P2a consumes one entry

`cortex_core.install.owner.propose_effect(original_request: bytes) -> OwnerProposal`

The sole public input is the original bounded canonical P1 request. A fixed,
zero-argument private `_installed_owner_inputs()` supplies independent current
peer/code/image/owner observations and held originals. Its production default is
UNBOUND before any candidate, schema, path, clock or R01 candidate access. There
is no public binder, provider argument, environment override or grant mint.
The eventual protected adapter must prove the accepted C0/R0/B0 composition,
including KOS CPython3.12.15 and cryptography50.0.0/cffi2.1.1/pycparser3.0. This
source row installs no adapter, physical identity, clock, image or privilege.

The common policy validates original references through the existing P1
RecordRef checker/canonical codec and the original P1 parser. It checks exact
peer/request/scope/consent/current-state correspondence. R01 admission reuses the
existing `signed_member.request_member` and sealed `read_member` for the external
lock; their fixed U3 backend owns complete release admission. No crypto/parser
or image verifier is added. After that read-only audit, the gate independently
reobserves owner data, reruns current checks and rejects changed context.

`OwnerProposal` is immutable DATA: original request SHA, immutable original
context projection, observed pre-reserve state revision and `ProposedDeadline`.
Its stage is `PRECONSUME_DATA`; `effect_authority`, `native_qualified`,
`reservation_created` and `consumed` are false and cannot be constructor options.
There is no reservation ID, consume marker, history or execution receipt. A
caller-constructed Python value confers no authority.

`ProposedDeadline` contains owner epoch, proposed UTC/monotonic start, UTC expiry
and monotonic expiry. UTC expiry is the minimum of start+30 seconds and all known
consent/policy/session/host/applicable-private UTC bounds. Monotonic expiry is
the minimum of start+30 billion nanoseconds and all independently known bounds
in that same epoch. Both clocks must remain before expiry; equality refuses.
The entire UTC uncertainty interval must meet lower not-before and strict upper
expiry bounds. Unknown quality, rollback, missing epoch/bound or malformed exact
integer data refuses. A private bound remains distinct from the host fence.

## Durable work stays in P2a

P2a must independently bind and retain the original request/context/observations,
perform the current revision CAS and durable intent/reservation (C0 section3.4),
repeat final owner/backend checks and durably consume once (3.6), then retain
the original independently measured result (3.7). It must preserve an existing
reservation's original deadline across later admission calls and cannot turn a
new proposal into renewal. It owns protected original role/ACL/DB/HBA/carrier
preimages in the same existing P1 `JournalEvent.observations_ref/result_ref`
history. U13 remains exactly `{pair,phase,steps}`.

All30 original F2 methods are named in `fixtures/subjects.json:B1_held_F2` and
remain BODY RED with no PASS credit: “waits for P2a durable reserve/consume/result
(C0 §3.4/3.6/3.7)”. `compare_execution`, dynamic F2 methods, every literal B0 case
and all session test bytes remain unchanged. The artifact-only P1 index literal
in the admission oracle is rebased to the already accepted E2 fixture tuple;
the current P1 fixture and all original47 P1 assertions remain untouched.
No `GateB_accepted_execution` is emitted. Native Gate B acceptance remains held.

## Source evidence scope

The new tests exercise pure proposal policy and time arithmetic using literal
owner-fact data and Nemo's existing explicitly nonauthoritative original-member
double at the sole RC2 port. This proves source policy/composition only: there is
no signature PASS, actual protected observation, durable history, native or
product effect credit. Production still refuses its missing bindings first.
Gate and clock mutants must break their matched original BODY assertions and
restore exact product bytes; tests are frozen before implementation.
