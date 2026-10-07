"""Native RED-first storage metadata contract, isolated public PostgreSQL data."""
import asyncio
import functools
import copy
import json
import uuid
import asyncpg
import pytest
from fixtures.agent_boot_native_r425 import boot_database
from fixtures.state_import_native import state_cluster

KINDS = ('persona', 'rule', 'skill')

def native(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run

def manifest(kind):
    value = dict(schema_version=f'cortex.boot-{kind}-manifest.v1', name=None,
                 description=None, scope='project', permission=None, body_ref=None, version=None)
    if kind == 'persona':
        value.update(functional_roles=['PUBLIC-role'], identity_text='PUBLIC identity')
    if kind == 'rule':
        value.update(title=None, source_file=None)
    return value

async def insert(fixture, kind, metadata, audience=None):
    columns = 'scope_id,' + kind + '_id,revision,body,created_by_principal,boot_manifest'
    args = [fixture['scope'], uuid.uuid4(), 1, 'PUBLIC canonical body', fixture['principal'],
            None if metadata is None else json.dumps(metadata)]
    if kind == 'persona':
        columns += ',template_version,payload'; args += ['cortex.persona.v2', '{}']
    elif kind == 'rule':
        columns += ',slug,obligation,state'; args += ['public-rule', 'mandatory', 'active']
    else:
        columns += ',slug,when_to_use'; args += ['public-skill', 'PUBLIC trigger']
    if audience is not None:
        columns += ',audience'; args.append(audience)
    values = ','.join('$' + str(i) for i in range(1, len(args)+1))
    return await fixture['db'].fetchrow(f'INSERT INTO cortex_context.{kind}_revisions({columns}) VALUES({values}) RETURNING *', *args)

@native
@pytest.mark.parametrize('kind', KINDS)
async def test_null_metadata_preserves_legacy_rows(boot_database, kind):
    async with boot_database() as fixture:
        row = await insert(fixture, kind, None)
        assert row['boot_manifest'] is None
        assert row['body'] == 'PUBLIC canonical body'
        if kind != 'skill':
            assert row['audience'] == 'scope'

@native
@pytest.mark.parametrize('kind', KINDS)
async def test_exact_nullable_manifest_stored_without_rewriting(boot_database, kind):
    async with boot_database() as fixture:
        value = manifest(kind)
        row = await insert(fixture, kind, value)
        assert json.loads(row['boot_manifest']) == value

@native
@pytest.mark.parametrize('kind', ('persona', 'rule'))
async def test_agent_boot_audience_is_explicit_and_legacy_default_retained(boot_database, kind):
    async with boot_database() as fixture:
        row = await insert(fixture, kind, manifest(kind), audience='agent_boot')
        assert row['audience'] == 'agent_boot'
        legacy = await insert(fixture, kind, None)
        assert legacy['audience'] == 'scope'

@native
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('damage', ('unknown', 'wrong-schema', 'missing-nullable', 'numeric-version', 'bad-scope', 'escape-ref', 'dot-ref', 'empty-ref'))
async def test_sql_rejects_ambiguous_or_incomplete_metadata(boot_database, kind, damage):
    value = copy.deepcopy(manifest(kind))
    if damage == 'unknown': value['can_grant'] = True
    elif damage == 'wrong-schema': value['schema_version'] = 'cortex.untrusted.v1'
    elif damage == 'missing-nullable': del value['name']
    elif damage == 'numeric-version': value['version'] = 1
    elif damage == 'bad-scope': value['scope'] = 'installation'
    elif damage == 'escape-ref': value['body_ref'] = '../outside.md'
    elif damage == 'dot-ref': value['body_ref'] = '.'
    elif damage == 'empty-ref': value['body_ref'] = ''
    async with boot_database() as fixture:
        with pytest.raises(asyncpg.CheckViolationError):
            await insert(fixture, kind, value)
        assert await fixture['db'].fetchval(f'SELECT count(*) FROM cortex_context.{kind}_revisions') == 0

@native
@pytest.mark.parametrize('kind', ('persona', 'rule'))
async def test_unrecognized_audience_refuses_without_row(boot_database, kind):
    async with boot_database() as fixture:
        with pytest.raises(asyncpg.CheckViolationError):
            await insert(fixture, kind, manifest(kind), audience='all-agents')
        assert await fixture['db'].fetchval(f'SELECT count(*) FROM cortex_context.{kind}_revisions') == 0
