"""Explicit active contract inventory; retired phase checkpoint is never green."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT.parents[1]/'src')]
RETIRED={'test_native_matrix_helper_protocol.HelperProtocol.test_real_cli_unsupported_profile_reason_is_not_json_decode':
         'CONTROL EDIT Kai2026-10-12 05:01b: preimplementation-only unsupported-profile checkpoint; byte-identical history; replaced by phase-neutral v2.'}


def cases(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):yield from cases(item)
        else:yield item


def main():
    loader=unittest.TestLoader();active=[];retired=[]
    for path in sorted(ROOT.glob('test_*.py')):
        suite=loader.loadTestsFromName(path.stem)
        for case in cases(suite):
            if case.id() in RETIRED:retired.append({'id':case.id(),'reason':RETIRED[case.id()],'file_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            else:active.append(case)
    if {r['id'] for r in retired}!=set(RETIRED):raise RuntimeError('retirement inventory changed')
    print(json.dumps({'active_cases':len(active),'retired_phase_only':retired},sort_keys=True))
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(active))
    print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skips':len(result.skipped),'retired':retired},sort_keys=True))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__=='__main__':raise SystemExit(main())
