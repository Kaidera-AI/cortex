"""RED-first native append-stream integrity; production authority not stubbed."""
import uuid
import asyncpg
import pytest
from fixtures.agent_boot_native_r425 import boot_database
from fixtures.state_import_native import state_cluster
from fixtures.agent_boot_streams_r426 import TABLES, append, caller, require_table, seed
from test_agent_boot_metadata_sql_r425 import native

@native
@pytest.mark.parametrize('stream', TABLES)
async def test_new_stream_forces_rls_and_has_only_select_insert_app_privilege(boot_database, stream):
    async with boot_database() as f:
        await require_table(f, stream)
        name='cortex_context.'+TABLES[stream]
        row=await f['db'].fetchrow('SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=$1::regclass',name)
        assert row['relrowsecurity'] and row['relforcerowsecurity']
        for privilege in ('SELECT','INSERT'):
            assert await f['db'].fetchval('SELECT has_table_privilege($1,$2,$3)','cortex_v2_app',name,privilege)
        for privilege in ('UPDATE','DELETE','TRUNCATE'):
            assert not await f['db'].fetchval('SELECT has_table_privilege($1,$2,$3)','cortex_v2_app',name,privilege)

@native
@pytest.mark.parametrize('stream', TABLES)
async def test_append_history_keeps_retired_and_reactivated_heads(boot_database, stream):
    async with boot_database() as f:
        await seed(f)
        first=await append(f,stream)
        retired=await append(f,stream,revision=2,state='retired')
        active=await append(f,stream,revision=3,state='active')
        assert [first['revision'],retired['revision'],active['revision']] == [1,2,3]
        assert [first['state'],retired['state'],active['state']] == ['active','retired','active']
        assert await f['db'].fetchval('SELECT max(revision) FROM cortex_context.'+TABLES[stream]) == 3

@native
@pytest.mark.parametrize('stream', TABLES)
@pytest.mark.parametrize('revision', (2,3))
async def test_first_revision_cannot_skip_a_predecessor(boot_database,stream,revision):
    async with boot_database() as f:
        await seed(f);await require_table(f,stream)
        with pytest.raises(asyncpg.CheckViolationError): await append(f,stream,revision=revision)
        assert await f['db'].fetchval('SELECT count(*) FROM cortex_context.'+TABLES[stream]) == 0

@native
@pytest.mark.parametrize('stream', TABLES)
@pytest.mark.parametrize('mutation', ('UPDATE','DELETE','TRUNCATE'))
async def test_even_schema_owner_cannot_rewrite_or_clear_stream(boot_database,stream,mutation):
    async with boot_database() as f:
        await seed(f);await append(f,stream)
        table='cortex_context.'+TABLES[stream]
        before=await f['db'].fetch('SELECT to_jsonb(t)::text FROM '+table+' t')
        statement = 'UPDATE '+table+" SET state='retired'" if mutation=='UPDATE' else mutation+(' FROM ' if mutation=='DELETE' else ' ')+table
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError): await f['db'].execute(statement)
        assert await f['db'].fetch('SELECT to_jsonb(t)::text FROM '+table+' t') == before

@native
@pytest.mark.parametrize('stream', TABLES)
async def test_member_write_grant_does_not_confer_boot_enact_authority(boot_database,stream):
    async with boot_database() as f:
        await seed(f);await require_table(f,stream);await caller(f,f['member'])
        change={'published_by_principal':f['member']} if stream=='publication' else {'enacted_by_principal':f['member']}
        with pytest.raises(asyncpg.InsufficientPrivilegeError): await append(f,stream,**change)
        assert await f['db'].fetchval('SELECT count(*) FROM cortex_context.'+TABLES[stream]) == 0

