"""C05 semantic faults; only declared expected BODY assertions count as kills."""
import hashlib
import json
import os
from pathlib import Path
import sys

import psycopg

NEXT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(NEXT/'tests'))
from test_receipts import classify,report,suite
RECORDS='src/cortex_core/records.py'
JOBS='src/cortex_core/coordination.py'
FIXTURE='contracts/core-adapters-conformance.json'
DB_FIXTURE='tests/core_db_fixture.py'
MUTATOR='scripts/mutate_core_adapters.py'
MUTATIONS=[
 (RECORDS,'changed request digest ignored', [('row[0] != digest','False')],'records','test_records.RecordAdapters.test_changed_body_kind_record_or_operation_under_key_refuses'),
 (RECORDS,'tombstone exposed by default', [('(row[2] and not include_tombstone)','False')],'records','test_records.RecordAdapters.test_delete_is_revisioned_tombstone_and_does_not_erase_history'),
 (RECORDS,'delete stored as live revision', [("tombstone = operation == 'delete'",'tombstone = False')],'records','test_records.RecordAdapters.test_delete_is_revisioned_tombstone_and_does_not_erase_history'),
 (RECORDS,'original byte zero discarded', [('payload_id = uuid4()',"body = body.replace(b'\\x00',b'')\n    payload_id = uuid4()")],'records','test_records.RecordAdapters.test_create_read_preserves_exact_bytes_and_digest'),
 (RECORDS,'record kind changes accepted', [("(operation == 'put' and row[0] != kind)",'False')],'records','test_records.RecordAdapters.test_kind_cannot_change_after_creation'),
 (RECORDS,'bool accepted as revision', [('type(revision) is not int','not isinstance(revision,int)')],'records','test_records.RecordAdapters.test_invalid_inputs_refuse_without_partial_canonical_rows'),
 (RECORDS,'trailing newline accepted as kind', [("re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}',kind)","re.match(r'^[a-z][a-z0-9_.-]{0,63}$',kind)")],'records','test_records.RecordAdapters.test_invalid_inputs_refuse_without_partial_canonical_rows'),
 (RECORDS,'stale expected revision ignored', [('row[1] != expected','False'),('revision = expected + 1','revision = (row[1] if row is not None else expected) + 1')],'records','test_records.RecordAdapters.test_update_compare_and_swap_is_atomic'),
 (JOBS,'owner control omitted for creation', [('if control:',"if control and operation != 'job.create':")],'coordination','test_coordination.JobAdapters.test_owner_controls_and_read_cannot_write'),
 (JOBS,'explicit recipient ignored', [('if recipient is not None and recipient != str(scope.principal_id):','if False:')],'coordination','test_coordination.JobAdapters.test_explicit_recipient_refuses_other_principal'),
 (JOBS,'running job reclaimed automatically', [("if row[3] != 'pending' or row[4]:",'if False:')],'coordination','test_coordination.JobAdapters.test_concurrent_claim_has_one_winner'),
 (JOBS,'new attempt reuses previous fence', [('previous[0]+1','previous[0]')],'coordination','test_coordination.JobAdapters.test_expiry_refuses_completion_and_needs_explicit_retry'),
 (JOBS,'expired completion accepted', [(' or not current[3]','')],'coordination','test_coordination.JobAdapters.test_expiry_refuses_completion_and_needs_explicit_retry'),
 (JOBS,'holder and independent reviewer checks omitted', [('if (current[2] == str(scope.principal_id)) == review:','if False:')],'coordination','test_coordination.JobAdapters.test_owner_worker_cannot_self_accept_or_rework'),
 (JOBS,'handoff completed without review', [("if row[1] == 'handoff':",'if False:')],'coordination','test_coordination.JobAdapters.test_handoff_return_requires_independent_accept'),
 (JOBS,'canceled job eligible for retry', [("if row[3] not in ('failed','unresolved') or row[4]:",'if False:')],'coordination','test_coordination.JobAdapters.test_cancel_never_becomes_success_and_is_terminal'),
 (JOBS,'successful outcome mislabeled failed', [("return self._terminal(scope,row,attempt_id,'succeeded',body,'completed')","return self._terminal(scope,row,attempt_id,'failed',body,'completed')")],'coordination','test_coordination.JobAdapters.test_complete_preserves_exact_result_and_is_immutable'),
 (JOBS,'pagination descending rather than stable ascending', [('ORDER BY id LIMIT %s','ORDER BY id DESC LIMIT %s')],'coordination','test_coordination.JobAdapters.test_stable_bounded_pagination'),
 (JOBS,'claim holder receipt forged as creator', [("attempt_number=number,fence=fence,holder=str(scope.principal_id))","attempt_number=number,fence=fence,holder=str(UUID(self._metadata(scope,row)['creator'])))")],'coordination','test_coordination.JobAdapters.test_claim_binds_verified_principal_and_fence'),
 (FIXTURE,'fixture original bytes changed', [('"657861637400ff"','"657861637401ff"')],'core_adapters','test_core_adapters.AdapterConformance.test_fixture_request_hits_real_canonical_record_port'),
 (FIXTURE,'eventless API acceptance asserted', [('"canonical_event_ack_qualified": false','"canonical_event_ack_qualified": true')],'core_adapters','test_core_adapters.AdapterConformance.test_external_ack_and_role_human_compatibility_remain_explicitly_held'),
 (FIXTURE,'handoff bypass mapped to complete', [('"C01-R055_put_complete_handoff": "accept"','"C01-R055_put_complete_handoff": "complete"')],'core_adapters','test_core_adapters.AdapterConformance.test_legacy_method_aliases_are_retained_with_real_private_targets'),
 (FIXTURE,'budget enforcement claimed', [('"budget_status": "not_enforced"','"budget_status": "enforced"')],'core_adapters','test_core_adapters.AdapterConformance.test_budget_fixture_hits_real_claim_without_enforcement'),
 (DB_FIXTURE,'fixture writer silently elevated to owner', [('WRITE_A=auth_fixture.WRITE_A','WRITE_A=auth_fixture.OWNER_A')],'coordination','test_coordination.JobAdapters.test_claim_binds_verified_principal_and_fence'),
 (DB_FIXTURE,'fixture reader silently elevated to owner', [('READ_A=auth_fixture.READ_A','READ_A=auth_fixture.OWNER_A')],'records','test_records.RecordAdapters.test_read_key_cannot_mutate_and_cross_tenant_id_is_hidden'),
 (MUTATOR,'caller accepts exit-code-only faults', [('def mutation_status(result,expected):\n    return classify(result,expected)',"def mutation_status(result,expected):\n    return 'killed' if result.returncode else 'survived'")],'core_adapters','test_core_adapters.AdapterConformance.test_mutation_caller_refuses_operational_fixture_wrong_test_and_errors'),
]


