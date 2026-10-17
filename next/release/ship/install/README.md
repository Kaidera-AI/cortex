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
