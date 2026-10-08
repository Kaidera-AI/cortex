"""OCR-001: exercise canonical authority transitions on disposable databases."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import uuid

import pytest

from fixtures.agent_boot_native_r425 import DEPENDENCIES
from fixtures.state_import_native import state_cluster
from test_agent_boot_metadata_sql_r425 import native
from test_legacy_boot_import_native_r428 import (
    FIX, authority, canonical_rows, fixture_data, inputs,
)
from cortex_v2.state_import import ImportRefused

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def canonical_fixture(factory):
    rows, bodies, cases = fixture_data()
    async with factory() as pair:
        admin = await pair['connect']('postgres', 'fixture_owner')
        name = 'kaidera-test-ocr001'
        db = None
        created = False
        try:
            await admin.execute(f'CREATE DATABASE "{name}" OWNER cortex_v2_migrator')
            created = True
            db = await pair['connect'](name, 'cortex_v2_migrator')
            await db.execute('CREATE SCHEMA cortex_core; CREATE SCHEMA cortex_auth;')
            for filename in (*DEPENDENCIES, '0024_context_agent_boot.sql'):
                await db.execute((ROOT / 'migrations' / filename).read_text())
            installation = uuid.uuid4()
            owner = await db.fetchrow(
                'SELECT * FROM cortex_auth.bootstrap_installation($1,$2,$3,$4,$5)',
                installation, 'PUBLIC OCR-001', 'PUBLIC canonical owner',
                b'public-owner-hash'.ljust(32, b'0'),
                b'public-recovery-hash'.ljust(32, b'1'),
            )
            principal = owner['owner_principal_id']
            scopes = {'scope': uuid.uuid4(), 'global_scope': uuid.uuid4()}
            for key, kind in [('scope', 'project'), ('global_scope', 'shared')]:
                await db.execute(
                    'INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES($1,$2,$3)',
                    scopes[key], kind, 'PUBLIC canonical import ' + kind,
                )
                await db.execute(
                    'SELECT cortex_auth.bind_scope($1,$1,$2,true,true,false)',
                    principal, scopes[key],
                )
            await pair['writer'].execute((FIX / 'source-schema.sql').read_text())
            for row in rows:
                await pair['writer'].execute(
                    f"INSERT INTO {row['table']} SELECT * FROM jsonb_populate_record(NULL::{row['table']},$1::jsonb)",
                    json.dumps(row['row']),
                )
            originals = {}
            for table in ('public.agent_profiles', 'public.rules', 'public.agent_skills'):
                for row in await pair['source'].fetch(
                    f'SELECT id,to_jsonb(t)::text AS original FROM {table} t ORDER BY id'
                ):
                    originals[table + ':' + str(row['id'])] = row['original'].encode()
            yield dict(db=db, pair=pair, principal=principal, installation=installation,
                       originals=originals, bodies=bodies, cases=cases, **scopes)
        finally:
            if db is not None:
                await db.close()
            if created:
                await admin.execute(f'DROP DATABASE "{name}"')
            await admin.close()


async def effects(f):
    receipts = await f['db'].fetch(
        'SELECT to_jsonb(t)::text AS original FROM cortex_core.command_receipts t '
        'ORDER BY to_jsonb(t)::text'
    )
    return await canonical_rows(f), [r['original'] for r in receipts], await authority(f)


async def revoke(f, scope):
    await f['db'].execute('SELECT cortex_auth.revoke_scope_grant($1,$1,$2)',
                          f['principal'], scope)
    grant = await f['db'].fetchrow(
        'SELECT can_write,revoked_at FROM cortex_auth.scope_grants '
        'WHERE principal_id=$1 AND scope_id=$2', f['principal'], scope,
    )
    assert grant['can_write'] is True and grant['revoked_at'] is not None


@native
@pytest.mark.parametrize('scope_key', ['scope', 'global_scope'])
@pytest.mark.parametrize('stage', ['before', 'replay'])
async def test_canonical_revocation_refuses_import_and_exact_replay_without_effects(
    scope_key, stage, state_cluster,
):
    async with canonical_fixture(state_cluster) as f:
        module, snapshot, policy = await inputs(f)
        if stage == 'replay':
            await module.import_snapshot(f['db'], snapshot, policy, idempotency_key='canonical-revoke')
        await revoke(f, f[scope_key])
        before = await effects(f)
        with pytest.raises(ImportRefused, match='legacy_boot_scope_unavailable'):
            await module.import_snapshot(f['db'], snapshot, policy, idempotency_key='canonical-revoke')
        assert await effects(f) == before


@native
@pytest.mark.parametrize('scope_key', ['scope', 'global_scope'])
async def test_canonical_fresh_grant_accepts_import_and_exact_replay(scope_key, state_cluster):
    async with canonical_fixture(state_cluster) as f:
        module, snapshot, policy = await inputs(f)
        await revoke(f, f[scope_key])
        await f['db'].execute('SELECT cortex_auth.bind_scope($1,$1,$2,true,true,false)',
                              f['principal'], f[scope_key])
        assert await f['db'].fetchval(
            'SELECT revoked_at IS NULL FROM cortex_auth.scope_grants '
            'WHERE principal_id=$1 AND scope_id=$2', f['principal'], f[scope_key],
        ) is True
        before_authority = await authority(f)
        result = await module.import_snapshot(f['db'], snapshot, policy, idempotency_key='fresh-grant')
        assert result['counts']['migrated'] > 0
        assert await module.read_inverse(f['db'], result) == f['originals']
        before = await effects(f)
        assert await module.import_snapshot(f['db'], snapshot, policy, idempotency_key='fresh-grant') == result
        assert await effects(f) == before
        assert await authority(f) == before_authority
