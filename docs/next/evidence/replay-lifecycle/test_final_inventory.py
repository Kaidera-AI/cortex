"""A final inventory timeout must leave a failed receipt before rejection."""
import ast
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_lifecycle_pattern import Engine

HERE=Path(__file__).resolve().parent


class FinalInventory(unittest.TestCase):
    def exercise(self,filename):
        from replay_lifecycle import Lifecycle
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); wt=root/'.worktrees/task'; out=root/'receipts'
            (wt/'next').mkdir(parents=True); (wt/'tmp/wheels').mkdir(parents=True)
            (wt/'tmp/legacy-schema.sql').write_text('fixture metadata')
            graph=wt/'packages/containers/graph-worker';graph.mkdir(parents=True)
            out.mkdir();(out/'test_receipts.py').write_text('# fixture classifier metadata')
            node=next(n for n in ast.parse((HERE/filename).read_text()).body if isinstance(n,ast.Try) and n.finalbody)
            engine=Engine();life=Lifecycle(root,engine);life.acquire()
            name='kaidera-test-fixture-1'
            life.create('container',name,['podman','run','--name',name]+life.labels())
            class Shell:
                def run(self,args,**kwargs):
                    raise subprocess.TimeoutExpired(args,30)
                def check_output(self,args,**kwargs):return 'fixture-head'
            target=out/'inventory-fixture.json'
            ns={'life':life,'subprocess':Shell(),'json':json,'hashlib':hashlib,
                'TREE':'fixture-head','HEAD':'fixture-head','wt':wt,'WT':wt,'GRAPH':graph,
                'out':out,'OUT':out,'phase':'inventory-fixture','PHASE':'inventory-fixture','TARGET':target,
                'pod':name,'db':name,'driver':name,'pg':'fixture','py':'fixture','IMAGE':'fixture','NAME':name,
                'SOURCE':{},'TOOLS':{},'passed':True,'results':[],'VARIANT':'safe','mutation_rows':[]}
            caught=None
            with contextlib.redirect_stdout(io.StringIO()):
                try:exec(compile(ast.Module(body=node.finalbody,type_ignores=[]),filename,'exec'),ns)
                except Exception as error:caught=error
            self.assertTrue(target.exists(),'final inventory timeout erased the attempted receipt')
            record=json.loads(target.read_text())
            self.assertFalse(record['passed'])
            self.assertIn('TimeoutExpired',record['final_inventory_error'])
            self.assertIsInstance(caught,RuntimeError)
            self.assertFalse(engine.rows)
            self.assertIsNone(life.lock)

    def test_c03_inventory_timeout_retains_failed_receipt(self):self.exercise('safe-run-adoption.py')
    def test_graph_inventory_timeout_retains_failed_receipt(self):self.exercise('safe-run-graph.py')
    def test_safety_inventory_timeout_retains_failed_receipt(self):self.exercise('run-safety.py')


if __name__=='__main__':unittest.main()