@native
@pytest.mark.parametrize('damage', ('identity','persona-revision','roles','profile','identity-name'))
async def test_agent_binding_rejects_unregistered_or_noncanonical_fact(boot_database,damage):
    async with boot_database() as f:
        await seed(f);await require_table(f,'agent')
        change={'identity_id':uuid.uuid4()} if damage=='identity' else {'persona_revision':2} if damage=='persona-revision' else {'functional_roles':['unregistered']}
        if damage=='profile':
            from test_agent_boot_metadata_sql_r425 import insert,manifest
            value=manifest('persona');value['identity_text']='PUBLIC unregistered profile'
            persona=await insert(f,'persona',value,audience='agent_boot');change={'persona_id':persona['persona_id']}
        elif damage=='identity-name':
            identity=uuid.uuid4()
            await f['db'].execute("INSERT INTO cortex_core.project_identities VALUES($1,$2,'PUBLIC-other-identity','foreign-agent','agent',$3::jsonb)",identity,f['scope'],'{"role":"PUBLIC-role"}')
            change={'identity_id':identity}
        with pytest.raises((asyncpg.CheckViolationError,asyncpg.ForeignKeyViolationError)): await append(f,'agent',**change)
        assert await f['db'].fetchval('SELECT count(*) FROM cortex_context.boot_agent_binding_revisions') == 0

@native
@pytest.mark.parametrize('change', ({'subject_kind':'project'},{'role_slug':'PUBLIC-role'}, {'rule_id':uuid.UUID('42600000-0000-4000-8000-000000000001')}, {'bound_revision':2}, {'source_reference':''}))
async def test_entry_binding_rejects_ambiguous_subject_entry_or_provenance(boot_database,change):
    async with boot_database() as f:
        await seed(f);await require_table(f,'entry')
        with pytest.raises((asyncpg.CheckViolationError,asyncpg.ForeignKeyViolationError)): await append(f,'entry',**change)
        assert await f['db'].fetchval('SELECT count(*) FROM cortex_context.boot_entry_binding_revisions') == 0

@native
@pytest.mark.parametrize('change', ({'catalogue_scope_id':'project'}, {'entry_revision':2}, {'source_reference':''}))
async def test_publication_requires_shared_exact_canonical_revision(boot_database,change):
    async with boot_database() as f:
        await seed(f);await require_table(f,'publication');change=dict(change)
        if change.get('catalogue_scope_id')=='project':change['catalogue_scope_id']=f['scope']
        with pytest.raises((asyncpg.CheckViolationError,asyncpg.ForeignKeyViolationError)): await append(f,'publication',**change)
        assert await f['db'].fetchval('SELECT count(*) FROM cortex_context.boot_catalogue_publication_revisions') == 0

@native
async def test_valid_foreign_project_entry_is_not_a_published_catalogue_binding(boot_database):
    from test_agent_boot_metadata_sql_r425 import insert,manifest
    async with boot_database() as f:
        await seed(f);await require_table(f,'entry');scope=uuid.uuid4();db=f['db']
        await db.execute("INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES($1,'project','PUBLIC foreign project')",scope)
        await db.execute('INSERT INTO cortex_auth.project_installations(scope_id,installation_id) VALUES($1,$2)',scope,f['installation'])
        await db.execute('INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) VALUES($1,$2,true,true)',f['principal'],scope)
        foreign=await insert({**f,'scope':scope},'skill',manifest('skill'))
        with pytest.raises(asyncpg.CheckViolationError):
            await append(f,'entry',entry_scope_id=scope,skill_id=foreign['skill_id'])
        assert await db.fetchval('SELECT count(*) FROM cortex_context.boot_entry_binding_revisions') == 0

@native
@pytest.mark.parametrize('stream',TABLES)
async def test_concurrent_same_expected_head_creates_only_one_next_revision(boot_database,stream):
    import asyncio
    from fixtures.agent_boot_streams_r426 import app_reader
    async with boot_database() as f:
        await seed(f);await append(f,stream)
        async def enact(connection):
            try:
                async with connection.transaction():
                    return await append({**f,'db':connection},stream,revision=2)
            except asyncpg.CheckViolationError as error:
                return error
        async with app_reader(f,f['principal']) as left, app_reader(f,f['principal']) as right:
            results=await asyncio.gather(enact(left),enact(right))
        assert sum(isinstance(row,asyncpg.CheckViolationError) for row in results)==1
        assert await f['db'].fetchval('SELECT count(*) FROM cortex_context.'+TABLES[stream])==2
