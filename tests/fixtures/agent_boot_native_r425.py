"""Public synthetic boot SQL fixture; actual dependency migrations, owned socket only."""
from contextlib import asynccontextmanager
from pathlib import Path
import uuid
import pytest
from fixtures.state_import_native import state_cluster

ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES = ('0001_core.sql', '0002_w1_identity_memory.sql', '0006_context_interface.sql',
                '0010_keys_bootstrap.sql', '0011_keys_lead_sponsor.sql', '0012_keys_create_right.sql',
                '0013_keys_issuance_t21.sql', '0014_keys_365d_t34.sql', '0015_keys_project_create.sql')

@pytest.fixture(scope='session')
def boot_database(state_cluster):
    @asynccontextmanager
    async def database():
        async with state_cluster() as cluster:
            admin = await cluster['connect']('postgres', 'fixture_owner')
            name = 'kaidera-test-bootdb'
            opened = []
            try:
                await admin.execute(f'CREATE DATABASE "{name}" OWNER cortex_v2_migrator')
                db = await cluster['connect'](name, 'cortex_v2_migrator')
                opened.append(db)
                await db.execute('CREATE SCHEMA cortex_core; CREATE SCHEMA cortex_auth;')
                for filename in DEPENDENCIES:
                    await db.execute((ROOT / 'migrations' / filename).read_text())
                migration = ROOT / 'migrations/0024_context_agent_boot.sql'
                if migration.exists():
                    await db.execute(migration.read_text())
                installation, principal, scope = (uuid.uuid4() for _ in range(3))
                await db.execute("INSERT INTO cortex_auth.installations(installation_id,display_name) VALUES($1,'PUBLIC boot fixture')", installation)
                await db.execute("INSERT INTO cortex_auth.principals(principal_id,installation_id,principal_name,status) VALUES($1,$2,'PUBLIC fixture owner','active')", principal, installation)
                await db.execute("INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES($1,'project','PUBLIC fixture project')", scope)
                await db.execute('INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) VALUES($1,$2,true,true)', principal, scope)
                yield dict(db=db, principal=principal, scope=scope, installation=installation)
            finally:
                for conn in reversed(opened):
                    await conn.close()
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
                await admin.close()
    return database
