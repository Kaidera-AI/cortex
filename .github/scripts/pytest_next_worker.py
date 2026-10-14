"""One explicit suite file per interpreter; enforce every collected test phase."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('unchanged_u13_outcome',
                                            Path(__file__).with_name('pytest_u13_worker.py'))
U13 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(U13)


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
    os.chdir(ROOT)
    import pytest
    gate = Gate(args.output, args.force_skip)
    gate.packet.update(suite=args.suite, source_sha256=hashlib.sha256(suite.read_bytes()).hexdigest(),
                       uid=os.getuid(), python=sys.version, force_skip=args.force_skip)
    result = pytest.main(['-q', '--tb=short', '--strict-markers', '-c', '/dev/null',
                          '--rootdir=' + str(ROOT), '-o', 'cache_dir=' + str(args.temporary / 'cache'),
                          '--basetemp=' + str(args.temporary / 'pytest'), args.suite], plugins=[gate])
    return int(result)


if __name__ == '__main__':
    raise SystemExit(main())
