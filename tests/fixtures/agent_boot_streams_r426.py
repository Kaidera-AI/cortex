"""Registered public identities for boot append/RLS SQL tests, never live data."""
import json
import uuid
from test_agent_boot_metadata_sql_r425 import insert, manifest

TABLES = dict(agent='boot_agent_binding_revisions', entry='boot_entry_binding_revisions',
              publication='boot_catalogue_publication_revisions')

async def seed(f):
    db = f['db']; scope = f['scope']; owner = f['principal']; installation = f['installation']
    await db.execute('INSERT INTO cortex_auth.installation_owners(installation_id,principal_id) VALUES($1,$2)', installation, owner)
    await db.execute('INSERT INTO cortex_auth.project_installations(scope_id,installation_id) VALUES($1,$2)', scope, installation)
    await db.execute("""INSERT INTO cortex_core.project_registry
        VALUES($1,'PUBLIC-project','PUBLIC project',NULL,'active',NULL,'/fixture/public-boot',
               '[{"path":"/fixture/public-boot","kind":"primary"}]',now(),now())""", scope)
    f['actor'] = uuid.uuid4(); f['member'] = uuid.uuid4(); f['identity'] = uuid.uuid4()
    await db.execute("INSERT INTO cortex_auth.principals(principal_id,installation_id,principal_name,status) VALUES($1,$2,'public-agent','active')", f['member'], installation)
    await db.execute("INSERT INTO cortex_auth.actors(actor_id,installation_id,actor_kind,display_name) VALUES($1,$2,'agent','public-agent')", f['actor'], installation)
    await db.execute('INSERT INTO cortex_auth.actor_bindings(actor_id,principal_id,bound_by_principal_id) VALUES($1,$2,$3)', f['actor'], f['member'], owner)
    await db.execute("INSERT INTO cortex_auth.memberships(scope_id,actor_id,membership_role) VALUES($1,$2,'member')", scope, f['actor'])
    await db.execute('INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) VALUES($1,$2,true,true)', f['member'], scope)
    await db.execute("INSERT INTO cortex_core.project_identities VALUES($1,$2,'PUBLIC-identity','public-agent','agent',$3::jsonb)", f['identity'], scope, json.dumps({'name':'public-agent','role':'PUBLIC-role'}))
    await db.execute("INSERT INTO cortex_core.project_profiles VALUES($1,$2,'PUBLIC-profile','public-agent',$3::jsonb)", uuid.uuid4(), scope, json.dumps({'profile_kind':'identity','profile_text':'PUBLIC identity'}))
    f['persona'] = await insert(f, 'persona', manifest('persona'), audience='agent_boot')
    f['skill'] = await insert(f, 'skill', manifest('skill'))
    f['catalogue'] = uuid.uuid4(); f['publication'] = uuid.uuid4(); f['binding'] = uuid.uuid4()
    await db.execute("INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES($1,'shared','PUBLIC catalogue')", f['catalogue'])
    await db.execute('INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write,can_publish) VALUES($1,$2,true,true,true)', owner, f['catalogue'])
    global_f = {**f, 'scope':f['catalogue']}; value = manifest('skill'); value['scope'] = 'global'
    f['global_skill'] = await insert(global_f, 'skill', value)
    await caller(f, owner)
    return f

async def caller(f, principal):
    await f['db'].execute("SELECT set_config('cortex.principal_id',$1,false),set_config('cortex.read_scope_ids',$2,false),set_config('cortex.write_scope_id',$2,false)", str(principal), str(f['scope']))

async def require_table(f, stream):
    name = 'cortex_context.' + TABLES[stream]
    assert await f['db'].fetchval('SELECT to_regclass($1)::text', name) == name

async def append(f, stream, **changes):
    await require_table(f, stream)
    if stream == 'agent':
        row = dict(project_scope_id=f['scope'],actor_id=f['actor'],revision=1,state='active',
                   identity_id=f['identity'],persona_id=f['persona']['persona_id'],persona_revision=1,
                   functional_roles=['PUBLIC-role'],enacted_by_principal=f['principal'],source_reference='PUBLIC fixture')
    elif stream == 'entry':
        row = dict(project_scope_id=f['scope'],binding_id=f['binding'],revision=1,subject_kind='agent',
                   actor_id=f['actor'],role_slug=None,entry_kind='skill',entry_scope_id=f['scope'],
                   skill_id=f['skill']['skill_id'],rule_id=None,bound_revision=1,priority=0,state='active',
                   enacted_by_principal=f['principal'],source_reference='PUBLIC fixture')
    else:
        row = dict(installation_id=f['installation'],publication_id=f['publication'],revision=1,
                   catalogue_scope_id=f['catalogue'],entry_kind='skill',entry_id=f['global_skill']['skill_id'],
                   entry_revision=1,state='active',published_by_principal=f['principal'],source_reference='PUBLIC fixture')
    row.update(changes)
    columns=','.join(row); values=','.join('$'+str(i) for i in range(1,len(row)+1))
    return await f['db'].fetchrow('INSERT INTO cortex_context.'+TABLES[stream]+'('+columns+') VALUES('+values+') RETURNING *', *row.values())

from contextlib import asynccontextmanager
from pathlib import Path
import os
import stat
import asyncpg

@asynccontextmanager
async def app_reader(f, principal=None, scope=None):
    db=f['db']
    # Reconnect to the fixture's actual socket; PG18 hides the server socket
    # GUC from the migrator. Do not grant pg_read_all_settings to a test role.
    address=Path(db._addr)
    socket=address.parent
    port=int(address.name.removeprefix('.s.PGSQL.'))
    assert str(socket).startswith(os.environ['CORTEX_NATIVE_FIXTURE_ROOT']+'/')
    assert not socket.is_symlink() and stat.S_IMODE(socket.stat().st_mode)==0o700
    database=await db.fetchval('SELECT current_database()')
    assert database=='kaidera-test-bootdb'
    app=await asyncpg.connect(host=str(socket),port=port,
                             database=database,user='cortex_v2_app',password='',ssl=False,command_timeout=10)
    try:
        await caller({**f,'db':app,'scope':scope or f['scope']},principal or f['member'])
        yield app
    finally:
        await app.close()
