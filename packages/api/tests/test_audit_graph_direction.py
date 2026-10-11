"""Frozen direction assertions delegated to real disposable SQL; no live API."""
import ast,asyncio,copy,json,os,subprocess,unittest
from pathlib import Path
import pytest
WT=Path(__file__).resolve().parents[3]
SOURCE=WT/'packages/api/main.py'
_PORT=None
KEYS={'seed_name','seed_type','related_name','related_type','relationship_type','rel_description'}
TRACE=[]
def namespace():
    tree=ast.parse(SOURCE.read_text());n=copy.deepcopy(next(x for x in tree.body if getattr(x,'name','')=='search_graph'));n.decorator_list=[]
    future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
    ns={'__file__':str(SOURCE)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future,n],type_ignores=[])),str(SOURCE),'exec'),ns)
    return ns

def quote(value):
    if value is None:return 'NULL'
    if isinstance(value,int):return str(value)
    return "'"+str(value).replace("'","''")+"'"

class NativePort:
    def __init__(self,name):self.name=name
    def sql(self,statement):
        return subprocess.check_output(['podman','exec','-i',self.name,'psql','-X','-q','-At','-U','postgres','-v','ON_ERROR_STOP=1'],input=statement,text=True).strip()
    def rows(self,statement,args):
        assert len(args)==4
        # Preserve actual SQL and its parameter positions. JSON retains NULL and
        # types; no direction guessing or handcrafted replacement row values.
        wrapped='SELECT row_to_json(t) FROM ('+statement.strip().rstrip(';')+') t'
        prepared='PREPARE graph_proof(text,text,text,integer) AS '+wrapped+'; EXECUTE graph_proof('+','.join(quote(x) for x in args)+');'
        raw=self.sql(prepared);result=[json.loads(line) for line in raw.splitlines() if line]
        TRACE.append({'sql':statement,'parameters':list(args),'rows':result})
        return result

@pytest.fixture(autouse=True)
def disposable_graph():
    global _PORT
    name=os.environ.get('CORTEX_GRAPH_PROOF_CONTAINER')
    if not name:pytest.skip('explicit owned disposable schema PG required')
    info=json.loads(subprocess.check_output(['podman','inspect',name],text=True))[0];h=info['HostConfig']
    assert info['Config']['Labels'].get('io.kaidera.proof')=='bob-api1-007-20261010'
    assert h['NetworkMode']=='none' and not h.get('PortBindings')
    assert 0<h['Memory']<=1073741824
    assert h['CpuQuota']>0 and h['CpuPeriod']>0 and h['CpuQuota']/h['CpuPeriod']<=2
    _PORT=NativePort(name)
    _PORT.sql("INSERT INTO cortex_projects(project_key,display_name,repo_root) VALUES('test','Synthetic graph','/proof/test'),('other-proof','Other graph','/proof/other') ON CONFLICT(project_key) DO NOTHING; DELETE FROM cortex_relationships WHERE project IN ('test','other-proof'); DELETE FROM cortex_entities WHERE project IN ('test','other-proof');")
    _PORT.sql("INSERT INTO cortex_entities(id,project,name,entity_type) VALUES('30000000-0000-0000-0000-000000000001','test','Source','service'),('30000000-0000-0000-0000-000000000002','test','Target','service'),('30000000-0000-0000-0000-000000000003','other-proof','Source','service'),('30000000-0000-0000-0000-000000000004','other-proof','Target','service'); INSERT INTO cortex_relationships(project,source_entity_id,target_entity_id,relationship_type,properties) VALUES('test','30000000-0000-0000-0000-000000000001','30000000-0000-0000-0000-000000000002','depends_on','{}'),('other-proof','30000000-0000-0000-0000-000000000003','30000000-0000-0000-0000-000000000004','depends_on',jsonb_build_object('description','OTHER-PROJECT'));")
    yield
    _PORT.sql("DELETE FROM cortex_relationships WHERE project IN ('test','other-proof'); DELETE FROM cortex_entities WHERE project IN ('test','other-proof');")
    receipt=os.environ.get('CORTEX_GRAPH_TRACE_FILE')
    if receipt:Path(receipt).write_text(json.dumps(TRACE,indent=2))
    _PORT=None

def native_fetch(sql,args):
    rows=_PORT.rows(sql,args)
    for row in rows:
        assert set(row)==KEYS
        assert all(isinstance(row[k],str) for k in KEYS-{'rel_description'})
        assert row['rel_description'] is None or isinstance(row['rel_description'],str)
    return rows

class ActualAPIControls(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_graph_preserves_actual_direction(self):
            ns=namespace()
            class Connection:
                async def fetch(self,sql,*args):
                    self.sql=sql
                    return native_fetch(sql,args)
            connection=Connection();rows=await ns['search_graph'](connection,'test','Target',None)
            self.assertIn('r.target_entity_id = s.id',connection.sql)
            self.assertEqual(rows[0]['text'],'Source (service) --depends_on--> Target (service)',
                'incoming alias is rendered with the reversed directed relationship')

    async def test_outgoing_and_incoming_agree_with_stored_direction(self):
        ns=namespace()
        class Connection:
            async def fetch(self,sql,*args):return native_fetch(sql,args)
        conn=Connection()
        outgoing=await ns['search_graph'](conn,'test','Source',None)
        incoming=await ns['search_graph'](conn,'test','Target',None)
        expected='Source (service) --depends_on--> Target (service)'
        self.assertEqual([r['text'] for r in outgoing],[expected])
        self.assertEqual([r['text'] for r in incoming],[expected])
        self.assertEqual(outgoing,incoming)
        self.assertEqual(incoming[0]['meta'],'knowledge graph')
        self.assertEqual(await ns['search_graph'](conn,'test','missing',None),[])
        self.assertEqual(await ns['search_graph'](conn,'test','Target','unmatched-room'),[])
        self.assertEqual(await ns['search_graph'](conn,'unregistered','Target',None),[])
        self.assertEqual(await ns['search_graph'](conn,'test','Target',None),incoming)
        self.assertFalse(any('OTHER-PROJECT' in str(row) for row in incoming))

    async def test_json_transport_preserves_null_description_control(self):
        row=_PORT.rows("SELECT 'Source'::text AS seed_name,'service'::text AS seed_type,'Target'::text AS related_name,'service'::text AS related_type,'depends_on'::text AS relationship_type,NULL::text AS rel_description",('unused','test',None,6))[0]
        self.assertEqual(set(row),KEYS)
        self.assertIsNone(row['rel_description'])
        self.assertEqual(row['seed_name'],'Source')

    async def test_distinct_endpoint_types_and_description_preserve_real_direction(self):
        _PORT.sql("UPDATE cortex_entities SET entity_type='project' WHERE project='test' AND name='Target'; UPDATE cortex_relationships SET properties=jsonb_build_object('description','TYPE-CONTROL') WHERE project='test';")
        ns=namespace()
        class Connection:
            async def fetch(self,sql,*args):return native_fetch(sql,args)
        for query in ('Source','Target'):
            rows=await ns['search_graph'](Connection(),'test',query,None)
            self.assertEqual(rows[0]['text'],'Source (service) --depends_on--> Target (project)')
            self.assertEqual(rows[0]['meta'],'TYPE-CONTROL')
