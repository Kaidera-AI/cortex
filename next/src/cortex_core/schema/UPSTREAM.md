# Schema foundation provenance

Kaidera-AI/cortex, MIT, reviewed base
`7a1078213ac2497d6219e7778c7866001f97f30b`.

Reused: `cortex_core.migrations.apply_migrations`, including its advisory lock,
driver transaction ownership, checksum and prefix/replay rules. Its preparation
loop is extracted as `prepare_migrations` for read-only readiness, rather than
duplicating a manifest parser or migration engine. The existing 21 SQL members
and manifest bytes are preserved. New readiness is a closed library join only.

No third-party code downloaded or copied. Preserve repository MIT LICENSE.
Finn's separate sole-seed implementation supplies its own provenance amendment
when integrated; this document does not claim the seed is implemented.
