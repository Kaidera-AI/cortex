# Schema foundation interface

`apply_forward_migrations(connection, directory=SCHEMA)` delegates to the existing
`cortex_core.migrations.apply_migrations` unchanged. Its existing connection
ownership, transaction lock, checksum and forward-prefix rules remain the sole
migration authority. It does not enroll or seed.

`check_schema_ready(connection, directory=SCHEMA) -> True` verifies manifest bytes
through `prepare_migrations`, then reads the exact complete ledger. Any missing,
unknown, duplicate or changed ID/digest refuses with MigrationError. This is
schema readiness only, not safe-login/enrollment/API/installed readiness. Caller
supplies the already-purpose-admitted connection; no connection factory, DDL or
credential resolution runs in this function.

Finn owns `seed.py` and `tests/test_seed.py`; Cox owns migrate/readiness and
`tests/test_readiness.py`. Seed must use the shared config interface and existing
SQL, with durable intent/custody before SQL. No migration bytes change here.
