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

Review correction: classify canonical private IP literals before DNS labels;
reject alternate numeric IP and ambiguous double-leading-slash vault spellings.
The causal helper reuses the shared AssertionResult/report/classifier, executing
the same exact node healthy, mutant and restored; it admits no skipped, setup,
undeclared-failure or missing-code-witness result. No new outcome parser.
