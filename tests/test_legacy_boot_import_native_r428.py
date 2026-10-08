"""Public canonical legacy fixture import; expected rows come from actual source receipts."""
import asyncio,collections,copy,hashlib,importlib,json,os,uuid
from contextlib import asynccontextmanager
from pathlib import Path
import pytest
from fixtures.agent_boot_native_r425 import boot_database
from fixtures.state_import_native import state_cluster
from test_agent_boot_metadata_sql_r425 import native
ROOT=Path(__file__).resolve().parents[1];FIX=ROOT/'tests/fixtures/legacy_boot_r428'
KINDS={'public.agent_profiles':'persona','public.rules':'rule','public.agent_skills':'skill'}

def fixture_data():
 provenance=json.loads((FIX/'provenance.json').read_text())
 for name,digest in provenance['fixtures'].items():assert hashlib.sha256((FIX/name).read_bytes()).hexdigest()==digest
 rows=json.loads((FIX/'rows.json').read_text());assert dict(collections.Counter(x['table'] for x in rows))==provenance['parts'];assert len(rows)==sum(provenance['parts'].values())==provenance['total']
 return rows,json.loads((FIX/'bodies.json').read_text()),json.loads((FIX/'cases.json').read_text())

@asynccontextmanager
async def fixture(source_factory,target_factory):
 rows,bodies,cases=fixture_data()
 async with source_factory() as pair,target_factory() as f:
  await pair['writer'].execute((FIX/'source-schema.sql').read_text())
  for row in rows:await pair['writer'].execute(f"INSERT INTO {row['table']} SELECT * FROM jsonb_populate_record(NULL::{row['table']},$1::jsonb)",json.dumps(row['row']))
  f['global_scope']=uuid.uuid4()
  await f['db'].execute("INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES($1,'shared','PUBLIC legacy catalogue')",f['global_scope'])
  await f['db'].execute('INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) VALUES($1,$2,true,true)',f['principal'],f['global_scope'])
  originals={}
  for table in KINDS:
   for row in await pair['source'].fetch(f'SELECT id,to_jsonb(t)::text AS original FROM {table} t ORDER BY id'):
    originals[table+':'+str(row['id'])]=row['original'].encode()
  receipt={'database':pair['source_name'],'originals':{k:v.decode() for k,v in originals.items()},'body_sha256':{k:hashlib.sha256(v.encode()).hexdigest() for k,v in bodies.items()}}
  if os.environ.get('CORTEX_LEGACY_EVIDENCE'):
   dest=Path(os.environ['CORTEX_LEGACY_EVIDENCE']);dest.mkdir(exist_ok=True);(dest/('source-'+pair['source_name']+'.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
  f.update(pair=pair,originals=originals,bodies=bodies,cases=cases,source_receipt=receipt)
  yield f

def module():
 assert importlib.util.find_spec('cortex_v2.legacy_boot'),'canonical legacy adapter missing'
 return importlib.import_module('cortex_v2.legacy_boot')

async def inputs(f):
 m=module();s=await m.read_snapshot(f['pair']['source'],expected_database=f['pair']['source_name'],bodies=f['bodies'])
 assert {r.source_reference:r.original_bytes for r in s.records}==f['originals']
 p={'schema':'cortex.legacy-boot-policy.v1','source_database':s.database,'source_snapshot_sha256':s.fingerprint,
    'target_installation_id':str(f['installation']),'target_database':await f['db'].fetchval('SELECT current_database()'),'principal_id':str(f['principal']),
    'projects':{'public-project':{'scope_id':str(f['scope']),'scope':'project'},'_global':{'scope_id':str(f['global_scope']),'scope':'global'}},'records':{}}
 for ref,raw in f['originals'].items():
  row=json.loads(raw);p['records'][ref]={'kind':KINDS[ref.split(':')[0]],'id':row['id'],'revision':1,'audience':'agent_boot','obligation':'mandatory'}
 return m,s,p

async def canonical_rows(f):
 result={}
 for kind in KINDS.values():result[kind]=[dict(x) for x in await f['db'].fetch(f'SELECT * FROM cortex_context.{kind}_revisions ORDER BY scope_id,{kind}_id,revision')]
 return result

async def authority(f):
 tables=['cortex_auth.principals','cortex_auth.scope_grants','cortex_auth.actor_bindings','cortex_auth.installation_owners','cortex_auth.memberships','cortex_context.boot_agent_binding_revisions','cortex_context.boot_entry_binding_revisions','cortex_context.boot_catalogue_publication_revisions']
 return {table:[str(x['original']) for x in await f['db'].fetch(f'SELECT to_jsonb(t)::text AS original FROM {table} t ORDER BY to_jsonb(t)::text')] for table in tables}

@native
async def test_three_kinds_preserve_public_originals_ids_metadata_bodies_and_no_authority(state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  before=await authority(f);m,s,p=await inputs(f);r=await m.import_snapshot(f['db'],s,p,idempotency_key='public-import')
  assert await authority(f)==before
  expected=collections.Counter('migrated' if case=='accepted' else 'quarantined' for case in f['cases'].values())
  assert r['counts']==dict(expected) and sum(r['counts'].values())==len(f['originals'])
  assert len(r['rows'])==len(f['originals']) and {x['source_reference'] for x in r['rows']}==set(f['originals'])
  assert await m.read_inverse(f['db'],r)==f['originals']
  actual=await canonical_rows(f)
  for item in r['rows']:
   ref=item['source_reference'];original=f['originals'][ref];row=json.loads(original)
   assert item['original']==original.decode() and item['source_sha256']==hashlib.sha256(original).hexdigest()
   if f['cases'][ref]!='accepted':assert item['outcome']=='quarantined' and item['reason'];continue
   kind=KINDS[ref.split(':')[0]];scope=p['projects'][row['project']]['scope_id']
   found=[x for x in actual[kind] if str(x[kind+'_id'])==row['id'] and str(x['scope_id'])==scope]
   assert len(found)==1;native_row=found[0];body=row.get('profile_text',row.get('body',f['bodies'].get(row.get('body_ref'))))
   assert native_row['body']==body and item['body_sha256']==hashlib.sha256(body.encode()).hexdigest()
   manifest=json.loads(native_row['boot_manifest']);assert manifest['scope']==p['projects'][row['project']]['scope']
   assert manifest['permission'] is None and manifest['description'] is None
   assert native_row['revision']==p['records'][ref]['revision']
   if kind=='persona':assert manifest['identity_text']==row['profile_text'] and manifest['functional_roles']==row['metadata']['functional_roles'] and native_row['audience']=='agent_boot'
   elif kind=='rule':assert manifest['title']==row['title'] and manifest['source_file']==row['source_file'] and native_row['audience']=='agent_boot'
   else:assert manifest['body_ref']==row['body_ref'] and manifest['version']==row['version']
  parts={k:len(v) for k,v in actual.items()};assert sum(parts.values())==r['counts'].get('migrated',0)

@native
async def test_exact_replay_and_changed_binding_conflict_no_append(state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f);r=await m.import_snapshot(f['db'],s,p,idempotency_key='same');before=await canonical_rows(f)
  assert await m.import_snapshot(f['db'],s,p,idempotency_key='same')==r and await canonical_rows(f)==before
  q=copy.deepcopy(p);next(iter(q['records'].values()))['audience']='scope'
  with pytest.raises(RuntimeError):await m.import_snapshot(f['db'],s,q,idempotency_key='same')
  assert await canonical_rows(f)==before

@native
@pytest.mark.parametrize('damage',['fingerprint','database','missing-map','duplicate-id','boolean-revision','extra-policy','wrong-id','wrong-kind'])
async def test_invalid_approved_policy_refuses_atomically(damage,state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f);q=copy.deepcopy(p);refs=list(q['records'])
  if damage=='fingerprint':q['source_snapshot_sha256']='0'*64
  elif damage=='database':q['source_database']='PUBLIC-wrong'
  elif damage=='missing-map':q['records'].pop(refs[0])
  elif damage=='duplicate-id':q['records'][refs[1]]['id']=q['records'][refs[0]]['id']
  elif damage=='boolean-revision':q['records'][refs[0]]['revision']=True
  elif damage=='extra-policy':q['can_grant']=True
  elif damage=='wrong-id':q['records'][refs[0]]['id']=str(uuid.uuid4())
  else:q['records'][refs[0]]['kind']='skill'
  before=await canonical_rows(f)
  with pytest.raises(RuntimeError):await m.import_snapshot(f['db'],s,q,idempotency_key='bad')
  assert await canonical_rows(f)==before and await f['db'].fetchval("SELECT count(*) FROM cortex_core.command_receipts WHERE operation='legacy.boot.import'")==0

@native
@pytest.mark.parametrize('when',['before','replay'])
@pytest.mark.parametrize('revoke',['principal','installation','grant'])
async def test_current_authority_checked_before_effects_or_replay(when,revoke,state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f)
  if when=='replay':await m.import_snapshot(f['db'],s,p,idempotency_key='authority')
  if revoke=='principal':await f['db'].execute("UPDATE cortex_auth.principals SET status='revoked' WHERE principal_id=$1",f['principal'])
  elif revoke=='installation':await f['db'].execute("UPDATE cortex_auth.installations SET status='decommissioned' WHERE installation_id=$1",f['installation'])
  else:await f['db'].execute('UPDATE cortex_auth.scope_grants SET can_write=false WHERE principal_id=$1',f['principal'])
  before=await canonical_rows(f)
  with pytest.raises(RuntimeError):await m.import_snapshot(f['db'],s,p,idempotency_key='authority')
  assert await canonical_rows(f)==before

@native
async def test_post_write_fault_rolls_back_all_effects_and_receipt(state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f);before=await canonical_rows(f)
  def fault(stage):assert stage=='before_commit';raise RuntimeError('PUBLIC injected fault')
  with pytest.raises(RuntimeError):await m.import_snapshot(f['db'],s,p,idempotency_key='fault',fault=fault)
  assert await canonical_rows(f)==before and await f['db'].fetchval("SELECT count(*) FROM cortex_core.command_receipts WHERE operation='legacy.boot.import'")==0
  r=await m.import_snapshot(f['db'],s,p,idempotency_key='fault');assert await m.read_inverse(f['db'],r)==f['originals']

@native
async def test_foreign_canonical_collision_is_never_overwritten(state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f);ref=next(k for k in f['cases'] if k.startswith('public.rules:') and f['cases'][k]=='accepted' and json.loads(f['originals'][k])['project']=='public-project');row=json.loads(f['originals'][ref])
  await f['db'].execute("INSERT INTO cortex_context.rule_revisions(scope_id,rule_id,revision,slug,obligation,state,body,created_by_principal) VALUES($1,$2,1,'foreign','optional','active','PUBLIC foreign',$3)",f['scope'],uuid.UUID(row['id']),f['principal'])
  before=await canonical_rows(f)
  with pytest.raises(RuntimeError):await m.import_snapshot(f['db'],s,p,idempotency_key='collision')
  assert await canonical_rows(f)==before

@native
async def test_explicit_second_revision_preserves_prior_inverse_and_stable_ids(state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f);first=await m.import_snapshot(f['db'],s,p,idempotency_key='revision-one');old={r.source_reference:r.original_bytes for r in s.records}
  bodies=dict(f['bodies'])
  for ref,case in f['cases'].items():
   if case!='accepted':continue
   table,identity=ref.split(':');row=json.loads(f['originals'][ref]);kind=KINDS[table]
   if kind=='persona':await f['pair']['writer'].execute("UPDATE public.agent_profiles SET profile_text=profile_text || 'PUBLIC second revision',metadata=metadata || '{\"version\":\"2\"}'::jsonb WHERE id=$1",uuid.UUID(identity))
   elif kind=='rule':await f['pair']['writer'].execute("UPDATE public.rules SET body=body || 'PUBLIC second revision',version='2' WHERE id=$1",uuid.UUID(identity))
   else:
    bodies[row['body_ref']]+='PUBLIC second revision'
    await f['pair']['writer'].execute("UPDATE public.agent_skills SET body_hash=$1,version='2' WHERE id=$2",hashlib.sha256(bodies[row['body_ref']].encode()).hexdigest(),uuid.UUID(identity))
  newer=await m.read_snapshot(f['pair']['source'],expected_database=f['pair']['source_name'],bodies=bodies);q=copy.deepcopy(p);q['source_snapshot_sha256']=newer.fingerprint
  for ref,case in f['cases'].items():
   if case=='accepted':q['records'][ref]['revision']=2
  second=await m.import_snapshot(f['db'],newer,q,idempotency_key='revision-two')
  assert await m.read_inverse(f['db'],first)==old
  assert await m.read_inverse(f['db'],second)=={r.source_reference:r.original_bytes for r in newer.records}
  actual=await canonical_rows(f)
  for kind,rows in actual.items():
   for identity in {str(r[kind+'_id']) for r in rows}:assert sorted(r['revision'] for r in rows if str(r[kind+'_id'])==identity)==[p['records'][next(k for k in p['records'] if k.endswith(identity))]['revision'],q['records'][next(k for k in q['records'] if k.endswith(identity))]['revision']]

@native
@pytest.mark.parametrize('damage',['metadata-list','bad-body-ref','empty-roles'])
async def test_ambiguous_source_rows_quarantine_with_originals(damage,state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f);kind='skill' if damage=='bad-body-ref' else 'persona';table=next(t for t,k in KINDS.items() if k==kind)
  ref=next(ref for ref,case in f['cases'].items() if case=='accepted' and ref.startswith(table+':'));identity=uuid.UUID(ref.split(':')[1])
  if damage=='metadata-list':await f['pair']['writer'].execute(f"UPDATE {table} SET metadata='[]'::jsonb WHERE id=$1",identity)
  elif damage=='empty-roles':await f['pair']['writer'].execute(f"UPDATE {table} SET metadata='{{\"functional_roles\":[]}}'::jsonb WHERE id=$1",identity)
  else:await f['pair']['writer'].execute(f"UPDATE {table} SET body_ref='../outside.md' WHERE id=$1",identity)
  newer=await m.read_snapshot(f['pair']['source'],expected_database=f['pair']['source_name'],bodies=f['bodies']);p['source_snapshot_sha256']=newer.fingerprint
  result=await m.import_snapshot(f['db'],newer,p,idempotency_key='ambiguous');item=next(x for x in result['rows'] if x['source_reference']==ref)
  assert item['outcome']=='quarantined' and item['native'] is None and item['reason']
  assert await m.read_inverse(f['db'],result)=={r.source_reference:r.original_bytes for r in newer.records}

@native
async def test_two_competing_keys_cannot_both_own_same_canonical_ids(state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f);name=await f['db'].fetchval('SELECT current_database()');other=await f['pair']['connect'](name,'cortex_v2_migrator')
  try:
   results=await asyncio.gather(m.import_snapshot(f['db'],s,p,idempotency_key='compete-a'),m.import_snapshot(other,s,p,idempotency_key='compete-b'),return_exceptions=True)
   good=[x for x in results if isinstance(x,dict)];bad=[x for x in results if isinstance(x,RuntimeError)]
   assert len(good)==len(bad)==1 and await m.read_inverse(f['db'],good[0])==f['originals']
   assert await f['db'].fetchval("SELECT count(*) FROM cortex_core.command_receipts WHERE operation='legacy.boot.import'")==len(good)
  finally:await other.close()

@native
async def test_wrong_role_cannot_import_and_source_wrong_database_refuses(state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f)
  with pytest.raises(RuntimeError):await m.read_snapshot(f['pair']['source'],expected_database='PUBLIC-wrong',bodies=f['bodies'])
  name=await f['db'].fetchval('SELECT current_database()');other=await f['pair']['connect'](name,'cortex_v2_app')
  try:
   with pytest.raises(RuntimeError):await m.import_snapshot(other,s,p,idempotency_key='wrong-role')
  finally:await other.close()
  assert all(not rows for rows in (await canonical_rows(f)).values())

@native
async def test_forged_receipt_inverse_cannot_relabel_originals(state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f);result=await m.import_snapshot(f['db'],s,p,idempotency_key='inverse');forged=copy.deepcopy(result);forged['rows'][0]['original']='PUBLIC forgery'
  with pytest.raises(RuntimeError):await m.read_inverse(f['db'],forged)
  assert await m.read_inverse(f['db'],result)==f['originals']

@native
async def test_wrong_target_database_binding_refuses_without_effects(state_cluster,boot_database):
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f);p['target_database']='PUBLIC-wrong'
  with pytest.raises(RuntimeError):await m.import_snapshot(f['db'],s,p,idempotency_key='wrong-database')
  assert all(not rows for rows in (await canonical_rows(f)).values())

@native
async def test_snapshot_tamper_and_out_of_order_revision_refuse(state_cluster,boot_database):
 import dataclasses
 async with fixture(state_cluster,boot_database) as f:
  m,s,p=await inputs(f)
  with pytest.raises(RuntimeError):await m.import_snapshot(f['db'],dataclasses.replace(s,fingerprint='0'*64),p,idempotency_key='tamper')
  q=copy.deepcopy(p)
  for ref in q['records']:
   if f['cases'][ref]=='accepted':q['records'][ref]['revision']=2
  with pytest.raises(RuntimeError):await m.import_snapshot(f['db'],s,q,idempotency_key='gap')
  assert all(not rows for rows in (await canonical_rows(f)).values())
