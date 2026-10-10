"""Frozen metric cardinality gate plus local ASGI routing controls; no live I/O."""
import ast,asyncio,copy,time,unittest
from pathlib import Path
from types import SimpleNamespace
WT=Path(__file__).resolve().parents[3]
SOURCE=WT/'packages/api/main.py'
def namespace():
    tree=ast.parse(SOURCE.read_text());n=copy.deepcopy(next(x for x in tree.body if getattr(x,'name','')=='prometheus_middleware'));n.decorator_list=[]
    future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
    ns={'time':time,'__file__':str(SOURCE)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future,n],type_ignores=[])),str(SOURCE),'exec'),ns)
    return ns

class ActualAPIControls(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_paths_collapse_different_uuid_instances(self):
            ns=namespace();labels=[]
            class Histogram:
                def labels(self,**value):labels.append(value);return self
                def observe(self,value):pass
            ns['REQUEST_DURATION']=Histogram()
            async def response(request):return SimpleNamespace(status_code=200)
            for value in ['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002']:
                await ns['prometheus_middleware'](SimpleNamespace(method='GET',url=SimpleNamespace(path='/handoffs/'+value)),response)
            self.assertEqual(len({x['endpoint'] for x in labels}),1,
                'UUID-instance labels create a separate latency time series for every resource')


def test_real_asgi_route_templates_distinguish_static_routes_and_bound_unmatched():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    labels=[];durations=[]
    class Histogram:
        def labels(self,**values):labels.append(values);return self
        def observe(self,value):durations.append(value)
    ns=namespace();ns['REQUEST_DURATION']=Histogram();app=FastAPI();app.middleware('http')(ns['prometheus_middleware'])
    @app.get('/handoffs/{handoff_id}')
    async def handoff(handoff_id:str):return {'synthetic':True}
    @app.get('/health')
    async def health():return {'synthetic':True}
    @app.get('/metrics')
    async def metrics():return {'synthetic':True}
    c=TestClient(app)
    for id in ['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002']:
        assert c.get('/handoffs/'+id).status_code==200
    assert labels[:2]==[{'method':'GET','endpoint':'/handoffs/{handoff_id}'}]*2
    assert c.get('/health').status_code==200
    assert labels[-1]['endpoint']=='/health'
    for id in ['first-unknown','second-unknown']:assert c.get('/unregistered/'+id).status_code==404
    assert labels[-2:]==[{'method':'GET','endpoint':'unmatched'}]*2
    count=len(labels);assert c.get('/metrics').status_code==200 and len(labels)==count
    assert len(durations)==len(labels) and all(value>=0 for value in durations)

def test_auth_early_401_uses_fixed_unmatched_label_for_uuid_paths():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    labels=[]
    class Histogram:
        def labels(self,**values):labels.append(values);return self
        def observe(self,value):pass
    async def auth_refusal(request,call_next):
        return JSONResponse({'detail':'synthetic-auth-refusal'},status_code=401)
    ns=namespace();ns['REQUEST_DURATION']=Histogram();app=FastAPI()
    app.middleware('http')(auth_refusal)
    # Outermost metrics sees an inner auth response before route resolution.
    app.middleware('http')(ns['prometheus_middleware'])
    @app.get('/handoffs/{handoff_id}')
    async def handoff(handoff_id:str):raise AssertionError('auth refusal should not reach routing')
    c=TestClient(app)
    for id in ['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002']:
        assert c.get('/handoffs/'+id).status_code==401
    assert labels==[{'method':'GET','endpoint':'unmatched'}]*2
