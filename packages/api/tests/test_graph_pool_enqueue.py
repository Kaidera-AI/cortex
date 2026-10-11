"""Actual pinned graph lease/enqueue/schema functions; controlled four-slot pool."""
import ast,asyncio,copy,json,unittest
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
W=Path(__file__).resolve().parents[3];S=W/'packages/api/main.py'
NAMES={'graph_registry_lease','graph_build_proxy','ensure_graph_build_jobs_schema','_create_graph_build_jobs_schema','create_graph_build_job'}
def namespace(pool,gate,entered):
 nodes=[copy.deepcopy(n) for n in ast.parse(S.read_text()).body if getattr(n,'name',None) in NAMES];assert {n.name for n in nodes}==NAMES
 for n in nodes:
  if n.name!='graph_registry_lease':n.decorator_list=[]
 future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
 mod=ast.fix_missing_locations(ast.Module(body=[future,*nodes],type_ignores=[]))
 async def scope(*a,**k):entered.set();await gate.wait();return ('repo','storage','repo','generation')
 async def noop(*a,**k):return {'ok':True}
 @asynccontextmanager
 async def app_connection(*a,**k):yield pool.connection()
 ns={'asyncio':asyncio,'asynccontextmanager':asynccontextmanager,'pool_admin':pool,'uuid4':uuid4,'json':json,'Header':lambda **k:None,'require_project_scope':lambda x:x,'require_graph_repo_scope':scope,'graph_build_materializes_generation':lambda b:True,'_set_graph_generation_hold_under_lease':noop,'execute_graph_build_request':noop,'require_graph_build_success':lambda body,result,**kwargs:result,'acquire_scoped':app_connection,'schedule_graph_build_job':lambda *a:None,'JSONResponse':lambda **k:k}
 exec(compile(mod,str(S),'exec'),ns);return ns
class Body:
 def __init__(self,sync):self.sync=sync;self.async_job=False;self.full=True;self.embed=False;self.repo='repo'
 def model_copy(self,**kwargs):return self
 def model_dump(self):return {'full':True,'sync':self.sync,'embed':False,'repo':self.repo}
class Pool:
 def __init__(self):self.slots=asyncio.Semaphore(4);self.registry=asyncio.Lock();self.all_four=asyncio.Event();self.active=0;self.nested_wait=asyncio.Event()
 def connection(self):
  p=self
  class Connection:
   async def fetchval(self,sql,*args):
    if 'pg_advisory_unlock' in sql:p.registry.release();return True
    if 'pg_advisory_lock' in sql:await p.registry.acquire();return None
    raise AssertionError(sql)
   async def execute(self,*a):return 'OK'
   def terminate(self):raise AssertionError('unexpected terminate')
  return Connection()
 @asynccontextmanager
 async def acquire(self):
  if self.active==4:self.nested_wait.set()
  await self.slots.acquire();self.active+=1
  if self.active==4:self.all_four.set()
  try:yield self.connection()
  finally:self.active-=1;self.slots.release()
class GraphPoolControls(unittest.IsolatedAsyncioTestCase):
 async def exercise(self,sync):
  pool=Pool();gate=asyncio.Event();entered=asyncio.Event();ns=namespace(pool,gate,entered);tasks=[]
  try:
   tasks.append(asyncio.create_task(ns['graph_build_proxy'](Body(sync),'project')))
   await asyncio.wait_for(entered.wait(),1)
   tasks.extend(asyncio.create_task(ns['graph_build_proxy'](Body(sync),'project')) for _ in range(3))
   await asyncio.wait_for(pool.all_four.wait(),1);gate.set()
   done,pending=await asyncio.wait(tasks,timeout=0.2)
   return len(done),len(pending),pool.active
  finally:
   for task in tasks:
    if not task.done():task.cancel()
   await asyncio.gather(*tasks,return_exceptions=True)
   self.assertEqual(pool.active,0);self.assertFalse(pool.registry.locked())
 async def test_async_enqueue_completes_with_four_registry_requests(self):
  done,pending,active=await self.exercise(False)
  self.assertEqual((done,pending,active),(4,0,0),'four held admin slots deadlock owner second-acquire against three registry-lock waiters')
 async def test_sync_control_releases_all_four_registry_requests(self):
  self.assertEqual(await self.exercise(True),(4,0,0))

if __name__=="__main__":unittest.main()
