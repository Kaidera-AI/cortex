"""One explicit suite file per interpreter; enforce every collected test phase."""
import argparse
import ast
from collections import Counter
import hashlib
import functools
import time
import faulthandler
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
import types

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('shared_next_receipt',
                                            Path(__file__).with_name('pytest_next_offline.py'))
SHARED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHARED)


def declared_cases(module):
    """Honor the module's standard unittest load_tests contract without selectors."""
    def flatten(suite):
        for case in suite:
            if isinstance(case, unittest.TestSuite):
                yield from flatten(case)
            elif isinstance(case, unittest.TestCase):
                yield case
            else:
                raise TypeError('declared unittest suite contains a non-TestCase')
    return list(flatten(unittest.TestLoader().loadTestsFromModule(module)))


class Gate:
    def __init__(self, output, skip, retired=(), excluded=()):
        self.output, self.skip = output, skip
        self.retired = list(retired)
        self.excluded = list(excluded)
        self.assertions = {}
        self.exceptions = {}
        self.expected = ()
        self.bridge_stages = {}
        self.packet = dict(expected=[], selected=[], deselected=[], reports=[])

    def bridge_diagnostics(self, session):
        if self.packet.get('suite')!='next/tests/release/test_original_member_bridge.py':return
        from cortex_core.release import aggregate,trust,custody
        from cortex_core.runtime import signed_member
        module=session.items[0].module
        def wrap(owner,name):
            original=getattr(owner,name);label=owner.__name__+'.'+name
            @functools.wraps(original)
            def measured(*args,**kwargs):
                started=time.perf_counter()
                try:return original(*args,**kwargs)
                finally:
                    row=self.bridge_stages.setdefault(label,dict(calls=0,seconds=0.0))
                    row['calls']+=1;row['seconds']+=time.perf_counter()-started
            setattr(owner,name,measured)
        for owner,names in [(module.Fixture,['setUp','current','transport','backend']),
                            (module.Bridge,['installer']),
                            (trust,['_verify_original']),
                            (aggregate,['_validate_manifest','_measure_members','_check_pairs']),
                            (custody.RootReader,['aggregate_begin','aggregate_finish','_aggregate_inventory_finish','close']),
                            (signed_member,['request_member'])]:
            for name in names:wrap(owner,name)
        print('U3_BRIDGE_SELECTION '+json.dumps(self.packet['selected']),flush=True)
        # Samples the active stack; the parent still enforces the sole180s cap.
        faulthandler.dump_traceback_later(60,repeat=True)

    def pytest_runtest_logstart(self,nodeid,location):
        if self.packet.get('suite')=='next/tests/release/test_original_member_bridge.py':
            self.bridge_stages={}
            print('U3_BRIDGE_START '+nodeid,flush=True)

    def pytest_collection_finish(self, session):
        collected = [item.nodeid for item in session.items]
        nodes = {row['nodeid'] for row in self.retired}
        excluded = {row['nodeid'] for row in self.excluded}
        if nodes & excluded or len(excluded)!=len(self.excluded) or not all(Counter(collected)[node]==1 for node in excluded):
            raise ValueError('method_exclusion_collection')
        if not all(Counter(collected)[node] == 1 for node in nodes):
            raise ValueError('retirement_collection')
        self.packet.update(collected=collected, retired=self.retired,excluded_methods=self.excluded)
        fixtures={row['nodeid'] for row in self.excluded if row.get('evidence',{}).get('kind')!='unbuilt_unit'}
        session.items[:] = [item for item in session.items if item.nodeid not in nodes|fixtures]
        self.expected = tuple(item.nodeid for item in session.items)
        self.packet.update(expected=list(self.expected), selected=list(self.expected))
        self.bridge_diagnostics(session)
        if self.skip and session.items:
            import pytest
            session.items[0].add_marker(pytest.mark.skip(reason='next offline hosted forced-skip control'))

    def pytest_deselected(self, items):
        self.packet['deselected'].extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report):
        self.packet['reports'].append(dict(nodeid=report.nodeid, when=report.when,
            outcome=report.outcome, duration_seconds=report.duration, wasxfail=hasattr(report, 'wasxfail'),
            assertion=self.assertions.get((report.nodeid,report.when),False),
            exception_type=self.exceptions.get((report.nodeid,report.when),''),
            detail=str(report.longrepr) if report.failed or report.skipped else ''))

        if self.packet.get('suite')=='next/tests/release/test_original_member_bridge.py':
            print('U3_BRIDGE_PHASE '+json.dumps(dict(nodeid=report.nodeid,phase=report.when,seconds=report.duration)),flush=True)
            if report.when=='teardown':
                record=dict(nodeid=report.nodeid,stages=self.bridge_stages)
                self.packet.setdefault('bridge_stages',[]).append(record)
                print('U3_BRIDGE_STAGES '+json.dumps(record),flush=True)

    def pytest_runtest_makereport(self,item,call):
        outcome=yield
        outcome.get_result()
        if call.excinfo is not None:
            error=type(call.excinfo.value)
            self.exceptions[(item.nodeid,call.when)]=error.__module__+'.'+error.__qualname__
        self.assertions[(item.nodeid,call.when)] = bool(call.excinfo is not None
            and isinstance(call.excinfo.value,AssertionError))

    def pytest_sessionfinish(self, session, exitstatus):
        if self.packet.get('suite')=='next/tests/release/test_original_member_bridge.py':faulthandler.cancel_dump_traceback_later()
        self.packet['pytest_exit'] = int(exitstatus)
        self.packet['declared_unavailable']=SHARED.declaration_observations(self.packet,self.excluded)
        self.packet['gate_exit']=0
        self.packet['gate_exit']=0 if SHARED.receipt_valid(self.packet,0,retired=self.retired,excluded=self.excluded) else 1
        self.output.write_text(json.dumps(self.packet, indent=2) + '\n')
        session.exitstatus = self.packet['gate_exit']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--temporary', type=Path, required=True)
    parser.add_argument('--force-skip', action='store_true')
    parser.add_argument('--retired-json', default='[]', help='host-validated exact retirement witnesses')
    parser.add_argument('--excluded-json',default='[]',help='host-validated exact unavailable fixture nodes')
    parser.add_argument('--nodes-json',default='[]',help='named deciding proof nodes; all-suite execution remains separate')
    parser.add_argument('--observe-product',help='proof product locus relative to this owned source copy')
    args = parser.parse_args()
    args.temporary.mkdir(mode=0o700)
    suite = ROOT / args.suite
    # Historical fixture imports are bare names; each file still has a fresh process.
    directories = sorted({str(p.parent) for p in (ROOT / 'next/tests').rglob('*.py')})
    sys.path[:0] = [str(ROOT / 'next/src'), str(suite.parent), *directories]
    os.environ['PYTHONPATH'] = os.pathsep.join([str(ROOT / 'next/src'), *directories])
    sys.argv = [str(suite)]
    if args.suite in ('next/tests/release/adoption_controls/test_root_admission_identity.py',
                      'next/tests/release/adoption_controls/test_root_artifact_semantics.py'):
        # These standalone review controls accept source/output roots positionally.
        sys.argv += [str(ROOT), str(args.temporary)]
    if args.suite=='next/tests/c11_availability/test_deadline_cleanup.py':
        os.environ['TEST_PROBE_DATABASE_URL']='postgresql://offline@127.0.0.1:1/offline'
    os.chdir(ROOT)
    import pytest
    requested=json.loads(args.nodes_json)
    if not isinstance(requested,list) or any(not isinstance(n,str) or not n.startswith(args.suite+'::') for n in requested) or len(requested)!=len(set(requested)):
        raise ValueError('named_proof_collection')
    gate = Gate(args.output, args.force_skip, retired=json.loads(args.retired_json),excluded=json.loads(args.excluded_json))
    # Wrap the builder: unittest supplies deferred exceptions during report creation.
    Gate.pytest_runtest_makereport=pytest.hookimpl(tryfirst=True,hookwrapper=True)(Gate.pytest_runtest_makereport)
    gate.packet.update(suite=args.suite, source_sha256=hashlib.sha256(suite.read_bytes()).hexdigest(),
                       uid=os.getuid(), python=sys.version, force_skip=args.force_skip,
                       declared_unittest_outcomes=[])
    observed=None
    if args.observe_product:
        product=(ROOT/args.observe_product).resolve()
        if not product.is_relative_to(ROOT/'next/src') or not product.is_file():raise ValueError('product_locus')
        raw=product.read_bytes();compiled={};seen={}
        def signature(code):
            def constant(value):
                if isinstance(value,types.CodeType):return signature(value)
                if isinstance(value,tuple):return tuple(constant(v) for v in value)
                if isinstance(value,frozenset):return ('frozenset',tuple(sorted(map(repr,value))))
                return value
            return (code.co_code,tuple(constant(v) for v in code.co_consts),code.co_names,code.co_varnames,
                    code.co_freevars,code.co_cellvars,code.co_flags,code.co_exceptiontable)
        def fingerprint(code):return hashlib.sha256(repr(signature(code)).encode()).hexdigest()
        def visit(code):
            compiled.setdefault((code.co_qualname,code.co_firstlineno),set()).add(fingerprint(code))
            for value in code.co_consts:
                if isinstance(value,types.CodeType):visit(value)
        visit(compile(raw,str(product),'exec'))
        def observe(frame,event,arg):
            if event=='call' and frame.f_code.co_filename==str(product):
                code=frame.f_code;key=(code.co_qualname,code.co_firstlineno)
                seen[key]={'qualname':key[0],'line':key[1],'code_sha256':fingerprint(code),
                           'matches_current_compile':fingerprint(code) in compiled.get(key,set())}
        sys.setprofile(observe)
        observed=(product,raw,seen)

    class DeclaredItem(pytest.Item):
        def __init__(self, *arguments, case, **keywords):
            super().__init__(*arguments, **keywords)
            self.case = case

        def runtest(self):
            result = unittest.TestResult()
            self.case.run(result)
            outcome = dict(nodeid=self.nodeid, unittest_id=self.case.id(), tests_run=result.testsRun,
                failures=[trace for _, trace in result.failures], errors=[trace for _, trace in result.errors],
                skipped=[reason for _, reason in result.skipped],
                expected_failures=[trace for _, trace in result.expectedFailures],
                unexpected_successes=[case.id() for case in result.unexpectedSuccesses])
            gate.packet['declared_unittest_outcomes'].append(outcome)
            if result.skipped:
                pytest.skip('; '.join(outcome['skipped']))
            if result.expectedFailures:
                pytest.xfail('declared unittest expected failure')
            if result.testsRun != 1 or not result.wasSuccessful() or result.unexpectedSuccesses:
                raise AssertionError(json.dumps(outcome, indent=2))

        def reportinfo(self):
            return suite, None, self.name

    class DeclaredModule(pytest.Module):
        def collect(self):
            cases = declared_cases(self.obj)
            identities = [case.id() for case in cases]
            if len(set(identities)) != len(identities):
                raise ValueError('duplicate declared unittest test identity')
            gate.packet['declared_unittest_ids'] = identities
            for case in cases:
                yield DeclaredItem.from_parent(self, name=case.id(), case=case)

    class DeclaredProtocol:
        def pytest_pycollect_makemodule(self, module_path, parent):
            if module_path.resolve() == suite.resolve() and any(
                    isinstance(node, ast.FunctionDef) and node.name == 'load_tests'
                    for node in ast.parse(suite.read_text()).body):
                return DeclaredModule.from_parent(parent, path=module_path)

    result = pytest.main(['-q', '-rP', '--tb=short', '--strict-markers', '-c', '/dev/null',
                          '--rootdir=' + str(ROOT), '-o', 'cache_dir=' + str(args.temporary / 'cache'),
                          '--basetemp=' + str(args.temporary / 'pytest'), *(requested or [args.suite])],
                         plugins=[gate, DeclaredProtocol()])
    if observed:
        sys.setprofile(None);product,raw,seen=observed
        gate.packet['observed_product']=dict(path=args.observe_product,sha256=hashlib.sha256(raw).hexdigest(),
            source_unchanged=product.read_bytes()==raw,calls=list(seen.values()),
            loaded_source_matches=all(row['matches_current_compile'] for row in seen.values()))
        args.output.write_text(json.dumps(gate.packet,indent=2)+'\n')
    return int(result)


if __name__ == '__main__':
    raise SystemExit(main())
