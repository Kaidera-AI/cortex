# Gate B B0: owner contract amendments and frozen oracle

Status: CONTRACT + SURFACE-ABSENCE ORACLE FOR REVIEW. Gate B is NOT_ACCEPTED.
There is no effect/source/native/signing/bootstrap GO in this document. Kai's
dispatch c7dd415e-4f69-4a9e-8b29-f6a344e1d58c authorises these five contract/test
paths and normal publication of `finn/gateb-b0-20261014` only.

The normative owner semantics are Cox's [C0 return](</Volumes/WD-B-4TB/DevVault/helix/docs/handoffs/2026-10-14_cox_kai_gateb-c0.md>),
accepted by Kai with four amendments in the initial B0 dispatch; C0, R0 and the
Q3 qualification list remain accepted through the oracle correction. C0's exact input hash is
bound in `fixtures/gate-b-subjects.json` under `C0.sha256`. This document references
C0 §§1–8; it does not replace or duplicate that contract. The [Finn unit plan](</Volumes/WD-B-4TB/DevVault/helix/docs/handoffs/2026-10-14_finn_kai_gate-b-plan.md>)
and [Vera boundary ruling](</Volumes/WD-B-4TB/DevVault/helix/docs/handoffs/2026-10-14_vera_kai_seal-boundary-ruling.md>)
remain entry dependencies. ONE Vera review covers C0 and this B0 delta.

## 1. Finite reservation policy: 30 seconds

V's independently admitted owner policy has a **maximum reservation lifetime of
30 seconds**. Reason: the expensive audit finishes before reserve, so this short
ceiling bounds final-check/dispatch exposure without using an audit as a renewal.
It is a contract ceiling, never consent or a default grant. No policy/config file,
new wire field or caller TTL is introduced by B0.

At the completed audit/reserve point, the absolute UTC expiry is the minimum of
reserve-time UTC + 30 seconds and every explicit consent, policy, session, host
lease and applicable private lease expiry. The monotonic expiry is the minimum
of reserve-time monotonic + 30,000,000,000 ns and every applicable independently
admitted monotonic deadline, in the same owner epoch. Both bounds must hold. The
whole final UTC uncertainty interval must be inside every validity interval;
upper endpoints equal to expiry refuse. Unknown quality, rollback or unavailable
time uses C0's `time_unavailable`; it cannot turn into an infinite reservation.

An earlier deadline shortens the ceiling. Clock values advance, rather than being
byte-equal to the old snapshot. C0's post-reserve CAS revision is retained: context
observes r; reserve commits r+1; consume expects that recorded r+1. A new blocking
audit, yield/deferred pause, revocation, epoch/session change or relevant state
change invalidates the old reservation. Do not renew or redispatch consumed
authority. The exact C0 refusal and deciding-phase counters are frozen in the
literal case expectations, including strict UTC and monotonic boundary cases.

## 2. LEAN fold: existing P2 event history

Take C0's fold offer. Current-context, consent/grant, reservation and attempt
observations are original facts referenced by the **existing P2 JournalEvent
observations_ref/result_ref in its one protected owner event history**. Their
semantic closed fields remain C0 §2's fields. No additional journal, grant ledger,
record class, public bearer permit, parser, verifier or service is added.

P2's existing canonical codec/RecordRef/InstallRequest/JournalEffect are the sole
wire machinery. Owner-created facts are projections of that history, never
caller authority. Bind original request/transaction/operation/attempt/parent,
current context and exact finite primitive to the same durable attempt. Preserve
consent/revocation, clock, generation/revision, host/private holder/fence, reserve,
consume, actual consume clock and independent result/reconciliation observations.
Retain referenced originals before publishing an event. References target held
facts or earlier events; public receipts may refer to completed events, preventing
event/receipt hash cycles. No observation may replace actual current authority.

Use C0 §5's U13 operation exclusion and CAS/fsync design. The existing U13
AtomicUpgradeJournal stays exactly `{pair,phase,steps}`; none of these facts is
smuggled into it. Cross-operation/resource exclusion and independently enforcing
backend admission remain required. P2a must implement/prove atomic fact/history
integration; this fold is a contract, not a claim of that implementation.

Reserve/intent, consume and result bookkeeping use the same authoritative owner
transaction, with no recursive grant service. Each externally dispatched product
journal/cleanup/enrollment/activation primitive still gets its own current gate.
Uncertain consumption/result preserves the original attempt and HOLD; a readable
unknown result differs from unreadable/corrupt journal. Never erase earlier valid
reserve/consume/dispatch counts when a later deciding check refuses.

All 20 C0 groups are represented in the frozen fixture. Their per-phase history,
delta and totals cover the fold; no case group is removed. Full original P2/U13
history, private admission and actual atomicity are still execution dependencies.

## 3. Interpreter and one signature provider

