"""Actual pinned receipt class/classifier; synthetic unittest-only causal controls."""
import ast,copy,io,json,unittest
from pathlib import Path
from types import SimpleNamespace
W=Path(__file__).resolve().parents[3];S=W/'next/tests/test_receipts.py'
def actual():
 wanted={'AssertionResult','report','classify'};nodes=[copy.deepcopy(n) for n in ast.parse(S.read_text()).body if getattr(n,'name',None) in wanted];assert {n.name for n in nodes}==wanted
 ns={'unittest':unittest,'json':json,'MARKER':'CORTEX_TEST_RESULT='};exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(S),'exec'),ns);return ns
def classified(case):
 ns=actual();stream=io.StringIO();r=unittest.TextTestRunner(stream=stream,resultclass=ns['AssertionResult']).run(unittest.TestSuite([case('test_expected')]))
 value={'tests_run':r.testsRun,'failures':r.assertions,'errors':[{'id':t.id(),'traceback':s} for t,s in r.errors]};assert r.testsRun==1 and len(r.assertions)==1 and not r.errors
 output=SimpleNamespace(returncode=1,stdout='CORTEX_TEST_RESULT='+json.dumps(value)+'\n')
 return ns['classify'](output,{case.__module__+'.'+case.__qualname__+'.test_expected'}),value
class SetupSubTest(unittest.TestCase):
 def setUp(self):
  with self.subTest(origin='setup'):self.test_expected()
 def test_expected(self):self.fail('declared setup reuse failure')
class CleanupSubTest(unittest.TestCase):
 def setUp(self):self.in_cleanup=False;self.addCleanup(self.fixture_cleanup)
 def fixture_cleanup(self):
  self.in_cleanup=True
  with self.subTest(origin='cleanup'):self.test_expected()
 def test_expected(self):
  if self.in_cleanup:self.fail('declared cleanup reuse failure')
class BodySubTest(unittest.TestCase):
 def test_expected(self):
  with self.subTest(origin='body'):self.fail('declared genuine body failure')
def synchronous(phase):
 def fixture(self):
  self.in_fixture=True
  with self.subTest(origin=phase):self.test_expected()
 def setup(self):
  self.in_fixture=False
  if phase=='cleanup':self.addCleanup(fixture,self)
 class Case(unittest.TestCase):
  def test_expected(self):
   if getattr(self,'in_fixture',False):self.fail('synthetic fixture subtest reuse')
 Case.setUp=fixture if phase=='setUp' else setup
 if phase=='tearDown':Case.tearDown=fixture
 return Case

def asynchronous(phase):
 async def fixture(self):
  self.in_fixture=True
  with self.subTest(origin=phase):await self.test_expected()
 async def setup(self):
  self.in_fixture=False
  if phase=='cleanup':self.addAsyncCleanup(fixture,self)
 class Case(unittest.IsolatedAsyncioTestCase):
  async def test_expected(self):
   if getattr(self,'in_fixture',False):self.fail('synthetic async fixture subtest reuse')
 Case.asyncSetUp=fixture if phase=='asyncSetUp' else setup
 if phase=='asyncTearDown':Case.asyncTearDown=fixture
 return Case

class AsyncBodySubTest(unittest.IsolatedAsyncioTestCase):
 async def helper(self):
  with self.subTest(origin='body-helper'):self.fail('genuine async subtest')
 async def test_expected(self):await self.helper()

# Only outer control tests are discovered; synthetic cases are constructed explicitly.
class ReceiptSubtestControls(unittest.TestCase):
 def test_setup_reused_method_subtest_remains_fixture(self):
  status,receipt=classified(SetupSubTest);self.assertEqual(status,'inconclusive',str(receipt['failures']))
 def test_cleanup_reused_method_subtest_remains_fixture(self):
  status,receipt=classified(CleanupSubTest);self.assertEqual(status,'inconclusive',str(receipt['failures']))
 def test_genuine_body_subtest_is_a_named_kill(self):
  status,receipt=classified(BodySubTest);self.assertEqual(status,'killed');self.assertEqual(receipt['failures'][0]['phase'],'test')
 def test_sync_fixture_subtests_are_inconclusive(self):
  for phase in ('setUp','tearDown','cleanup'):
   with self.subTest(phase=phase):
    status,receipt=classified(synchronous(phase))
    self.assertEqual(status,'inconclusive',str(receipt['failures']))
    self.assertEqual(receipt['failures'][0]['phase'],'fixture')
 def test_async_fixture_subtests_are_inconclusive(self):
  for phase in ('asyncSetUp','asyncTearDown','cleanup'):
   with self.subTest(phase=phase):
    status,receipt=classified(asynchronous(phase))
    self.assertEqual(status,'inconclusive',str(receipt['failures']))
    self.assertEqual(receipt['failures'][0]['phase'],'fixture')
 def test_genuine_async_body_helper_subtest_is_killed(self):
  status,receipt=classified(AsyncBodySubTest)
  self.assertEqual(status,'killed')
  self.assertEqual(receipt['failures'][0]['phase'],'test')
 def test_new_records_bind_active_phase_attribution(self):
  status,receipt=classified(BodySubTest)
  self.assertEqual(status,'killed')
  self.assertEqual(receipt['failures'][0].get('phase_attribution'),'traceback+active-unittest-v2')
 def test_legacy_subtest_without_active_context_is_inconclusive(self):
  ns=actual();target='synthetic.Case.test_expected'
  row={'id':target+" (origin='body')",'phase':'test','is_assertion':True,
       'exception_type':'builtins.AssertionError','traceback':'AssertionError: retained legacy synthetic'}
  result=SimpleNamespace(returncode=1,stdout=ns['MARKER']+json.dumps({'tests_run':1,'failures':[row],'errors':[]})+'\n')
  self.assertEqual(ns['classify'](result,{target}),'inconclusive')
  for value in (None,'foreign-v9'):
   with self.subTest(value=value):
    row['phase_attribution']=value
    result.stdout=ns['MARKER']+json.dumps({'tests_run':1,'failures':[row],'errors':[]})+'\n'
    self.assertEqual(ns['classify'](result,{target}),'inconclusive')
 def test_ordinary_legacy_body_record_remains_compatible(self):
  ns=actual();target='synthetic.Case.test_expected'
  row={'id':target,'phase':'test','is_assertion':True,'exception_type':'builtins.AssertionError',
       'traceback':'AssertionError: retained ordinary legacy'}
  result=SimpleNamespace(returncode=1,stdout=ns['MARKER']+json.dumps({'tests_run':1,'failures':[row],'errors':[]})+'\n')
  self.assertEqual(ns['classify'](result,{target}),'killed')
def load_tests(loader,tests,pattern):return loader.loadTestsFromTestCase(ReceiptSubtestControls)
