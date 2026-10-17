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
