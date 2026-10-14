"""Bounded U13 host source fixtures only. No DB, Podman, HOME change or real key."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT/'tests/core_upgrade'
HEAD = ROOT/'tests/core_upgrade_head'
SIGNED = Path(__file__).resolve().parent
FILES = [(HOST/'test_coordinator.py','test_coordinator'),
         (HOST/'test_journal.py','test_journal'),
         (HOST/'test_operation_race.py','test_operation_race'),
         (HOST/'test_signed_pair.py','test_signed_pair'),
         (SIGNED/'test_signed_admission.py','test_signed_admission'),
         (HEAD/'test_current_coordinator_bridge.py','test_current_coordinator_bridge'),
         (HOST/'review_rollback_marker.py','review_rollback_marker')]

def code_fingerprint(code):
    """Hash code values, not marshal's reference/interning serialization choices."""
    def normal(value):
        if isinstance(value, types.CodeType):
            return {'code': {key: normal(getattr(value,key)) for key in
                ('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals',
                 'co_stacksize','co_flags','co_code','co_consts','co_names',
                 'co_varnames','co_filename','co_name','co_qualname','co_firstlineno',
                 'co_linetable','co_exceptiontable','co_freevars','co_cellvars')}}
        if isinstance(value,bytes): return {'bytes':value.hex()}
        if isinstance(value,tuple): return {'tuple':[normal(x) for x in value]}
        if isinstance(value,frozenset):
            return {'frozenset':sorted((normal(x) for x in value),
                                      key=lambda x:json.dumps(x,sort_keys=True))}
        if isinstance(value,complex): return {'complex':[value.real,value.imag]}
        if value is Ellipsis:return {'ellipsis':True}
        if value is None or isinstance(value,(str,int,float,bool)):return value
        raise TypeError('unsupported code constant type')
    raw=json.dumps(normal(code),sort_keys=True,separators=(',',':'),
                   allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()

def loaded_witness():
    paths = {str((ROOT/'src/cortex_core/upgrade.py').resolve()),
             str((ROOT/'src/cortex_core/migrations.py').resolve())}
    compiled = {}
    def codes(code):
        yield code
        for child in code.co_consts:
            if isinstance(child, types.CodeType): yield from codes(child)
    for path in paths:
        if Path(path).is_file():
            raw = Path(path).read_bytes()
            compiled[path] = (hashlib.sha256(raw).hexdigest(),
                {code_fingerprint(c)
                 for c in codes(compile(raw, path, 'exec'))})
    found = []
    seen = set()
    def capture(function):
        wrapped = getattr(function,'__wrapped__',None)
        if wrapped is not None: capture(wrapped)
        code = getattr(function, '__code__', None)
        if code is None or code.co_filename not in compiled: return
        identity = (code.co_filename, code.co_qualname,
                    code_fingerprint(code))
        if identity in seen: return
        seen.add(identity)
        source, expected = compiled[code.co_filename]
        found.append({'path':identity[0],'function':identity[1],
                      'source_sha256':source,'loaded_code_sha256':identity[2],
                      'matches_current_compile':identity[2] in expected})
    for module in tuple(sys.modules.values()):
        if module is None: continue
        for obj in tuple(vars(module).values()):
            if isinstance(obj, types.FunctionType): capture(obj)
            elif isinstance(obj, type) and obj.__module__ in {
                    'cortex_core.upgrade','cortex_core.migrations',
                    'cox_upgrade_operation_race','vera_pr83_exact_upgrade',
                    'vera_pr83_exact_upgrade_key'}:
                for member in vars(obj).values():
                    if isinstance(member,(staticmethod,classmethod)): member=member.__func__
                    capture(member)
    return found

class Result(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.body_failures=[]
    def addFailure(self,test,err):
        self.body_failures.append({'test':test.id(),'exception':err[0].__name__,
                                   'message':str(err[1])})
        super().addFailure(test,err)
    def addSubTest(self,test,subtest,err):
        if err and issubclass(err[0],test.failureException):
            self.body_failures.append({'test':subtest.id(),'exception':err[0].__name__,
                                       'message':str(err[1])})
        super().addSubTest(test,subtest,err)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--select',choices=('all','bridge'),default='all')
    parser.add_argument('--selector')
    parser.add_argument('--key-control',action='store_true')
    args=parser.parse_args()
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(ROOT/'src'))
    # Caller supplies task-specific private synthetic-case roots and explicit binary.
    cases=Path(os.environ['CORTEX_U13_CASE_ROOT']).resolve()
    signed_cases=Path(os.environ['SIGNED_CASE_ROOT']).resolve()
    binary=Path(os.environ['SIGNED_MINISIGN_BIN'])
    if (not cases.is_dir() or not signed_cases.is_relative_to(cases) or
            not signed_cases.is_dir() or not binary.is_absolute() or
            not binary.is_file() or not os.access(binary,os.X_OK)):
        raise RuntimeError('owned fixture roots or explicit minisign binary missing')
    def no_network(event,_args):
        if event in {'socket.connect','socket.getaddrinfo','socket.bind'}:
            raise RuntimeError('network is outside U13 host source fixtures')
    sys.addaudithook(no_network)
    output=io.StringIO()
    if args.key_control:
        class CurrentKeyPath(unittest.TestCase):
            def test_pinned_key_bytes_refuse_replacement(self):
                with contextlib.redirect_stdout(output):
                    runpy.run_path(str(HOST/'review_key_path.py'),run_name='__main__')
        result=unittest.TextTestRunner(stream=output,verbosity=2,resultclass=Result).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(CurrentKeyPath))
        packet={'kind':'key_path_control','tests_run':result.testsRun,
                'failures':result.body_failures,
                'errors':[{'test':t.id(),'traceback':raw} for t,raw in result.errors],
                'skips':[{'test':t.id(),'reason':reason} for t,reason in result.skipped],
                'successful':result.wasSuccessful(),'stdout':output.getvalue(),
                'loaded_witness':loaded_witness(),
                'native_qualified':False,'postgres_executed':False}
        print('CORTEX_U13_SOURCE_RESULT='+json.dumps(packet,sort_keys=True))
        return 0 if result.wasSuccessful() else 1
    files=FILES if args.select=='all' else [FILES[5]]
    suite=unittest.TestSuite()
    for path,name in files:
        if args.selector and not args.selector.startswith(name+'.'): continue
        spec=importlib.util.spec_from_file_location(name,path)
        module=importlib.util.module_from_spec(spec)
        sys.modules[name]=module
        spec.loader.exec_module(module)
        if args.selector:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(
                args.selector[len(name)+1:],module))
        else: suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    if suite.countTestCases()==0: raise RuntimeError('no selected host source case')
    result=unittest.TextTestRunner(stream=output,verbosity=2,resultclass=Result).run(suite)
    witness=loaded_witness()
    packet={'kind':'host_source_only','tests_run':result.testsRun,
            'failures':result.body_failures,
            'errors':[{'test':t.id(),'traceback':raw} for t,raw in result.errors],
            'skips':[{'test':t.id(),'reason':reason} for t,reason in result.skipped],
            'successful':result.wasSuccessful(),'stdout':output.getvalue(),
            'loaded_witness':witness,
            'minisign_binary':str(binary),
            'minisign_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
            'native_qualified':False,'postgres_executed':False}
    if any(not x['matches_current_compile'] for x in witness):
        raise RuntimeError('loaded source does not match actual current compile')
    print('CORTEX_U13_SOURCE_RESULT='+json.dumps(packet,sort_keys=True))
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__':
    raise SystemExit(main())
