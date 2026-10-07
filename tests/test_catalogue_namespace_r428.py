"""Actual retained public SQL rows through catalogue boundaries; native effects explicit seams."""
import asyncio
import copy
import json
from pathlib import Path
import pytest
from cortex_v2 import build_catalog,native_prerequisite
from test_cm2_build_catalog_rehearsal import load
from test_cm2_build_catalog_assembly import observation,SOURCE,VERSION

ROOT=Path(__file__).resolve().parents[1]
PUBLIC=json.loads((Path(__file__).parent/'fixtures/r428-full-catalogue.json').read_text())
SCHEMAS=('cortex_auth','cortex_core','cortex_coord','cortex_processing','cortex_retrieval','cortex_context','cortex_feed','cortex_verification')


def test_producer_accepts_actual_all_namespace_rows_without_weakening_rls():
    rows=PUBLIC['rows'];result=build_catalog._relations(rows)
    assert len(result)==129 and sum(r['name'].startswith('cortex_context.boot_') for r in result)==3


@pytest.mark.parametrize('schema',SCHEMAS)
def test_actual_relation_in_each_exact_namespace_binds_to_current_payload(schema,monkeypatch):
    module=load('linux_build_catalog.py',monkeypatch)
    payload=native_prerequisite.payload_inventory(ROOT/'src',ROOT/'migrations')
    rows=[{k:v for k,v in r.items() if k!='app_owns'} for r in PUBLIC['rows'] if r['name'].startswith(schema+'.')]
    ledger=PUBLIC['ledger'];catalog=dict(schema='cortex.rls-inventory.v1',source_revision=SOURCE,api_source_payload_sha256=payload['sha256'],migrations={r['migration']:r['checksum'] for r in ledger},relations=rows)
    receipt=dict(instance='cortex_v2_package_test',fixture='verified',status='applied',migrations=ledger,build_catalog=catalog)
    raw=module.catalog_bytes(receipt,source_sha=SOURCE,source_root=ROOT/'src',migration_root=ROOT/'migrations')
    assert json.loads(raw)==catalog


@pytest.mark.parametrize('boundary',['producer','observer'])
def test_sql_observation_queries_all_exact_product_namespaces(boundary,monkeypatch):
    queries=[]
    class Stop(Exception):pass
    class Conn:
        def transaction(self,**kwargs):return self
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def fetchrow(self,query):return {'role_name':'cortex_v2_migrator' if boundary=='producer' else 'cortex_v2_app','rolsuper':False,'rolbypassrls':False,'migrator_member':False}
        async def fetch(self,query):queries.append(query);raise Stop
    async def role(*args):pass
    monkeypatch.setattr(build_catalog,'_assert_role_contract',role)
    async def run():
        with pytest.raises((Stop,native_prerequisite.NativeRefusal)):
            if boundary=='producer':await build_catalog.materialize_catalog(Conn(),source_revision=SOURCE,source_root=ROOT/'src',migration_root=ROOT/'migrations')
            else:await native_prerequisite.read_native_database(Conn(),{},project='public',project_root='/PUBLIC',migrations={},expected_relations=[])
    asyncio.run(run())
    assert len(queries)==1
    assert all("'"+s+"'" in queries[0] for s in SCHEMAS)
    assert 'public' not in queries[0]


def test_rehearsal_binds_full18_payload_instead_of_stale15_count(tmp_path,monkeypatch):
    module,entries,value,unused=observation(tmp_path,monkeypatch)
    migrations=tmp_path/'migrations'
    for p in migrations.iterdir():p.unlink()
    for name in (ROOT/'migrations').iterdir():(migrations/name.name).write_bytes(name.read_bytes())
    payload=native_prerequisite.payload_inventory(tmp_path/'src',migrations)
    ledger=PUBLIC['ledger'];hashes={r['migration']:r['checksum'] for r in ledger}
    for receipt in (value['migration'],value['migration_replay']):
        receipt['migrations']=copy.deepcopy(ledger);receipt['build_catalog']['migrations']=hashes;receipt['build_catalog']['api_source_payload_sha256']=payload['sha256']
    raw=json.dumps(value['build_catalog'],sort_keys=True,separators=(',',':')).encode()+b'\n'
    import hashlib
    value['build_catalog_sha256']=hashlib.sha256(raw).hexdigest()
    assert module.linux_catalog_bytes(value,entries,SOURCE,VERSION)==raw
