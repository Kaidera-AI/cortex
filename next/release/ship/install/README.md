# ship/install: roles (day 1)
`roles.py` renders three psql scripts from the ONE `roles` block of `contracts/runtime/runtime-operations-v1.json`:
`render_pre` (create the six logins with their exact flags and SCRAM passwords read inside psql from the mounted secret files, in one transaction),
`render_post` (after the unchanged migrations: the one set-only membership, then refuse any other membership), `render_close` (only with the seed's
durable acknowledgement: installer_enrollment, migrator and bootstrap become NOLOGIN, their backends end). Re-runnable. The two NOLOGIN groups
and the installer login name belong to the unchanged migrations 002 and 004. Tests: `python3 -m pytest release/ship/install/tests -q` (needs PostgreSQL 18 binaries; `CORTEX_PG_BIN`).
