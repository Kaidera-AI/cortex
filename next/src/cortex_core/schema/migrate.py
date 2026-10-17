"""Forward to the existing migration engine; no implicit seed or startup DDL."""
from cortex_core import migrations

SCHEMA = migrations.SCHEMA
MigrationError = migrations.MigrationError


def apply_forward_migrations(connection, directory=SCHEMA):
    return migrations.apply_migrations(connection, directory)
