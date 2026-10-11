# C05 PR44 named review repair

Accepted repair contract: user-adopted Kai H-D45510:05 routes Mike CANCEL001,
REPLAY002 and REPRO003 FIRST, with the frozen reviewer probe and all original
fixtures unchanged. Preserve write-only mutation semantics; no read+write
precondition. C04 public read, auth.py and existing migrations stay byte-exact.
C06 proof atc2562ff is preserved pre-C05; C05 preemption starts10:20 local
outside the C06 day. Bring the completed fix forward into C04a/C06 without
rebases; source/security/main/native/release gates stay separate.

RED: copy Mike's exact8 methods, preserve6 expected BODY failures and2 passing
controls on the published C05 source. Reproduce the actual missing helper import
in a no-resource controller prefix. Add separate own-receipt/principal/key,
write-only delete/replay and public-read-isolation probes before implementation.
No frozen assertion changes. Original173 tests and old30/26/8 faults remain.

BUILD: under the existing job lock, cancel pending state directly before loading
an old attempt; immutable history remains and same-key cancellation replays.
Add only a C05 forward migration afterauth0002: verifier-owned private scope,
exact own-principal/key replay decision, locked record CAS/head decision,
delete payload-reference lookup and header update. Functions derive fresh bound
write scope and tenant/project from C04's private context, never caller IDs;
receipt lookup returns only the exact caller principal+key. Private functions
expose no payload bytes and do not grant request public read. Keep existing
Python mutation/effect/rollback hooks and original fixtures; revalidate Core
authority on context exit. New private parallel checks receive C05-owned causal
fault compositions if they correctly mask an old mutation; never count errors.

Publish the pinned lifecycle helper locally beside the documented controller;
fix historical verifier relative resolution and retain exact tested controller
archive for old proofs. No-stack dependency guard runs first. Hash exact8 wheel
bytes before copy and inside the copied fixture before pip. Correct planning
metadata with per-file main/C04 provenance, retaining original metadata in Git.
Original root C05-source evidence remains historical; new repair evidence has
one home in lane-A/evidence/c05-rework with mirrored publication.

VERIFY: copied nativearm64 PG18.4/Python3.12, aggregate2CPU/1GiB, offline8 wheels,
UID70/10001, no ports/binds, pending-beforecreate/fresh nonce/owned immutableID
cleanup. Manual sequential entrypoints only, no held C03/graph producer reuse.
No host product imports/tests. RED before GREEN, full frozen173+Mike8+new probes,
old30+26+8 actual BODY matrix plus new guard-removal tests, zero errors, whole
source restoration and owned absence/password discard/unlock. Fresh independent
SDLC verifier checks source/custody/receipts. Push PR44 exacthead for Mike and
return; no main merge under H-D471. Then forward C05 into C06, then PR40 lock.

Door: two-way for source tooling; forward-only when migration applied. Blast:
coordination. Real application/client admission, issuer/adoption, released donor,
security reviews/main merge, native x86/install/signing/release remain held.
