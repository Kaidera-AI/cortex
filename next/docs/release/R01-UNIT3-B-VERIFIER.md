# R01 U3-B: original-manifest signature verification

This unit implements signature authentication data for the accepted U3 plan.
The installer/independent owner is the only intended placement. Production
`verify_manifest(raw, signature)` obtains its private owner context first and
currently returns the named `required_binding_gap`; candidate bytes cannot
provide, replace or repair the root. There is no public binder, signing function,
test-root switch, transport or effect owner.

## One provider and one byte path

`ed25519.verify_ed25519(public_key, message, signature)` is the sole Ed25519
boundary. It lazily loads provisionally selected PyNaCl 1.6.2 and uses its native
strict verification. It returns an exact Boolean for malformed/authentic input;
missing or unqualified dependencies raise bounded errors, sanitized by the trust
entry. It uses ordinary Ed25519 on the supplied message, never Ed25519ph or a
Python curve implementation. The provider can be replaced behind this function
if Nemo or Cox rejects the production lock delta.

`trust` reuses R01's canonical codec, byte-aware `WireValidator`, deep freeze/thaw,
and RC2's exact `Refusal`. The inherited aggregate/policy schemas are fixed local
subjects loaded at import, before candidate verification. No candidate path or
URL participates in schema loading. Importing the provider itself loads no
native library.

After independent owner-policy original bytes/hash/canonical encoding and the
installer/owner placement are checked, the private algorithm proceeds:

1. Bound exact original manifest bytes to 1 MiB and signature bytes to 4096;
   owner limits can lower them. Refuse unsigned input.
2. Compare independent public-root byte length, SHA-256 and key ID. Decode one
   bounded two-line LF Minisign public key; its packet algorithm is `Ed`.
3. Decode exactly four LF signature lines plus final LF, strict canonical
   base64, exact key ID, `ED` only, UTF-8, no CR/NUL and a 512-byte trusted comment.
4. Verify first signature over BLAKE2b-512 of the original manifest bytes. Verify
   second signature over the first 64 signature bytes followed by the exact UTF-8
   trusted comment bytes, excluding the label. Parse no manifest before these.
5. Decode with the existing sole canonical codec, enforce depth at most 32 (or
   a smaller owner bound), and validate the frozen aggregate shape. Compare the
   trusted comment to the frozen literal purpose/release/sequence/original-hash/
   source grammar. The untrusted comment grants no authority.
6. Compare independent purpose/key-use permission, observed time/generation/
   fence, validity/maximum age, sequence floor, release/source identity and
   current original-manifest digest. Return immutable signature DATA.

`ManifestSignature.native_qualified`, `.effect_authority` and `.members_admitted`
are always false. This unit does not read members, inspect OCI, qualify an image,
implement OriginalMember, activate RC2, assemble or sign a product release.
Public production policy additionally validates the frozen policy schema and
fixed production key ID. The actual independent context/root store remains
unbound; no release key or real root was accessed.

## Test-only evidence

The factory lives under `tests/release/r01u3_support`, outside installed source.
It rejects a product-source import/caller before key generation, including an
already cached import. Unrelated throwaway keys are generated only in test
bodies and retained in memory. No key file is made. The generic private algorithm
is exercised by a test context; public production entry has no such mode and
refuses that root. Private Python records/functions are a trusted-code assumption,
not an OS/process provenance proof; Vera's later seal remains open.

The frozen oracle contains three public RFC 8032 known answers plus a constructed
Wycheproof-style bounded adversarial subset (not the complete corpus), actual
two-signature controls, strict bounds/encoding, independent root/policy negatives,
replay, purpose checks both ways, default/import controls and sanitized faults.
The reverse purpose control runs unchanged captured KOS verifier source with a
TEST-only API adapter to the same actual provider. It checks authentic and tampered
signatures before KOS rejects the Cortex schema. It does not qualify the KOS
native cryptography dependency or a real KOS package.

Final controls: 128 new tests PASS; inherited U3-A 96 and RC2 106 PASS. Sequential
source mutations: 48 guard omissions, one missing-dependency refusal boundary,
three false authority flags and one immutability drop all die on original body
assertions, with zero errors/skips. Every source/test byte is restored. The frozen
inherited 17-case future oracle remains 9 PASS / 8 FAIL: six prior RC2 reader body
failures and two missing member-backend/assembly surfaces, outside U3-B.

The owned MAIN test venv uses macOS arm64 CPython 3.14.0 and eight publisher-hash/
length-checked wheels installed offline with `--no-index --no-deps --require-hashes`.
It is not production CPython 3.14.8 or Linux ABI qualification. PyNaCl 1.6.2, cffi
2.1.1 and pycparser 3.0 remain test-environment dependencies until admitted into
Nemo's installer/owner lock. No production requirements, lock or Core/API image
were changed. Exact install/native-module/source/mutation/log receipts are held
in the corresponding R01-unit3b-build-20261014 evidence directory.

LEAN: reused R01 codec/WireValidator/freeze and RC2 Refusal; did NOT add a second
parser/provider/verifier, production dependency entry, root config path, binder,
transport or member admission; could delete or merge none within U3-B.

Primary format/provider/vector references: [Minisign](https://jedisct1.github.io/minisign/),
[PyNaCl signing API](https://pynacl.readthedocs.io/en/latest/signing/),
[libsodium verification](https://doc.libsodium.org/public-key_cryptography/public-key_signatures),
[RFC 8032 section 7.1](https://datatracker.ietf.org/doc/html/rfc8032#section-7.1),
[Wycheproof](https://github.com/C2SP/wycheproof).
