# R01 U3-B: original-manifest signature data

Kai handoff f7715f38 authorizes C0 baseline/current alignment and cryptography50.0.0
behind the existing sole verify_ed25519 function. Production obtains private owner
context before candidate access; default _production_context=_unbound stays exact.
Actual protected owner/root/current-state/native closure delivery remains UNBOUND.

The existing _OwnerContext requires owner-delivered current_state with exactly
installed_baseline,policy_generation,fence. No public wire, record class, binder,
fallback or grant is added. Missing/None/extra current fields and nonpositive,
Boolean,floating or oversized current counters give required_binding_gap before
candidate/crypto. Baseline uses the inherited closed installed-or-explicit-empty
schema plus existing canonical codec to reject noninteger wire values.

Policy baseline must match its independently current attestation exactly through
the existing canonical codec. Observed generation matches current owner generation
and owner_binding.policy_generation; observed fence matches current owner fence.
Baseline generation/fence may be older, including admitted historical installed
fence0. Empty retains required nonzero receipt and positive counters. Attestation
or observed current drift gives aggregate_replay. Missing is never empty/default.
This is signature DATA against private context; C0 finite effect ownership, host
leases, atomic reservation and physical provenance are separate held mechanisms.

verify_ed25519 lazily imports cryptography50.0.0, calls
Ed25519PublicKey.from_public_bytes(public_key).verify(signature,message), and keeps
strict bytes types,32-byte key,64-byte signature,1MiB message cap and bounded
dependency/version/unsupported-algorithm errors. Import loads no native provider.

The first primitive run failed three unchanged small-order-forgery assertions.
SAME function now excludes noncanonical encoded-y values and all sign-bit variants
of upstream small-order encodings for public key and R before the native call.
These are bounded encoding checks, no Python curve/signature implementation.
Cryptography is the sole primitive. Reference:
https://github.com/jedisct1/libsodium/blob/1.0.20/src/libsodium/crypto_core/ed25519/ref10/ed25519_ref10.c

R01 decoder/two-signature semantics remain exact: LF/base64/UTF8, public Ed distinct
from ED signature, original root length/hash/ID, first over BLAKE2b-512(original),
second over first64bytes+exact trusted comment. No broader KOS parser is adopted.
Original policy validity/age/sequence/source/release/current-target guards remain.
Native/member/effect authority bits stay false; no production binding is installed.

Test factory remains outside installed source, rejects product caller before
key generation and holds only unrelated throwaway test keys/signatures in RAM.
No key of record is used/read and no signing material is persisted. Its test-only
SigningKey API compatibility wrapper uses the same cryptography primitive so
earlier frozen callers/deciding assertions remain unchanged.

Three inherited RFC8032 answers and29 constructed Wycheproof-style negatives
stay byte exact; subset is not the whole corpus/native proof. New31 alignment
controls were frozen RED before source:22 BODY failures,0 errors/skips. New32
encoding controls were frozen RED:32 BODY failures,0 errors/skips. Independent17
attestation-shape controls exposed two float wire-type BODY failures, then froze
before their codec-boundary fix. Seven authenticated-pipeline companions bind
valid older attestations, exact drift and missing-state-before-crypto behavior.
Final combined counts and source BODY mutants are in the named return receipts.

Captured reverse-purpose KOS source is byte exact. Its test-only API adapter
delegates to the same single actual primitive, proves authentic/tamper behavior,
then Cortex-schema refusal. This does not qualify KOS package/native installation.

Owned author environments use10 publisher length/SHA-checked wheels for eight
packages:cryptography50.0.0,cffi2.1.1,pycparser3.0 and five inherited schema packages;
5,334,104 bytes. Offline exact-hash installs use tests/release/r01u3_support/
TEST_REQUIREMENTS.hashes.txt. Author CPython3.14.0/3.13.7 are not V/native/Linux proof.

Finn B0 selects KOS249cc66c bundled CPython3.12.15 and this same closure as contract.
Nemo replaces the provisional eighth installer-owner crypto root with
cryptography==50.0.0, retaining cffi==2.1.1,pycparser==3.0 and no optional extras.
Existing KOS production lock already pins these at original lines241,137,588.
V requires admitted original cp312 CFFI and appropriate cryptography abi3 wheels;
author cp313/cp314 CFFI is not substituted. Superseded crypto payload/native-library
inventory leaves the NEW owner closure; history remains. Core/API seven direct
roots stay exact. Nemo owns lock/SBOM/native closure acceptance; no production
lock changes here. B0's physical custody/admission remains UNBOUND.

One Vera review covers A corrections/amendments and final full B trust. Corrected
38 desired original-corpus rows, whole aggregate/member stream, actual root/owner/
image/state providers, capacity and both-host Gate B remain held. No host/engine/
image/DB/product effects are performed by this unit.

LEAN: reused private context, canonical codec, closed baseline schema, RC2 Refusal,
sole provider function; added no port/parser/record class/fallback/second primitive;
deleted redundant attestation type check enforced by inherited closed schema.

References:
- https://cryptography.io/en/50.0.0/hazmat/primitives/asymmetric/ed25519/
- https://datatracker.ietf.org/doc/html/rfc8032#section-7.1
- https://jedisct1.github.io/minisign/
- https://github.com/C2SP/wycheproof
