# Runtime foundation provenance

Source: Kaidera-AI/cortex, MIT, reviewed commit
`7a1078213ac2497d6219e7778c7866001f97f30b`.

Reused: `runtime.contracts.decode`, `release.codec.decode_json` and
`runtime.signed_member.Refusal`. Existing public signed-runtime imports remain.
Change: defer jsonschema construction until a schema consumer requests it; the
same strict duplicate/Unicode/number/size/depth decoder is used by stdlib host
config projection and packaged Core. No second JSON parser or schema dialect.

Owned gap: explicit option-A closed config and a no-follow, purpose-bound private
file reader. It does not create a vault, encrypt custody, issue a secret, connect
to a database, admit a package, or start a server. Installer custody and signed
packaging remain separate gates. No external source was downloaded or forked.

The existing MIT LICENSE at repository root applies; preserve it in packaging.

Day-2 J06: use stock `cryptography==50.0.0` AESGCM, upstream
https://github.com/pyca/cryptography, tag `50.0.0`, peeled source commit
`dcb7050b807b00392fa9fe2eac7cb362fcf355cc`. Upstream package license:
`Apache-2.0 OR BSD-3-Clause`; retain the distribution license files in the
installed closure. No crypto implementation is copied or forked here. Use one
explicit raw-key descriptor and the existing strict JSON/canonical codec;
descriptor custody and filesystem state policy are owned integration code.

Source tests on macOS arm64 used the official CPython abi3 wheel
`cryptography-50.0.0-cp311-abi3-macosx_11_0_arm64.whl`, SHA256
`031e2d5dd4bb9caa3ca9c82e5a197fd8ae680232cee62603d1a813f3f07e3d03`.
This is source-test provenance, not a qualified native installer closure. The
product offline lock does not yet admit that macOS wheel; no product lock was
weakened. Nix owns both-target executable closure qualification. The adapter
never installs a dependency, generates a key or falls back to plaintext.

Review correction: classify canonical private IP literals before DNS labels;
reject alternate numeric IP and ambiguous double-leading-slash vault spellings.
The causal helper reuses the shared AssertionResult/report/classifier, executing
the same exact node healthy, mutant and restored; it admits no skipped, setup,
undeclared-failure or missing-code-witness result. No new outcome parser.

The fixed PostgreSQL connector uses stock `psycopg==3.2.9`, upstream
https://github.com/psycopg/psycopg, tag `3.2.9`, LGPL-3.0-or-later. Retain its
distribution license in the installed closure. No driver implementation is
copied or forked. Source tests use the pure Python wheel
`psycopg-3.2.9-py3-none-any.whl`, SHA256
`01a8dadccdaac2123c916208c96e06631641c0566b22005493f09663c7a8d3b6`,
with the existing native libpq. This source-test environment does not qualify
either target's installer closure or prove an actual TLS connection by itself.
