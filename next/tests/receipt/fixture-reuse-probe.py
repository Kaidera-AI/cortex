import importlib.util,io,json,subprocess,unittest
from pathlib import Path
root=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('receipt_helper',root/'head/next/tests/test_receipts.py');helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
class AsyncSetupReuse(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=True
        await self.test_expected()
        self.fixture=False
    async def test_expected(self):
        if self.fixture:self.fail('setup reused target method before framework body')
class AsyncCleanupReuse(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=False
        self.addAsyncCleanup(self.cleanup)
    async def cleanup(self):
        self.fixture=True
        await self.test_expected()
    async def test_expected(self):
        if self.fixture:self.fail('async cleanup reused target after successful body')
class SyncSetupReuse(unittest.TestCase):
    def setUp(self):self.test_expected()
    def test_expected(self):self.fail('sync setup reused target')
class OrdinaryAsyncFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.fail('ordinary fixture')
    async def test_expected(self):pass
class AsyncBody(unittest.IsolatedAsyncioTestCase):
    async def test_expected(self):self.fail('real body')
rows=[]
for cls in (AsyncSetupReuse,AsyncCleanupReuse,SyncSetupReuse,OrdinaryAsyncFixture,AsyncBody):
    stream=io.StringIO();result=unittest.TextTestRunner(stream=stream,resultclass=helper.AssertionResult).run(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
    value={'tests_run':result.testsRun,'failures':result.assertions,'errors':[{'id':t.id(),'traceback':tb} for t,tb in result.errors]}
    target=cls('test_expected').id();process=subprocess.CompletedProcess([],1,helper.MARKER+json.dumps(value),stream.getvalue());actual=helper.classify(process,{target})
    rows.append({'case':cls.__name__,'target':target,'classified':actual,'expected':'killed' if cls is AsyncBody else 'inconclusive','receipt':value,'raw_output':stream.getvalue()})
(root/'fixture-reuse-probe.json').write_text(json.dumps(rows,indent=2)+'\n')
print(json.dumps([{'case':r['case'],'classified':r['classified'],'expected':r['expected']} for r in rows]),flush=True)
assert all(r['classified']==r['expected'] for r in rows),'async fixture assertions admitted as expected body kills'