def mutation_status(result,expected):
    return classify(result,expected)


def capture(result):
    return dict(exit_code=result.returncode,stdout=result.stdout,stderr=result.stderr,receipt=report(result),
        stdout_sha256=hashlib.sha256(result.stdout.encode()).hexdigest(),stderr_sha256=hashlib.sha256(result.stderr.encode()).hexdigest())


def checkpoint():
    with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as c:c.execute('CHECKPOINT')


def run():
    paths=(RECORDS,JOBS,FIXTURE,DB_FIXTURE,MUTATOR)
    originals={p:(NEXT/p).read_bytes() for p in paths}
    original_hashes={p:hashlib.sha256(b).hexdigest() for p,b in originals.items()}
    baseline={p:capture(suite(NEXT/'tests'/p)) for p in ('records','coordination','core_adapters')}
    print(json.dumps({'baseline':baseline}),flush=True)
    if any(r['exit_code'] != 0 or r['receipt'] is None or r['receipt']['errors'] or r['receipt']['failures'] for r in baseline.values()):
        raise SystemExit('C05 mutation baseline is RED')
    rows=[]
    try:
        for relative,label,edits,directory,target in MUTATIONS:
            checkpoint()
            source=originals[relative].decode()
            for before,after in edits:
                assert source.count(before)==1,(label,before)
                source=source.replace(before,after,1)
            changed=source.encode()
            try:
                (NEXT/relative).write_bytes(changed)
                result=suite(NEXT/'tests'/directory)
                row=dict(source=relative,mutation=label,recipe=edits,expected_test=target,
                    status=mutation_status(result,{target}),original_sha256=original_hashes[relative],
                    mutated_sha256=hashlib.sha256(changed).hexdigest(),
                    effective_source_sha256={p:hashlib.sha256((NEXT/p).read_bytes()).hexdigest() for p in paths},**capture(result))
                rows.append(row);print(json.dumps(row),flush=True)
            finally:
                for p,b in originals.items():(NEXT/p).write_bytes(b)
    finally:
        for p,b in originals.items():(NEXT/p).write_bytes(b)
    checkpoint()
    restored={p:capture(suite(NEXT/'tests'/p)) for p in ('records','coordination','core_adapters')}
    final_hashes={p:hashlib.sha256((NEXT/p).read_bytes()).hexdigest() for p in paths}
    summary=dict(mutants=len(rows),killed=sum(r['status']=='killed' for r in rows),
        survivors=[r['mutation'] for r in rows if r['status']=='survived'],
        inconclusive=[r['mutation'] for r in rows if r['status']=='inconclusive'],
        restored_source_sha256=final_hashes,restored=restored)
    print(json.dumps(summary),flush=True)
    if len(rows)!=len(MUTATIONS) or any(r['status']!='killed' for r in rows):raise SystemExit('C05 mutation qualification incomplete')
    if final_hashes!=original_hashes or any(r['exit_code']!=0 or r['receipt']['errors'] or r['receipt']['failures'] for r in restored.values()):
        raise SystemExit('C05 full baseline/source restoration failed')


if __name__=='__main__':run()
