# Pinned reuse — compat spec only

Cortex source: Kaidera-AI/Cortex at
7a1078213ac2497d6219e7778c7866001f97f30b, root LICENSE: MIT,
Copyright (c) 2026 Kaidera contributors. Preserve copyright and permission notice
in copies/substantial portions. No third-party code is vendored in this slice.

Reviewed existing source (paths below are relative to this pinned repository):
- packages/api/main.py: LogRequest optional/null defaults and six carried fields.
- packages/cli/cortex-log, cortex-search, cortex-boot and _cortex_api.sh: daily wire;
  preserve source, exclude automatic rerank, summary prefix and v1 DB fallback.
- next/src/cortex_core/api/c11b.py: source-scoped UUIDv5, canonical record read/write.
- next/src/cortex_core/records.py: ONE Records transaction/idempotency and existing
  non-null coordination.c06_record_event guard. No C07 Jobs path borrowed.
- next/src/cortex_core/embeddings/text.py: section + LF + content, 16 KiB projection.
- next/tests/benchmarks/mutation_receipts.py: existing strict unittest receipts and
  BODY/fixture distinction, reused unchanged by the assertion/mutation commands.

Changes: add a normative accepted A field map plus document assertions; align
INDEX v2 J05/J09/J10. No runtime source is modified or copied. The nullable log
adapter, server log marker, mode gate and bound OpenAPI remain Finn/Cox work.
Stdlib unittest/json/Path are Python/PSF dependencies already present, no install.
Accepted dispatch and INDEX/DAY-1 hashes live in the task's PRE_EDIT.json evidence.

Day-2 published-owner pins (same repository MIT, no copied runtime code):
- Cox 8e1babb2118a7b2ecb11115babb4dd39c90750d6:
  runtime/bootstrap_config.py, runtime/credentials.py and runtime/contract.md.
  Reuse their actual closed field names, explicit mode purposes, private reader
  requirements and verify-full. This is foundation source, not encrypted custody
  or an operator/listener implementation.
- Bob 887c4d6d3d9e1523d13297b5bab949edb7c48b7c:
  next/release/ship/{run,cli}/contract.md. Freeze the bootstrap order and the
  PROPOSED operator argv/client keys with their open reconciliation status.
- Finn bc4514a66164c9a5cbee449d7ff5bd2dc839d74c:
  schema/tests/test_seed.py and packages/cli/_cortex_api.sh; seed.py is absent.
  Do not credit negatives or unchanged legacy helper as a seed implementation.

Full source paths/Git blob IDs/SHA256s are in contract.md day2.source_receipts;
exact J06 draft hashes/status are in day2.custody.design_receipts. Accepted
DAY-2/data-seed/data-transport obligations supply the replay/design-first gates.
J06's proposed cryptography dependency is owned by Cox/Nix; this documentary
slice neither installs nor vendors it. No external research/code donor is used.

Changes: add published-owner documentary conformance and honest unresolved
custody/client/CI joins. Retain all Day-1 normative fields and assertion methods.
Only Python stdlib unittest/json/Path are executed. The strict receipt helper is
unchanged; source/CI/native/installed/hosted evidence remain separate classes.

Accepted reconciliation provenance (documentary input, no code donor):
- J06-interface-v1.md: SHA256
  25f208b916fa71f7c42be52399c0a92b1be1b2241d412427ab395712893d6d49.
- Cox's j06-design-v1/RECONCILIATION.json: SHA256
  59749e80015e1cec977fd98a0f7f78f6fd75791cfac89151f578d0b4def793df;
  ACCEPT_FOR_BOUNDED_SOURCE_WITH_INTERFACE_RESOLUTIONS, not source/installed
  acceptance. Frozen input bytes are in the same-row rework evidence receipt.
- Finn's accepted J06-seed-replay.md v2: SHA256
  8b5a5ad5fb56a66d696427f54ec1d45fd45b2b3657381d3e118dc670049bd3ab;
  exact intent, 64 lower-case ASCII hex credential and eight-field receipt.

Changes: append a distinct controlling accepted section and 13 tests-first
assertions, retaining every earlier map value, source pin/proposal and the 23
existing test bodies. cryptography==50.0.0 is the accepted Cox/Nix runtime
closure obligation; this spec installs, imports and vendors none of it. No
external service, parser, transport, store or runtime authority is introduced.
