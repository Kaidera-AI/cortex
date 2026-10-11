"""Execute real controller finally blocks with a declared control-only pass flag."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

HERE=Path(__file__).resolve().parent

class ControllerReceiptGuards(unittest.TestCase):
    def tail(self,filename,pending,run):
        tree=ast.parse((HERE/filename).read_text())
        node=next(n for n in tree.body if isinstance(n,ast.Try) and n.finalbody)
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'receipt.json'
            ns={'pending':pending,'run':run,'checked':run,'owned':lambda *a:None,
                'cleanup':lambda:None,'cleanup_errors':[],'passed':True,
                'TARGET':target,'PHASE':'fixture-only','TREE':'fixture','NONCE':'fixture',
                'DB':'db','NAME':'name','PG':'fixture','PY':'fixture','IMAGE':'fixture',
                'results':[],'SOURCE':{},'TOOLS':{},'removed':False,
                'hashlib':hashlib,'Path':Path,'json':json,'__file__':str(HERE/filename)}
            caught=None
            try:exec(compile(ast.Module(body=node.finalbody,type_ignores=[]),filename,'exec'),ns)
            except Exception as error:caught=error
            self.assertTrue(target.exists(),'cleanup/inventory failure must retain its receipt')
            value=json.loads(target.read_text())
            self.assertFalse(value['passed'],'unverified cleanup must never retain a GREEN field')
            self.assertIsNotNone(caught,'unverified cleanup must reject the return')
            return value

    def test_pg_pending_cleanup_cannot_return_green(self):
        value=self.tail('run-auth-pg.py',[('pod','pod')],lambda a:None)
        self.assertFalse(value['stack_removed'])
        self.assertTrue(value['pending'])

    def test_consumer_foreign_name_cannot_return_green(self):
        value=self.tail('run-auth-receipts.py',True,lambda a:types.SimpleNamespace(returncode=2))
        self.assertFalse(value['container_removed'])

    def test_consumer_inventory_timeout_retains_failed_receipt(self):
        def timeout(args):raise subprocess.TimeoutExpired(args,120)
        value=self.tail('run-auth-receipts.py',True,timeout)
        self.assertIn('TimeoutExpired',value['final_inventory_error'])

if __name__=='__main__':unittest.main()
