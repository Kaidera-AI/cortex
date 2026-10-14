"""Bounded source-copy faults; exact byte restore and full focused rerun."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'src/cortex_core/api/c11a.py'
MUTANTS = {
    'probe_bypass': {
        'edits': [("        current = flight\n        try:\n", "        current = flight\n        return True\n        try:\n")],
        'methods': ['test_outage.AvailabilityAPI.test_owned_pg_outage_matrix_inflight_and_one_replay', 'test_controls.ControlTests.test_reconnects_after_success_failure_and_non_one_result']},
    'cached_ready': {
        'edits': [('if flight is None or flight.done():', 'if flight is None:'), ('flight.add_done_callback(discard)', 'pass')],
        'methods': ['test_controls.ControlTests.test_reconnects_after_success_failure_and_non_one_result']},
    'remove_single_flight': {
        'edits': [('if flight is None or flight.done():', 'if True:')],
        'methods': ['test_probe_bound.ProbeBoundTests.test_twenty_slow_callers_share_one_deadline_and_leave_no_thread']},
    'inflate_factory_deadlines': {
        'edits': [('connect_timeout_seconds=timeout_seconds,', 'connect_timeout_seconds=timeout_seconds * 10,'), ('int(timeout_seconds * 1000)', 'int(timeout_seconds * 10000)')],
        'methods': ['test_probe_bound.ProbeBoundTests.test_twenty_slow_callers_share_one_deadline_and_leave_no_thread', 'test_deadline_cleanup.SubsecondAndCloseTests.test_real_factory_async_connect_is_cancelled_at_outer_deadline']},
    'reject_subsecond': {
        'edits': [('or not 0.001 <= connect_timeout_seconds <= 10', 'or not 1 <= connect_timeout_seconds <= 10')],
        'methods': ['test_deadline_cleanup.SubsecondAndCloseTests.test_real_factory_connects_with_subsecond_budget']},
    'close_inside_flight': {
        'edits': [('cleanup = own(retire(connection))\n                cleanup.add_done_callback(observe_close)', 'await retire(connection)')],
        'methods': ['test_deadline_cleanup.SubsecondAndCloseTests.test_held_close_cannot_keep_success_inside_stale_flight']},
    'raw_exception_log': {
        'edits': [("LOGGER.warning('Core probe connection close failed')", "LOGGER.warning('Core probe connection close failed', exc_info=True)")],
        'methods': ['test_privacy.PrivacyTests.test_close_exception_never_reaches_formatted_log']},
    'shutdown_without_drain': {
        'edits': [('for task in tuple(closers):', 'for task in ():'), ('pending = {task for task in owned if not task.done()}', 'pending = set()')],
        'methods': ['test_shutdown.ShutdownTests.test_shutdown_cancels_held_close_and_blocks_new_admissions']},
    'remove_close_cancel': {
        'edits': [("if not done:\n                close_task.cancel()\n                LOGGER.warning", "if not done:\n                pass\n                LOGGER.warning")],
        'methods': ['test_deadline_cleanup.SubsecondAndCloseTests.test_held_close_cannot_keep_success_inside_stale_flight']},
}

def main():
    interpreter, out = sys.argv[1:3]
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    original=SOURCE.read_bytes();original_hash=hashlib.sha256(original).hexdigest()
    results=[]
    for name, spec in MUTANTS.items():
        row={'name':name,'methods':spec['methods'],'original_sha256':original_hash}
        try:
            text=original.decode()
            for before,after in spec['edits']:
                if text.count(before)!=1:raise RuntimeError('non-unique source mutation anchor: '+name)
                text=text.replace(before,after,1)
            SOURCE.write_text(text)
            row['mutated_sha256']=hashlib.sha256(SOURCE.read_bytes()).hexdigest()
            negative=out/(name+'.json')
            subprocess.run([interpreter,str(Path(__file__).with_name('run_l1.py')),'mutant',interpreter,str(negative),*spec['methods']],check=False,timeout=900)
            receipt=json.loads(negative.read_text());body=receipt['suites']['c11_availability']['body_receipt']
            failed={f['id'].split(' (')[0] for f in body['failures']}
            row['body_failures']=len(body['failures'])
            row['errors']=len(body['errors'])
            row['killed']=(receipt['exit_code']==1 and not body['errors'] and all(f['phase']=='test' and f['is_assertion'] for f in body['failures']) and bool(failed.intersection(spec['methods'])) and receipt['cleanup']=='PASS')
            if not row['killed']:raise RuntimeError('mutation has no conclusive deciding BODY kill: '+name)
        finally:
            SOURCE.write_bytes(original)
            row['restored_exact']=SOURCE.read_bytes()==original
            restore=out/(name+'-restore.json')
            subprocess.run([interpreter,str(Path(__file__).with_name('run_l1.py')),'focus',interpreter,str(restore)],check=False,timeout=900)
            rr=json.loads(restore.read_text());bb=rr['suites']['c11_availability']['body_receipt']
            row['restore_green']=rr['exit_code']==0 and bb['tests_run']==17 and not bb['failures'] and not bb['errors'] and rr['cleanup']=='PASS'
            results.append(row)
            (out/'SUMMARY.json').write_text(json.dumps(results,indent=2)+'\n')
            if not row['restored_exact'] or not row['restore_green']:raise RuntimeError('exact restore did not pass full focused oracle')
    print(json.dumps({'mutations':len(results),'killed':sum(r['killed'] for r in results),'exact_restore_full_green':sum(r['restored_exact'] and r['restore_green'] for r in results)}),flush=True)

if __name__=='__main__':main()
