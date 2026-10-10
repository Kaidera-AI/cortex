"""C07 copied-source semantic faults with the fixed active test-body classifier."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import psycopg

NEXT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NEXT/'tests'))
from test_receipts import classify, report, suite

CONSUMER = 'src/cortex_core/modules/consumer.py'
SQL = 'schema/coordination/003-module-consumers.sql'
TARGET = 'tests/module_consumer/common.py'
GRAPH = 'src/cortex_core/modules/graph/consumer_adapter.py'
MANIFEST = 'schema/manifest.json'
DIRECTORY = NEXT/'tests/module_consumer'

RECIPES = [
    ('after-core crash hook removed',CONSUMER,
     'if self.after_checkpoint is not None:', 'if False:',
     'test_apply_checkpoint.ApplyCheckpoint.test_crash_after_core_outcome_is_duplicate_safe'),
    ('foreign scope accepted',CONSUMER,
     "or UUID(env['project_id']) != self.project_id", 'or False',
     'test_poison.Poison.test_untrusted_aggregate_or_scope_refuses_cycle'),
    ('payload digest ignored',CONSUMER,
     "or hashlib.sha256(event.payload).hexdigest() != env['payload_sha256']", 'or False',
     'test_poison.Poison.test_poison_blocks_only_its_aggregate'),
    ('full page jumps global head',CONSUMER,
     'cursor = page.events[-1].cursor if len(page.events) == limit else page.head',
     'cursor = page.head',
     'test_apply_checkpoint.ApplyCheckpoint.test_full_project_page_does_not_jump_global_head'),
    ('poisoned aggregate not held',CONSUMER,
     "if not repairing and self._call('c07_poisoned(%s,%s,%s)',",
     "if False and self._call('c07_poisoned(%s,%s,%s)',",
     'test_poison.Poison.test_held_successors_replay_in_order_after_repair'),
    ('sparse gap promoted',SQL,
     "IF candidate.outcome NOT IN ('applied','stale') THEN EXIT; END IF;",
     'IF false THEN EXIT; END IF;',
     'test_poison.Poison.test_peer_aggregate_progress_does_not_claim_complete_cursor'),
    ('quarantine omitted',SQL,
     "ELSIF p_outcome='poison' THEN", 'ELSIF false THEN',
     'test_poison.Poison.test_poison_blocks_only_its_aggregate'),
    ('repair resolution omitted',SQL,
     'UPDATE coordination.quarantine SET resolved_at=clock_timestamp()',
     'UPDATE coordination.quarantine SET resolved_at=NULL',
     'test_poison.Poison.test_held_successors_replay_in_order_after_repair'),
    ('stale target revision overwritten',TARGET,
     "if existing[0] > env['aggregate_revision']:", 'if False:',
     'test_apply_checkpoint.ApplyCheckpoint.test_stale_upsert_cannot_resurrect_tombstone'),
    ('equal revision conflict ignored',TARGET,
     "if existing[1] != UUID(env['event_id']) or existing[2] != env['payload_sha256']:",
     'if False:',
     'test_apply_checkpoint.ApplyCheckpoint.test_equal_revision_different_digest_is_quarantined'),
    ('target tombstone removed',TARGET,
     "env['payload_sha256'],env['tombstone'],event.payload",
     "env['payload_sha256'],False,event.payload",
     'test_apply_checkpoint.ApplyCheckpoint.test_stale_upsert_cannot_resurrect_tombstone'),
    ('graph bytes re-encoded',GRAPH,
     'return GraphFactBinding(payload, source_kind, revision, extractor)',
     "return GraphFactBinding(json.dumps(fact).encode(), source_kind, revision, extractor)",
     'test_graph_codec.GraphCodec.test_graph_fact_bytes_are_verbatim'),
    ('new table forced RLS removed',SQL,
     "ALTER TABLE coordination.%I FORCE ROW LEVEL SECURITY",
     "ALTER TABLE coordination.%I NO FORCE ROW LEVEL SECURITY",
     'test_manifest.Manifest.test_new_tables_force_rls_and_request_has_no_direct_write'),
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_receipt(result):
    value = report(result)
    return value if value is not None else {'tests_run': 0, 'failures': [], 'errors': [{'id':'missing_receipt'}]}


def checkpoint():
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True) as connection:
        connection.execute('CHECKPOINT')


def exact_suite(name):
    """Isolate a declared fault boundary when unrelated repair cannot run."""
    script = '''import json,sys,unittest
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
from test_receipts import AssertionResult,MARKER
name=sys.argv[3]
tests=unittest.defaultTestLoader.loadTestsFromName(name)
assert tests.countTestCases()==1,'selected test ID must resolve exactly once'
result=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(tests)
print(MARKER+json.dumps({'tests_run':result.testsRun,'failures':result.assertions,
 'errors':[{'id':test.id(),'traceback':traceback} for test,traceback in result.errors]}),flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''
    return subprocess.run([sys.executable,'-c',script,str(NEXT/'tests'),str(DIRECTORY),name],
                          capture_output=True,text=True)


def run():
    paths = sorted({path for _,path,_,_,_ in RECIPES} | {MANIFEST})
    originals = {path:(NEXT/path).read_bytes() for path in paths}
    original_sha = {path:digest(data) for path,data in originals.items()}
    assert len({label for label,*_ in RECIPES}) == len(RECIPES)
    for label,path,before,after,expected in RECIPES:
        assert before != after and originals[path].decode().count(before) == 1, label
        assert expected.startswith('test_')
    baseline = suite(DIRECTORY)
    baseline_receipt = check_receipt(baseline)
    assert baseline.returncode == 0 and baseline_receipt['tests_run'] == 22
    assert not baseline_receipt['failures'] and not baseline_receipt['errors']
    print(json.dumps({'baseline':baseline_receipt,'source_sha256':original_sha}),flush=True)
    rows = []
    try:
        for label,path,before,after,expected in RECIPES:
            checkpoint()
            source = originals[path].decode().replace(before,after)
            try:
                (NEXT/path).write_text(source)
                if path == SQL:
                    manifest = json.loads(originals[MANIFEST])
                    manifest['migrations'][-1]['sha256'] = digest((NEXT/SQL).read_bytes())
                    (NEXT/MANIFEST).write_text(json.dumps(manifest,indent=2)+'\n')
                effective = {name:digest((NEXT/name).read_bytes()) for name in paths}
                result = (exact_suite(expected) if label == 'sparse gap promoted'
                          else suite(DIRECTORY))
                receipt = check_receipt(result)
                status = classify(result,{expected})
                failed = {row['id'].split(' (')[0] for row in receipt['failures']}
                if (expected not in failed or receipt['errors'] or
                    any(row['phase'] != 'test' or not row['is_assertion'] or
                        row['phase_attribution'] != 'traceback+active-unittest-v2'
                        for row in receipt['failures'])):
                    status = 'inconclusive'
                row = {'mutation':label,'source':path,'expected_test':expected,
                       'status':status,'original_source_sha256':original_sha,
                       'effective_source_sha256':effective,'exit_code':result.returncode,
                       'receipt':receipt,'stdout':result.stdout,'stderr':result.stderr}
                rows.append(row)
                print(json.dumps(row),flush=True)
            finally:
                for name,data in originals.items():
                    (NEXT/name).write_bytes(data)
    finally:
        for name,data in originals.items():
            (NEXT/name).write_bytes(data)
    checkpoint()
    final = suite(DIRECTORY)
    restored = check_receipt(final)
    final_sha = {name:digest((NEXT/name).read_bytes()) for name in paths}
    summary = {'mutants':len(RECIPES),'killed':sum(row['status']=='killed' for row in rows),
               'survivors':[row['mutation'] for row in rows if row['status']=='survived'],
               'inconclusive':[row['mutation'] for row in rows if row['status']=='inconclusive'],
               'restored_source_sha256':final_sha,'restored_receipt':restored}
    print(json.dumps(summary),flush=True)
    assert len(rows)==len(RECIPES) and summary['killed']==len(RECIPES)
    assert final_sha==original_sha and final.returncode==0 and restored['tests_run']==22
    assert not restored['failures'] and not restored['errors']


if __name__ == '__main__':
    run()
