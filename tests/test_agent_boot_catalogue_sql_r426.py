"""Real application-role published-origin RLS: exact head, owner, installation."""
import uuid
import pytest
from fixtures.agent_boot_native_r425 import boot_database
from fixtures.state_import_native import state_cluster
from fixtures.agent_boot_streams_r426 import append,app_reader,require_table,seed
from test_agent_boot_metadata_sql_r425 import insert,manifest,native

@native
@pytest.mark.parametrize('history,visible', (([],False),(['active'],True),(['active','retired'],False),(['active','retired','active'],True)))
async def test_exact_publication_head_controls_canonical_row_visibility(boot_database,history,visible):
    async with boot_database() as f:
        await seed(f);await require_table(f,'publication')
        for revision,state in enumerate(history,1): await append(f,'publication',revision=revision,state=state)
        async with app_reader(f) as app:
            rows=await app.fetch('SELECT skill_id,revision FROM cortex_context.skill_revisions WHERE scope_id=$1',f['catalogue'])
            assert [row['skill_id'] for row in rows] == ([f['global_skill']['skill_id']] if visible else [])
        assert await f['db'].fetchval('SELECT count(*) FROM cortex_auth.scope_grants WHERE principal_id=$1 AND scope_id=$2',f['member'],f['catalogue']) == 0

@native
async def test_publication_does_not_disclose_another_unpublished_canonical_row(boot_database):
    async with boot_database() as f:
        await seed(f);await require_table(f,'publication');await append(f,'publication')
        value=manifest('skill');value['scope']='global'
        other=await insert({**f,'scope':f['catalogue']},'skill',value)
        async with app_reader(f) as app:
            rows=await app.fetch('SELECT skill_id FROM cortex_context.skill_revisions WHERE scope_id=$1',f['catalogue'])
            assert [row['skill_id'] for row in rows] == [f['global_skill']['skill_id']]
            assert all(row['skill_id'] != other['skill_id'] for row in rows)
            assert await app.fetchval('SELECT count(*) FROM cortex_core.project_identities WHERE project_scope_id=$1',f['catalogue']) == 0

@native
async def test_distinct_active_publication_remains_when_another_stream_withdraws(boot_database):
    async with boot_database() as f:
        await seed(f);await require_table(f,'publication');await append(f,'publication')
        await append(f,'publication',publication_id=uuid.uuid4())
        await append(f,'publication',revision=2,state='retired')
        async with app_reader(f) as app:
            assert await app.fetchval('SELECT count(*) FROM cortex_context.skill_revisions WHERE scope_id=$1',f['catalogue']) == 1

@native
async def test_revoked_owner_publication_cannot_keep_exposing_a_canonical_row(boot_database):
    async with boot_database() as f:
        await seed(f);await require_table(f,'publication');await append(f,'publication')
        await f['db'].execute('UPDATE cortex_auth.installation_owners SET revoked_at=now() WHERE installation_id=$1 AND principal_id=$2',f['installation'],f['principal'])
        async with app_reader(f) as app:
            assert await app.fetchval('SELECT count(*) FROM cortex_context.skill_revisions WHERE scope_id=$1',f['catalogue']) == 0

@native
async def test_project_member_from_another_installation_cannot_read_publication(boot_database):
    async with boot_database() as f:
        await seed(f);await require_table(f,'publication');await append(f,'publication')
        inst,principal,actor,scope=(uuid.uuid4() for _ in range(4));db=f['db']
        await db.execute("INSERT INTO cortex_auth.installations(installation_id,display_name) VALUES($1,'PUBLIC other install')",inst)
        await db.execute("INSERT INTO cortex_auth.principals(principal_id,installation_id,principal_name,status) VALUES($1,$2,'public-other','active')",principal,inst)
        await db.execute("INSERT INTO cortex_auth.actors(actor_id,installation_id,actor_kind,display_name) VALUES($1,$2,'agent','public-other')",actor,inst)
        await db.execute('INSERT INTO cortex_auth.actor_bindings(actor_id,principal_id,bound_by_principal_id) VALUES($1,$2,$2)',actor,principal)
        await db.execute("INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES($1,'project','PUBLIC other project')",scope)
        await db.execute('INSERT INTO cortex_auth.project_installations(scope_id,installation_id) VALUES($1,$2)',scope,inst)
        await db.execute("INSERT INTO cortex_auth.memberships(scope_id,actor_id,membership_role) VALUES($1,$2,'member')",scope,actor)
        await db.execute('INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) VALUES($1,$2,true,true)',principal,scope)
        async with app_reader(f,principal,scope) as app:
            assert await app.fetchval('SELECT count(*) FROM cortex_context.skill_revisions WHERE scope_id=$1',f['catalogue']) == 0
            assert await app.fetchval('SELECT count(*) FROM cortex_context.boot_catalogue_publication_revisions') == 0

@native
@pytest.mark.parametrize('withdraw', ('member','grant','project','installation','curator'))
async def test_publication_visibility_requires_current_registered_reader_and_curator(boot_database,withdraw):
    async with boot_database() as f:
        await seed(f);await require_table(f,'publication');await append(f,'publication');db=f['db']
        if withdraw=='member':await db.execute("UPDATE cortex_auth.actors SET status='retired' WHERE actor_id=$1",f['actor'])
        elif withdraw=='grant':await db.execute('UPDATE cortex_auth.scope_grants SET revoked_at=now() WHERE principal_id=$1 AND scope_id=$2',f['member'],f['scope'])
        elif withdraw=='project':await db.execute('UPDATE cortex_core.scopes SET is_active=false WHERE scope_id=$1',f['scope'])
        elif withdraw=='installation':await db.execute("UPDATE cortex_auth.installations SET status='decommissioned' WHERE installation_id=$1",f['installation'])
        else:await db.execute('UPDATE cortex_auth.scope_grants SET can_publish=false WHERE principal_id=$1 AND scope_id=$2',f['principal'],f['catalogue'])
        async with app_reader(f) as app:
            assert await app.fetchval('SELECT count(*) FROM cortex_context.skill_revisions WHERE scope_id=$1',f['catalogue'])==0
