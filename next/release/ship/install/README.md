# ship/install: roles (day 1)
`roles.py` renders three psql scripts from the ONE `roles` block of `contracts/runtime/runtime-operations-v1.json`:
`render_pre` (create the six logins with their exact flags and SCRAM passwords read inside psql from the mounted secret files, in one transaction),
`render_post` (after the unchanged migrations: the one set-only membership, then refuse any other membership), `render_close` (only with the seed's
durable acknowledgement: installer_enrollment, migrator and bootstrap become NOLOGIN, their backends end). Re-runnable. The two NOLOGIN groups
and the installer login name belong to the unchanged migrations 002 and 004. 
## Day 2 (HALE-A1-NIX-JOIN-001, HALE-A1-NIX-CI-001)
**Config join.** `bind()` takes the seed/serve/migrate views of the ONE Core snapshot (the real carrier, no second parser). It refuses views whose shared
fields differ (`role_config_conflict:snapshot`, vault root first as `role_config_conflict:vault_root`) and a combined set whose five logins or resolved secret
paths are not unique (`role_config_collision:login` / `:secret`): each view only checks its own purposes, so a mix of two valid snapshots used to pass.
Refusal happens in `bind()`, before any SQL is rendered.

**Two test homes.** `tests/test_roles.py` is PURE (19 methods: policy, stand-in join, real-carrier join, CI-home checks): no PostgreSQL, no native process, no
import-time discovery; it stays in next-offline. `tests/test_roles_pg.py` holds the nine real-PostgreSQL methods (RolesInDatabase 8 + SecretGuard 1), bodies
byte-identical to Day 1. Shared pure fixtures: `tests/_fixtures.py`.

**PG-gate fixture (the qualified home of `test_roles_pg.py`, to be admitted by Mike).** Needs `initdb`, `pg_ctl`, `psql` of PostgreSQL 18 (`CORTEX_PG_BIN`, else PATH,
/usr/lib/postgresql/*/bin, /opt/homebrew/bin); discovery runs in the fixture, a missing toolset FAILS (never skips). Each cluster lives in a private temp dir, listens
only on a unix socket in it (`listen_addresses=''`), runs as the invoking user, and is stopped and deleted in `tearDown`/`tearDownClass`. Run:
`cd release/ship/install/tests && CORTEX_PG_BIN=<bin dir> python3 -m unittest -v test_roles_pg` (take the heavy-run lock and keep >= 35% free memory first).
The Core image carries the same three binaries, so the same file can run inside the paired Core image; that run is an installed-fixture receipt, a separate gate.
Causal controls for the join: `python3 tests/mutate_join.py [out.json]` (five mutants of `roles.py`, unchanged test bytes).

## Day 2 continued: signature before effects, installer spine (source-level only)
`sign/write_release.py` writes `release.json` + `SHA256SUMS` (payload = every file of a clean folder; index files and signatures are never payload, exit 3) from
one identity file and one images file; the CTO signs with one `minisign -S -s KEY -m release.json SHA256SUMS`. `sign/verify.sh` (POSIX sh, read-only) checks the
minisign signature of `SHA256SUMS` and then every file THIS platform needs, `release.json` included, before any effect; typed refusals (stderr JSON, exit 2):
`release_signature_missing`, `release_signature_invalid`, `artifact_missing:<name>`, `artifact_digest_mismatch:<name>`, `platform_not_in_release`, `missing_tool:*`.
`install.sh` (one POSIX file, <= 400 lines) = check_tools (podman >= 6.0.2 by effect, python3) -> verify -> load_images (this platform's two archives, tagged
`localhost/cortex-v2-<role>:<release_id>`, recorded in `<prefix>/install-record.json` the moment they exist; a rerun changes nothing). The phases that need Bob's render,
Cox's custody and Finn's seed (make_secret, write_config, provision_roles, bootstrap, serve) refuse `install_not_wired:<phase>` BEFORE any effect, so no half
install exists; `--only load` is the unit entry. The pinned release key line is the 7BBB4B7E0CE8852E key from the design pack: its byte equality with the KOS
public-key file is not yet checked here (open). `--trust-key-file` is explicit and printed. Tests: `tests/test_sign.py` (needs `minisign` and sh; a missing tool FAILS),
causal controls `tests/mutate_sign.py` (12 mutants). Source/file proof only: no installed, paired or signed-candidate credit.

## J06 sole catalog validator and the closure package (Day 2 continuation 2)
`roles.validate_catalog(config, observation, *, phase='seed') -> dict | RoleRefusal` is the ONE policy check of Cox's raw read-only catalog observation
(runtime/catalog.py, J06-catalog-observation-v1): same five-purpose policy and flags as `render_*`; the actual bootstrap session (name AND oid) and database; five
roles; the two fixed NOLOGIN groups with no privileges or memberships; EVERY edge incident on them (foreign incoming included) must equal the one set-only
api_request membership; no service role (api_request, probe, installer_enrollment) owns the database or a protected schema/relation; bindings are absent (fresh)
or exactly the three revision-1 rows of ONE operation (replay). Typed reasons: catalog_phase/config/shape/database/session/roles/flags:<purpose>/groups/
membership/ownership/bindings. The result is a deep copy (detached) carrying the explicit revision INTENT (bootstrap/migrator 0 = not protected-bound;
api_request/probe/installer_enrollment 1 = intended initial binding); an observed revision is never copied as authority. Source fixtures are not production
authority: only Cox's own observer feeds it. `package_closure.py --out DIR` stages the SAME roles.py and the SAME runtime-operations-v1.json as
`cortex_ship/install/` plus `cortex_ship/receipt.json` (digests); standard library only, so the closure is identical for linux/amd64 and linux/arm64; the
packaged module reads only its packaged contract (no tree fallback). Wiring the staged tree into the API image (recipes/api) is the next source step; the image
rebuild and per-arch smoke stay a separate, heavy-run gate. Controls: `tests/mutate_catalog.py` (26 mutants).

## Installer caller (provision.py; J06)
`provision.run_phase(config, custody=, deadline=, phase=, seed_ack=False)` is the only caller of `roles.provision_steps`. It is not a second executor: it opens Core's
`open_bootstrap_resources(config, custody=, deadline=)` (config.mode must be `seed`), runs the phase's steps on `.transaction('bootstrap')` (ONE transaction, rolled back on any error), and
resolves each step's secret with `custody.resolve(purpose)` at the moment the step runs, passing it as the single bound parameter (never statement text, file, env, argv or shell).
Refusals before any resource opens: `provision_phase`, `provision_config`, `ledger_not_accepted`, `seed_not_acknowledged`; `provision_secret` (empty/NUL/non-UTF-8) before binding.
`provision.provision_sequence(config, custody=, deadline=, migrate=, seed=)`: pre -> `migrate()` (Core's unchanged command) -> post -> [ledger grant: PENDING, `LEDGER_ACCEPTED=False` until Kai's
consult fb3b7996 is decided] -> `seed()` (Finn's durable seed; must return exactly `True`) -> close. Not proved: the real Core opener/TLS/encrypted custody run, and the `bootstrap` login's own creation.
