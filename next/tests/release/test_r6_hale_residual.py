"""Hale's R6 mechanics counterexamples (hale-r6-code-review-20261016/RESIDUAL.py; one
precondition ported in test_time_row_must_refuse_a_missing_run_journal). No product edits or native engine effects."""
import copy, hashlib, json, pathlib, subprocess, sys, tempfile, unittest
from test_r6_native_operator import row
from test_r4_native_lane import Lane, originals, runner, inspector, r3

WITNESSES={}

class Boundaries(unittest.TestCase):
    def shortened(self):
        budget=copy.deepcopy(runner.operator_budget(600))
        budget['stages']['lanes']=4*600
        budget['total_sec']=sum(budget['stages'].values())
        return budget

    def test_control_five_window_budget_and_unit_agree(self):
        budget=runner.operator_budget(600)
        deadline=runner.operator_deadline(budget, clock=lambda:0)
        _,unit=runner.operator_unit('cortex-r6-hale-control',budget,['/bin/true'],['/bin/true'])
        WITNESSES['five_window']={'budget':budget,'expires':deadline.expires,'work_expires':deadline.work_expires}
        self.assertEqual(budget['stages']['lanes'],3000)
        self.assertEqual(deadline.expires,8400)
        self.assertEqual(deadline.work_expires,7200)
        self.assertIn('RuntimeMaxSec=8400\n',unit)

    def test_reconciled_four_window_budget_must_refuse(self):
        budget=self.shortened()
        try:
            deadline=runner.operator_deadline(budget,clock=lambda:0)
        except ValueError as error:
            self.assertEqual(str(error),'operator_budget_unreconciled')
            return
        WITNESSES['four_window_deadline_accepted']={'budget':budget,'expires':deadline.expires,
                                                  'work_expires':deadline.work_expires}
        self.fail('operator_deadline accepted four windows with a reconciled 7800-second total')

    def test_unit_must_refuse_shortened_or_missing_teardown_budget(self):
        bad=[self.shortened(),runner.operator_budget(600)]
        bad[1]['stages']['teardown']=0
        bad[1]['total_sec']=sum(bad[1]['stages'].values())
        accepted=[]
        for budget in bad:
            try:
                path,unit=runner.operator_unit('cortex-r6-hale-short',budget,['/bin/true'],['/bin/true'])
            except ValueError as error:
                self.assertEqual(str(error),'operator_unit_refused')
            else:
                accepted.append({'budget':budget,'unit':unit,'path':path})
        WITNESSES['shortened_units_accepted']=accepted
        self.assertEqual(accepted,[],'unit generated with four windows or no teardown reserve')

    def test_control_clean_deallocation_passes(self):
        calls=[]
        result=runner.operator_drive(lambda:calls.append('start'),lambda:'inactive',
                                     lambda:calls.append('deallocate'),lambda:'deallocated',polls=1)
        WITNESSES['clean_driver']={'calls':calls,'result':result}
        self.assertEqual(result['result'],'PASS')
        self.assertEqual(calls,['start','deallocate'])

    def test_deallocation_failure_is_kept_even_after_deallocated_readback(self):
        calls=[]
        def fail():
            calls.append('deallocate')
            raise OSError('deallocate request failed')
        result=runner.operator_drive(lambda:calls.append('start'),lambda:'inactive',fail,
                                     lambda:'deallocated',polls=1)
        WITNESSES['deallocate_error_masked']={'calls':calls,'result':result}
        self.assertEqual(calls,['start','deallocate'])
        self.assertIn(result['result'],('RED','HOLD'),'deallocation exception erased into PASS')

    def test_real_adapter_timeout_retains_its_original_journal(self):
        # Exact production _engine/run_operation; the argv is a harmless bounded local Python sleep.
        with tempfile.TemporaryDirectory(dir=r3.private_base()) as temporary:
            root=pathlib.Path(temporary)
            evidence=root/'evidence';evidence.mkdir()
            children=inspector._Children({'PATH':'/usr/bin:/bin'},evidence_dir=evidence)
            argv=[sys.executable,'-I','-S','-B','-c','import time; time.sleep(60)']
            with self.assertRaises(subprocess.TimeoutExpired):
                children.call(argv,root,0.1,4096)
            journals=list(root.glob('op-*.out.operation.jsonl'))
            self.assertEqual(len(journals),1)
            original=[json.loads(line) for line in journals[0].read_text().splitlines()]
            self.assertEqual(original[-1]['status'],'failed')
            self.assertEqual(original[-1]['error'],'TimeoutExpired')
            recorded=children.records[0]
            WITNESSES['real_timeout_original_lost']={'original':original,'raw_journal_utf8':journals[0].read_text(),'sha256':hashlib.sha256(journals[0].read_bytes()).hexdigest(),'child':recorded,
                  'exported_files':sorted(p.name for p in evidence.iterdir())}
            self.assertIsNotNone(recorded.get('journal'),'original timeout journal exists but collector drops it')

    def test_time_row_must_refuse_a_missing_run_journal(self):
        lane=Lane(self)
        files,rows=originals(lane)
        children=json.loads(files[rows['time']['children']])
        # ported (nix): after the seam fix the collector keeps the journal, so this probe
        # REMOVES it from the originals; the deciding expectation is unchanged
        children[2]['journal']=None
        files=dict(files,**{rows['time']['children']:r3.codec.canonical_bytes(children)})
        try:
            consumed,_=row(lane,'time',files=files,rows=rows)
        except ValueError as error:
            self.assertEqual(str(error),'native_inspector_execution_unqualified')
            return
        WITNESSES['time_accepted_no_adapter_journal']={'run':children[2],
                                                   'consumed_stream_count':len(consumed)}
        self.fail('time qualification accepted no adapter run journal, relying on collector metadata')
