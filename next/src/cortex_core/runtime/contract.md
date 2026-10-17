# Option A foundation v1 (H-D614, 17 October 2026)

The accepted Day-1 scope is config/resolver and schema adapter/readiness only.
Resource ownership, operator dispatch and the listening API composition follow
on day 2. Existing signed-runtime declarations remain available unchanged.

`decode_config(raw: bytes) -> BootstrapConfig` uses the existing strict wire
decoder. `read_config_file(path, *, mode) -> BootstrapConfig` reads at most 65536
bytes from a private regular, owned, non-symlink file. `validate_bootstrap(value,
*, mode) -> BootstrapConfig` is effect-free. Supported modes are `serve`,
`migrate`, `seed`, `status`, `client`. No mode is inferred.

The one vault `config/cortex.json` has exactly these required fields:

```
version: 1
installation_id, tenant_id, project_id: canonical nonzero UUID strings
project_names: {project_name: project_UUID}; includes project_id exactly once
ai_mode: "off"
vault_root: absolute existing project-vault root
bind: {host: explicit IP, port: 1..65535}
publish: {host: loopback IP, port: 1..65535}
client: {credential_ref: "secrets/cortex/<name>"}
database: {host: explicit private DNS/IP, port: 1..65535, name: SQL identifier,
           tls: {mode: "verify-full", root_cert_ref: "secrets/cortex/<name>"}}
roles: {bootstrap, migrator, api_request, probe, installer_enrollment:
        {login: SQL identifier, secret_ref: "secrets/cortex/<name>"}}
limits: {request_seconds: integer 1..120, shutdown_seconds: integer 1..120}
```

Keys are closed at every level; booleans are not integers, IDs/logins/refs are
distinct, project names are bounded identifiers, strings are bounded. No vendor,
model, provider credential, URL override, supervisor/observer or secret value.
The existing verify-full transport is preserved; socket/SCRAM requires a named
transport ruling, not a default guessed by an author. This foundation does not
create certificates or claim custody encryption is solved.

`resolve_credential(config, purpose) -> bytes`: purpose-bound references, maximum
4096 bytes, owned private regular files, no symlink at any path component, no
hard links. Serve permits only api_request/probe, migrate only migrator, seed
bootstrap/installer_enrollment/api_request, client only client, status no secret.
Config reading has no secret I/O. Secrets are never represented in config repr,
argv, logs or refusal text. Missing/unmounted/unsafe references refuse. A serving
pod receives only its two private projections; canonical vault custody remains
outside it. Project custody/encryption proof is a separate installer admission.

`schema.migrate.apply_forward_migrations(connection, directory=SCHEMA)` forwards
to the ONE existing locked runner. `schema.readiness.check_schema_ready` is
read-only: verify all manifest bytes using the same preparation function, then
require the exact full applied ID/digest set. It is schema readiness only, not
enrollment/login/installed readiness. Finn alone owns schema/seed.py.

Plan amendment: defer jsonschema loading in the existing wire codec/contracts
without changing validation, so the same decoder can run on stdlib-only host
Python. Extract the existing migration preparation unchanged for shared reuse;
do not create another parser or migration engine. These small prerequisite
edits are covered by import/admission regression tests and causal assertions.

## Day-2 J06 encrypted custody (source foundation)

The accepted J06 interface v1 SHA256 is
`25f208b916fa71f7c42be52399c0a92b1be1b2241d412427ab395712893d6d49`.
The actual config carrier retains nonsecret `client_ref` in all modes and adds
immutable `source_sha256` over the full canonical decoded snapshot plus the
read-only five-purpose `role_logins` map. Its credential authority remains
limited to the selected mode. No second config or JSON decoder is introduced.

`custody.open_custody(config, *, unlock_fd, key_id)` consumes exactly 32 raw key
bytes plus EOF through an explicitly admitted private descriptor. It uses
stock cryptography 50.0.0 AESGCM and the existing private vault layout. It does
not generate, store, recover or default a key. Credential purposes are checked
before filesystem access. Unknown/malformed/unsafe/wrong-key records refuse;
no plaintext-reader fallback exists in this adapter.

`vault.seed_record()` holds a nonblocking stable private lock. `load()` returns
private immutable `SeedMaterial` or None for an absent leaf only; `create()` is
absent-only and durably publishes encrypted PREPARED; `commit(receipt=...)`
retains intent/credential, checks the loaded file snapshot, and durably writes
COMMITTED. Exact committed replay does not rewrite the record. A failed write
acknowledgement invalidates the handle and requires later reload/reconcile.
The only first-console record is the existing client reference. `resolve`
admits only a committed client record; role imports use the same adapter and
require an explicit purpose and secret. Lock files are never unlinked.

This source stage is not a SQL seed, resource connector, operator, listener or
installed proof. Those joins require the actual later modules and independent
exact-head review. The old `credentials` reader remains an unbound foundation
interface; Option A must not use it as an encrypted credential fallback. Pure
config admission remains stdlib; encrypted client projection needs the
qualified installed Core cryptography closure.
