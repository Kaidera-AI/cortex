"""One explicit suite file per interpreter; enforce every collected test phase."""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('unchanged_u13_outcome',
                                            Path(__file__).with_name('pytest_u13_worker.py'))
U13 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(U13)


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
    def __init__(self, output, skip):
        self.output, self.skip = output, skip
        self.expected = ()
        self.packet = dict(expected=[], selected=[], deselected=[], reports=[])

    def pytest_collection_finish(self, session):
        self.expected = tuple(item.nodeid for item in session.items)
        self.packet.update(expected=list(self.expected), selected=list(self.expected))
        if self.skip and session.items:
            import pytest
            session.items[0].add_marker(pytest.mark.skip(reason='next offline hosted forced-skip control'))

    def pytest_deselected(self, items):
        self.packet['deselected'].extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report):
        self.packet['reports'].append(dict(nodeid=report.nodeid, when=report.when,
            outcome=report.outcome, wasxfail=hasattr(report, 'wasxfail'),
            detail=str(report.longrepr) if report.failed or report.skipped else ''))

    def pytest_sessionfinish(self, session, exitstatus):
        self.packet['pytest_exit'] = int(exitstatus)
        self.packet['gate_exit'] = 0 if U13.valid(self.expected, self.packet) else 1
        self.output.write_text(json.dumps(self.packet, indent=2) + '\n')
        session.exitstatus = self.packet['gate_exit']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--temporary', type=Path, required=True)
    parser.add_argument('--force-skip', action='store_true')
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
    os.chdir(ROOT)
    import pytest
    gate = Gate(args.output, args.force_skip)
    gate.packet.update(suite=args.suite, source_sha256=hashlib.sha256(suite.read_bytes()).hexdigest(),
                       uid=os.getuid(), python=sys.version, force_skip=args.force_skip,
                       declared_unittest_outcomes=[])

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

    result = pytest.main(['-q', '--tb=short', '--strict-markers', '-c', '/dev/null',
                          '--rootdir=' + str(ROOT), '-o', 'cache_dir=' + str(args.temporary / 'cache'),
                          '--basetemp=' + str(args.temporary / 'pytest'), args.suite],
                         plugins=[gate, DeclaredProtocol()])
    return int(result)


if __name__ == '__main__':
    raise SystemExit(main())