The V contract selects **KOS 249cc66c's bundled CPython 3.12.15**, as documented
in [Nemo's lock delta](</Volumes/WD-B-4TB/DevVault/helix/docs/handoffs/2026-10-14_nemo_kai_w4-lock-delta.md>).
Its logical bundle executable is `runtime/python/bin/python3`, with an isolated,
protected import closure. This selects an interpreter contract; it does not claim
an installed V launcher, UID/root custody, ABI or native import measurement.
Core/API's Python or the author's test interpreter cannot substitute for V.

Select the KOS closure's existing **cryptography 50.0.0**, with **cffi 2.1.1** and
**pycparser 3.0**, behind R01's single `verify_ed25519`. When V admits that exact
protected closure, drop provisional PyNaCl entirely. **No PyNaCl branch, dual
provider, fallback, parallel resolver or second decoder/verifier** is admitted.
If closure admission or the provider is missing, refuse; switching providers or
interpreter requires a separately accepted contract. Preserve R01's ED-only,
strict first/global-signature and purpose/current-original semantics; do not reuse
KOS's broader Ed/ED parser. Lock/adapter changes and crypto execution belong to
their independently dispatched owner work, not B0.

Actual executable/import/crypto/policy/journal/endpoint roots and ancestors, C/V
numeric UIDs/privileges, clocks, launchers, backend mechanics and Mac/Linux
protection are **UNBOUND**. Tate/Bob measure later under their own GOs. Neither
these package pins nor a Python object/hash/verified flag qualifies Gate B.

## 4. Frozen subjects, controls and exact missing originals

Initial oracle freeze commit: `86261e52b2dba814e808a190a857c76ffa6cf4a9`, from P1
`7cd39ee2383d77c285c6870fc8b0e361534e3585`. The initial test/fixture bytes were frozen
before the initial protocol. Accepted correction row
`2e17ed66-f5a2-45d8-ae2e-64125f8abc8f` first joins repaired P1, then re-freezes
only F1/F2/Q3 oracle corrections. Every unrelated P1/RC2/RC0/R01 byte remains preserved;
`subjects.json` has only additive top-level GateB entries. Normal merge `a822bc6a9fef090811701df0329dbfef0f6bb3f2` joins repaired P1
`0b7f98c40128465a8ffc1167ff413e708560825b`, including its explicit RC2 identity
merge. The oracle correction itself changes no product source; U13/R01 are not merged.

The literal fixture has **155 case bodies**: conditional owner-fact positives,
sole-field/sole-boundary negatives, and separately labelled multi-fault precedence
controls. Every negative names its full literal control, exact deciding outcome,
and independent expected owner-boundary reserve/consume and primitive-boundary
dispatch/verified-result counters. Earlier history, current delta and totals are
separate. Identical positive controls/original facts are reused without dropping
the 20 C0 groups or B-N1…9. Expectations are authored independently of any product
serializer. Original fact refs bind real test-only literal bytes and remain
bounded; these bytes are never installed authority or signature/image positives.

The input explicitly distinguishes complete **owner-fact** controls from absent
composed originals: `originals_required` is null, execution is
`NOT_RUN_MISSING_ORIGINAL_CORPUS`, and material is test-only. Request selectors
are explicitly not InstallRequests. No U3-A shape fixture, old two-fault signature
test, mocked provider or native_qualified flag supplies an accepted full positive.

The committed tests currently yield **180 checks: 25 artifact checks PASS,
155 labelled GATE_B_SURFACE_ABSENT BODY failures, 0 errors/skips**. The owner.py
assertion occurs in each body before any owner/provider import. This is surface
absence, not source behavioral causality or native proof. Artifact checks cover
literal fault isolation, ref closure/P1 shapes, exact C0 §6 mapping, counters,
clock/revision bounds, corpus IDs and additive index custody.

The correction assertions are frozen before subject repair at
`86e4cbc852dc0abe8d6afb14f1c49cf0217d101e` (the return binds the actual full SHA).
They fail against the earlier subjects, independently of owner.py absence.
F1 changes 28 existing consumed/recovery cases and adds three distinct matched
recovery controls that had been wrongly deduplicated. Consumed attempts retain
actual sequence/clock originals and consumed reservations. Completed original
read-only recovery carries its APPLIED result; durable reply loss changes only
the reply/crash observation and returns that same result with zero new counters.
Unknown results stay HOLD, independently known APPLIED appends once, definitive
NOT_APPLIED creates a fresh child with original parent/effect/idempotency lineage,
and accepted fix-forward retains its original prefix and fresh finite scope.
Historical counters remain; the current original attempt, parent histories and
current owner-revision observations are held by original byte refs. Current
authority observations can drift from a stored reservation without rewriting it.
The revision observation distinguishes a proposed reservation (r), a durable
reserve (r+1), and consumed authority (r+2). A fresh recovery child observes the
original parent's completed reserve/consume history before its fresh reserve.

F2 adds 30 controls: seven positives and 23 one-boundary negatives. Earlier
consent/host/private/policy/session deadlines are explicit winning controls;
a separately earlier same-epoch monotonic deadline and the lower not-before
uncertainty interval are also covered. UTC and monotonic equality/just-after
cases retain the already valid reserved intent and add no consume/dispatch.
The metadata assertions independently compute BOTH minima from the current
consent/lease facts and original policy/peer/clock observations, and check the
whole mint-time interval including not-before. No new production clock, field,
record class, config channel or parser is introduced by these test observations.

If the future owner surface exists, a missing accepted execution packet is a BODY
failure `NOT_RUN_MISSING_ORIGINAL_CORPUS`, never an automatic PASS/skip. The
test-only additive `GateB_accepted_execution` index entry may later bind original
execution receipts from a separately authorised runner. Tests compare observed
outcomes to these frozen expectations and require exact fixture/source binding,
actual common-owner invocation, distinct independent probes, intended BODY causal
failure with zero errors/skips and full exact restore. They do not execute a
provider or supply a product test factory/binder. Receipt comparison alone is not
the full B1 source exit: the reviewed actual original runner/trace and guard
omission/restores remain mandatory independent evidence. No receipt opens effects.

The exact missing R01 corpus is **all 38 corrected desired rows** at immutable
`a3ae1bc9abfba88b71c8f2779c1944941642672a:next/docs/release/r01u3/ORACLE_CASES.json`
(SHA256 `0e1f1b5948ebdf9a07ac552cdac2f51437fbd79664e5f627f7fa71a736a56355`):
SIG01–SIG15; SEM01–SEM06; ID01–ID03; BOUNDARY01–BOUNDARY02; SIG02-ID;
SIG07-FIRST; SIG07-GLOBAL; SIG08-REQUEST-ID; SIG08-ORIGINAL; SIG08-REENCODING;
SIG10-TRANSPORT; SIG10-SIGNED-SHAPE; SIG10-PHYSICAL; SIG11-AUTHENTIC-CONTEXT;
SIG14-DOMAIN; CURRENT-HASH. The fixture lists each literal ID, expected reason
and NOT_RUN status, rather than copying Ren's construction contract.

Required composed originals, also listed exactly in the fixture:

1. Complete original P1 InstallRequest bound to that accepted R01 corpus.
2. ED-only manifest, detached first/global signatures, independent full test-root
   material pin and current policy, under their separately named test-material GO.
3. Four actual Core/API target archives/OCI identities, external lock and full
   physical member originals, with separation/ABI/compatibility custody.
4. Selected/executed image, code/owner custody and current-policy refresh receipts.
5. Accepted bounded/streamed original reader, sole verifier callable traces and
   matched positive/sole-fault/control/restore execution receipts.
6. Independent owner peer/clock/consent/generation/fence/atomicity/backend/result
   reconciliation receipts and independently observed two-boundary counters.
7. Phase-specific finite private bootstrap receipt and runtime session/OID/map/
   DB-clock holder/fence/lease receipt, with actual post-lock expiry enforcement.

Prior Ren B evidence is not relabelled as execution of the tightened sole-fault
recipes. No new keys, signatures, crypto calls, hosts, images or effects occur in
B0. The full positive/negative oracle exit remains unavailable until these
originals and authorised execution are independently accepted.

## 5. R0 join and B1 entry gate

R0 is checked **last**, without waiting, against Ren-KOS row `094f42bd` and its
published original return/library surface. R0 supplies the sole in-V R01 library,
current-policy/held-original custody refresh, streamed large-artifact reader and
phase-specific actual image port/refusals. Its contract does not itself deliver
the accepted original execution corpus or installed owner.

JOIN STATUS: **DONE, contract only**, at the last-step readback. R0 row
`094f42bd-8100-46d7-b816-9a288bcb103c` was returned with handback
`7af844a9-8290-48ee-a2bb-e28cc365292c` (`returned:true`, `auto_accepted:false`).
The [R0 return](</Volumes/WD-B-4TB/DevVault/helix/docs/handoffs/2026-10-14_ren-kos_kai_gateb-r0.md>)
is SHA256 `d80855dcc7d450c68ac52cbd9c6a20fbff16f7aadb95d13fe82a88ef593bc4d4`;
its SURFACE.json is SHA256
`1d00f5ec39dd586bedf3d28c1419c4c84f4af9e55b3f711d5c99484985c0df01`.
Its source reference is `1e41f13e4251e2aed99072777a64e8bd881c0877`; this is an
immutable input, not a branch merged into B0 or an installed V qualification.

V's complete read-only admission contract is R0's proposed
`cortex_core.release.member_admission.verify_aggregate(aggregate_id)` over an exact
canonical `sha256:<64 lowercase hex>` string. `load_current_policy()` takes zero
arguments and returns DATA that cannot be passed back as authority; admission
independently reloads accepted current policy/root/permission/revocation/time/
generation/fence after audit and before effect. `stream_original_member(aid,name)`
uses the same sole authority, exact signed names up to 4096 UTF8 bytes and chunks
up to 65536 bytes. It authenticates the whole original before first delivery,
requires proved immutable/exclusive held custody, and rechecks hash/count/custody
and freshness at EOF. R0's maxima are 8MiB records, 4GiB images, 16GiB aggregate
and 120s reads, with tighter current-owner bounds permitted. Such audit finishes
before the separate 30-second reservation; streaming never renews effect scope.
These proposed functions are NOT_IMPLEMENTED. This document introduces no class,
provider, parser or public callback/configuration channel to make them appear real.

The private `request_original_member(aid,name)` adapter supplies the existing RC2
delivery protocol through that same custody body, preserving 512 identity atoms
and 262144 bytes. R0's fixed private
`verified_image_for(role,target)` allows only core-image/api-image and
linux/arm64/linux/amd64; current Cox scope supplies phase. It requires qualified
selected originals before launch and actual running/loaded closure before later
effects, with actual archive/platform-manifest/module/ABI/host/engine/backend
bindings. The supplier is MISSING_UNBOUND; client receipts/flags/paths cannot fill
it. R0 preserves the same 18 Refusal reasons and exact default unit
`R01 unit 3: signed Core/API aggregate and member admission`. Missing composition
still wins before candidates; exact R01 detail maps through C0 §6 at the owner.

Two explicit join deltas prevent stale proposals from becoming authority:

* R0's provisional PyNaCl text is superseded **for the V contract** by Kai's B0
  amendment in §3: cryptography 50.0.0 with the two pinned transitives, behind
  the same `verify_ed25519`, no dual provider/fallback. R0's existing U3-B byte
  algorithm/default are reused; provider/lock adapter execution and native closure
  measurements remain separately owned and unperformed here.
* The initial P1 base inherited RC2-IDENTITY-001. The correction's explicit normal
  P1 merge `a822bc6a9fef090811701df0329dbfef0f6bb3f2` carries the accepted RC2
  `edf604ebe0d15363a741d19e286001f421868e09` exact-type reader and both narrow P1
  trust repairs. P1's 38 source methods stay GREEN after join; the B0 absence shape
  stays RED. This source integration supplies no real R01/owner/native binding.

R0 requires the fixed protected module-relative `contracts/release` layout (or a
separately ratified package-resource refactor), actual interpreter/import/schema/
native dependency and ancestor protection, and no search/repair fallback. Platform
roots/UIDs/launchers and independently enforced backend separation stay UNBOUND.
The accepted original corpus and actual implementation/native exits remain held;
this completed contract join supplies no grant or acceptance.

Preserve stable RC2 `request_member(aggregate_id, member_name)` and
`read_member(member, *, aggregate_id, member_name)`, exact string identity checks,
262144-byte OriginalMember and default missing-binding-before-candidate behavior.
Large archives use R0's separately admitted streamed original path behind the sole
verifier; Finn does not enlarge OriginalMember or duplicate verification.
B1's planned owner.py is one internal common gate, not a public grant minting
surface. Missing image/owner binding refuses before candidate handling. Current
scope/consent, final clocks/CAS, private lease and original attempt recovery are
enforced there through accepted C0/P2 composition; they cannot be supplied by C.

## 6. Accepted Q3 qualification reference

The historical V-BOOTSTRAP/V-POSITIVE OPEN entries are superseded by Vera's
accepted **Q3-1…Q3-6** list, without re-ruling it. The immutable normative source is
[the accepted C0/R0/Q3 return](</Volumes/WD-B-4TB/DevVault/helix/docs/handoffs/2026-10-14_vera_kai_gateb-c0-r0-trust.md>):
20,401 bytes, SHA256 `8fb4badfba7d8fa4c7ecbfe54b247e9a23fdfa7061eee2b29f44fff15bf00c32`.
`Q3_acceptance_ref` in the re-frozen fixture binds exactly that original and the
six item identifiers. C0/R0 acceptance and Q3 stand; this B0 correction issues no
qualification/bootstrap/host/product GO and cannot authorise the first effect.

Both-host actual protection, admitted original providers/material, and separately
named exact qualification authorisations remain required by that list. Every
post-acceptance product effect still requires independent current finite consent,
full C0/R0 admission, durable intent, final checks and single consumption. The
existing physical/native and P2 gates remain held; fixture counters, source hashes,
package pins, client results or a passing artifact assertion supply no authority.
