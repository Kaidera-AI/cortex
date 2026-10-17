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
