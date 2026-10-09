"""Actual controller finally-block guards, not simulated application success."""
import ast
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from test_lifecycle_pattern import Engine

HERE = Path(__file__).resolve().parent


class ControllerGuards(unittest.TestCase):
    def tail(self, kind):
        from replay_lifecycle import Lifecycle
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); wt=root/'.worktrees/task'; out=root/'receipts'
            (wt/'next').mkdir(parents=True); (wt/'tmp/wheels').mkdir(parents=True)
            (wt/'tmp/legacy-schema.sql').write_text('source-fixture-only')
            graph=wt/'packages/containers/graph-worker'; graph.mkdir(parents=True)
            out.mkdir(); (out/'test_receipts.py').write_text('# fixture metadata only')
            filename='safe-run-adoption.py' if kind=='c03' else 'safe-run-graph.py'
            tree=ast.parse((HERE/filename).read_text())
            node=next(n for n in tree.body if isinstance(n,ast.Try) and n.finalbody)
            code=compile(ast.Module(body=node.finalbody,type_ignores=[]),filename,'exec')
            engine=Engine(); stack=Lifecycle(root,engine); stack.acquire()
            name='kaidera-test-fixture-1'
            stack.create('container',name,['podman','run','--name',name]+stack.labels())
            engine.fail_remove=3  # Both bounded cleanup attempts fail.
            class Shell:
                def run(self,args,**kwargs): return engine(args)
                def check_output(self,args,**kwargs): return 'fixture-head'
            ns={'life':stack,'subprocess':Shell(),'json':json,'hashlib':hashlib,
                'TREE':'fixture-head','HEAD':'fixture-head','wt':wt,'WT':wt,'GRAPH':graph,
                'out':out,'OUT':out,'phase':'tail-fixture','PHASE':'tail-fixture',
                'TARGET':out/'tail-fixture.json','pod':name,'db':name,'driver':name,
                'pg':'fixture','py':'fixture','IMAGE':'fixture','NAME':name,'SOURCE':{},
                'TOOLS':{},'passed':True,'results':[]}
            # Only the real finally block executes; no PG/model suite is invented.
            caught=None; output=io.StringIO()
            with contextlib.redirect_stdout(output):
                try: exec(code,ns)
                except Exception as error: caught=error
            record=json.loads((out/'tail-fixture.json').read_text())
            self.assertIsInstance(caught,RuntimeError,'cleanup failure must reject successful return')
            self.assertFalse(record['passed'],'cleanup failure must never retain a GREEN field')
            self.assertFalse(stack.cleanup_verified)
            self.assertTrue(engine.rows)
            self.assertIsNone(stack.password)
            self.assertIsNone(stack.lock)
            self.assertNotIn('"removed": true',output.getvalue())

    def test_c03_cleanup_failure_cannot_return_green(self): self.tail('c03')

    def test_graph_cleanup_failure_cannot_return_green(self): self.tail('graph')

    def test_direct_podman_control_calls_are_bounded(self):
        for filename in ('safe-run-adoption.py','safe-run-graph.py','run-safety.py'):
            with self.subTest(filename=filename):
                nodes=ast.walk(ast.parse((HERE/filename).read_text()))
                for n in nodes:
                    if not isinstance(n,ast.Call) or not isinstance(n.func,ast.Attribute): continue
                    if not isinstance(n.func.value,ast.Name) or n.func.value.id!='subprocess': continue
                    if n.func.attr not in ('run','check_output') or not n.args: continue
                    a=n.args[0]
                    if isinstance(a,ast.List) and a.elts and isinstance(a.elts[0],ast.Constant) and a.elts[0].value=='podman':
                        self.assertIn('timeout',[k.arg for k in n.keywords],filename+':'+str(n.lineno))

    def test_tool_input_binding_precedes_resource_launch(self):
        for filename in ('safe-run-adoption.py','safe-run-graph.py'):
            with self.subTest(filename=filename):
                s=(HERE/filename).read_text()
                self.assertIn('TOOLS =',s)
                self.assertLess(s.index('TOOLS ='),s.index('life.acquire()'))
                self.assertIn("'tool_input_sha256':TOOLS",s)


if __name__=='__main__': unittest.main()
