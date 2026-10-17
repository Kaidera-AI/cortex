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


---

# Seed provenance

Repository MIT licence. The seed state machine is Cortex-owned glue. It imports
the existing Core migration manifest/readiness checker, canonical JSON codec,
private resource/custody owners and typed refusal. It consumes the actual fixed
`SeedResources.catalog()` accessor; the packaged
`cortex_ship.install.roles.validate_catalog` remains the sole role policy.

SQL effects use the repository's unchanged 21-migration schema:
`core/001-core.sql` anchors and `auth/007-runtime-grants.sql` binding map and
`auth.first_identity`, with `auth/006-enrollment-readiness.sql` as replaced by
the admitted runtime-grants migration for the request-login guard. These SQL
definitions remain in their canonical files; no supplier SQL body is copied or
changed here. The existing advisory migration lock is reused. The stdlib UUID,
secrets and SHA256 modules supply only identifiers, token generation and hashes.
Core owns AESGCM and its cryptography closure; seed adds no crypto dependency.

Test database and loopback recorder models are explicit source-only apparatus,
never a production policy, enrollment implementation or installed authority.
