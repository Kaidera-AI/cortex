"""Read-only complete schema prefix admission, distinct from enrollment health."""
from cortex_core.migrations import MigrationError, SCHEMA, prepare_migrations


def check_schema_ready(connection, directory=SCHEMA):
    prepared = prepare_migrations(directory)
    expected = [(identity, digest) for identity, digest, _ in prepared]
    try:
        rows = connection.execute('SELECT migration_id,sha256 FROM core.schema_migrations').fetchall()
        if len(rows) != len(expected) or dict(rows) != dict(expected):
            raise MigrationError('Schema prefix is incomplete or differs')
    except Exception:
        raise MigrationError('Schema readiness refused') from None
    return True
