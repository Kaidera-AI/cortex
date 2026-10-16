"""Frozen exact-inventory CI oracle; these unit fixtures are not PG evidence."""
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'pytest_u13_worker.py'

class ExactU13Gate(unittest.TestCase):
    def gate(self):
        self.assertTrue(SCRIPT.is_file(), 'U13 exact-node outcome gate is absent')
        spec = importlib.util.spec_from_file_location('u13_gate_oracle', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.valid

    def test_worker_selects_original_eight_and_three_exact_pg_c_methods(self):
        spec = importlib.util.spec_from_file_location('u13_inventory_oracle', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.TARGETS, HostReceiptAdmission.NODES)

    def packet(self):
        nodes = ['fixture::test_' + str(i) for i in range(8)]
        return nodes, {'selected': nodes[:], 'deselected': [], 'reports': [
            {'nodeid': node, 'when': phase, 'outcome': 'passed', 'wasxfail': False}
            for node in nodes for phase in ('setup', 'call', 'teardown')], 'pytest_exit': 0}

    def test_complete_eight_actual_call_phases_pass(self):
        nodes, packet = self.packet()
        self.assertTrue(self.gate()(nodes, packet))

    def test_removed_or_zero_selected_never_pass(self):
        for count in (7, 0):
            nodes, packet = self.packet()
            packet['selected'] = nodes[:count]
            packet['reports'] = [r for r in packet['reports'] if r['nodeid'] in packet['selected']]
            self.assertFalse(self.gate()(nodes, packet))

    def test_skip_and_xfail_and_xpass_never_pass(self):
        for outcome, xfail in [('skipped', False), ('skipped', True), ('passed', True)]:
            nodes, packet = self.packet()
            packet['reports'][1].update(outcome=outcome, wasxfail=xfail)
            self.assertFalse(self.gate()(nodes, packet))

    def test_deselection_fails_even_when_eight_nodes_pass(self):
        nodes, packet = self.packet()
        packet['deselected'] = ['unexpected::test_extra']
        self.assertFalse(self.gate()(nodes, packet))

    def test_body_or_setup_or_teardown_failure_never_pass(self):
        for index in (0, 1, 2):
            nodes, packet = self.packet()
            packet['reports'][index]['outcome'] = 'failed'
            self.assertFalse(self.gate()(nodes, packet))

    def test_missing_duplicate_or_foreign_phase_refuses(self):
        for change in ('missing', 'duplicate', 'foreign'):
            nodes, packet = self.packet()
            if change == 'missing': packet['reports'].pop()
            elif change == 'duplicate': packet['reports'].append(packet['reports'][0].copy())
            else: packet['reports'][0]['nodeid'] = 'foreign::test_extra'
            self.assertFalse(self.gate()(nodes, packet))

    def test_nonzero_exit_or_duplicate_inventory_refuses(self):
        nodes, packet = self.packet()
        packet['pytest_exit'] = 3
        self.assertFalse(self.gate()(nodes, packet))
        nodes, packet = self.packet()
        packet['selected'][-1] = packet['selected'][0]
        self.assertFalse(self.gate()(nodes, packet))

class HostReceiptAdmission(unittest.TestCase):
    """Own frozen oracle: real terminating child, mocked engine, actual host boundary."""
    NODES = (
        'next/tests/core_upgrade/test_migration_steps.py::MigrationSteps::test_named_prefix_and_reentry',
        'next/tests/core_upgrade/test_migration_steps.py::MigrationSteps::test_unknown_target_and_nonprefix_ledger_refuse_before_sql',
        'next/tests/core_upgrade/test_rehearsal.py::Rehearsal::test_two_step_crash_reentry_preserves_canonical_state',
        'next/tests/core_upgrade/test_rehearsal.py::Rehearsal::test_real_pg_rollback_after_every_expand_prefix',
        'next/tests/core_upgrade/test_rehearsal.py::Rehearsal::test_paused_real_pg_apply_drains_before_rollback',
        'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_current_four_steps_reconnect_reenter_and_preserve_seed',
        'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_unknown_target_refuses_before_any_later_sql',
        'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_nonprefix_drift_and_future_ledger_refuse_before_sql',
        'next/tests/core_upgrade/test_migration_steps.py::MigrationSteps::test_caller_begin_refuses_before_any_managed_ddl',
        'next/tests/core_upgrade/test_migration_steps.py::MigrationSteps::test_idle_top_level_commit_visible_before_return',
        'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_selected20_target_reentry_and_preservation',
    )

    def setUp(self):
        import tempfile
        import sys
        scripts = SCRIPT.parent
        sys.path.insert(0, str(scripts))
        spec = importlib.util.spec_from_file_location('own_host_receipt_oracle', scripts/'pytest_u13_pg.py')
        self.host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.host)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root/'source'
        for name in ('.github/scripts/pytest_u13_worker.py', 'next/src/cortex_core/migrations.py',
                     *dict.fromkeys(node.split('::')[0] for node in self.NODES)):
            destination = self.source/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((scripts.parents[1]/name).read_bytes())

    def packet(self, run_id):
        import hashlib
        nodes = list(self.NODES)
        digest = lambda name: hashlib.sha256((self.source/name).read_bytes()).hexdigest()
        return dict(schema_version=1, receipt_run_id=run_id, session_finished=True,
            worker_sha256=digest('.github/scripts/pytest_u13_worker.py'),
            expected=nodes, selected=nodes[:], deselected=[], passed=nodes[:],
            reports=[dict(nodeid=node, when=phase, outcome='passed', wasxfail=False, detail='')
                     for node in nodes for phase in ('setup','call','teardown')],
            counts=dict(selected=11, passed=11, phases=33, deselected=0),
            source_files={node:digest(node.split('::')[0]) for node in nodes},
            mutation=None, driver=dict(psycopg='3.2.9', pq_impl='python', libpq_version=170006),
            case_root=dict(uid=10001, mode='0o700'), pytest_exit=0, gate_exit=0, control='normal',
            qualification='HOSTED_CI_PG_ONLY', native_qualified=False, old_installation='G_OLD_UNRESOLVED',
            loaded_migrations=dict(path='/source/next/src/cortex_core/migrations.py',
                                   sha256=digest('next/src/cortex_core/migrations.py')),
            runtime=dict(uid=10001, gid=10001, cap_eff='0000000000000000', no_new_privs=1, root_read_only=True),
            case_root_cleanup=dict(owned_root_absent=True))

    def execute(self, change=None, *, mode='json', worker_exit=0, fake_mode=None):
        import json
        import os
        import stat
        import subprocess
        import sys
        from unittest.mock import patch
        owner = self

        class Engine(self.host.U13Fixture):
            def create_u13(self): pass
            def run(self, *args, **kwargs): return 'owned-mock-driver'
            def command(self, arguments, **kwargs):
                if arguments[0]=='unshare':
                    return subprocess.CompletedProcess(arguments, 0, '', '')
                owner.assertEqual(arguments[:3], ['exec','owned-mock-driver','python'])
                path=self.results/'outcomes.json'
                packet=owner.packet(self.run_id)
                if change: change(packet)
                raw=json.dumps(packet)
                if mode=='empty-object': raw='{}'
                elif mode=='truncated': raw=raw[:-1]
                elif mode=='empty': raw=''
                elif mode=='oversize': raw=' '*(1024*1024)+raw
                elif mode=='duplicate-key': raw=raw[:-1]+',"schema_version":1}'
                # An actual child process terminates; no worker or database is simulated as running.
                code="import os,sys;from pathlib import Path;p=Path(sys.argv[1]);mode=sys.argv[2];raw=sys.stdin.read();\nif mode!='absent':\n target=p.with_name('linked.json') if mode in ('symlink','hardlink') else p\n target.write_text(raw)\n if mode=='symlink':p.symlink_to(target)\n if mode=='hardlink':os.link(target,p)\nos._exit(int(sys.argv[3]))"
                process=subprocess.run([sys.executable,'-B','-c',code,str(path),mode,str(worker_exit)],
                                       input=raw,capture_output=True,text=True,timeout=10)
                owner.assertEqual(process.returncode,worker_exit,'child lifecycle differs from oracle')
                return process

        output=self.root/'output'
        output.mkdir()
        fixture=Engine(output,self.root/'site',self.source)
        if fake_mode is None:
            result=fixture.execute_u13('normal')
        else:
            original=os.fstat
            observed=[]
            def fstat(fd):
                value=original(fd)
                fields=list(value)
                if fake_mode=='changed':
                    observed.append(fd)
                    if len(observed)>1: fields[6]+=1
                else: fields[0]=stat.S_IFCHR | stat.S_IMODE(value.st_mode)
                return os.stat_result(fields)
            with patch('os.fstat',side_effect=fstat):
                result=fixture.execute_u13('normal')
        return result,fixture

    def refused(self, *args, **kwargs):
        code,fixture=self.execute(*args,**kwargs)
        self.assertNotEqual(code,0,'host credited an invalid or absent zero-exit worker receipt')
        admission=fixture.receipt.get('worker_admission')
        self.assertIsInstance(admission,dict,'host did not record a typed receipt admission')
        self.assertIs(admission['ok'],False)
        self.assertIsInstance(admission['code'],str)

    def test_early_zero_without_receipt_is_typed_host_failure(self): self.refused(mode='absent')
    def test_empty_object_receipt_is_typed_host_failure(self): self.refused(mode='empty-object')
    def test_truncated_receipt_is_typed_host_failure(self): self.refused(mode='truncated')
    def test_empty_or_oversized_receipt_is_typed_host_failure(self): self.refused(mode='oversize')
    def test_empty_receipt_is_typed_host_failure(self): self.refused(mode='empty')
    def test_duplicate_json_key_is_typed_host_failure(self): self.refused(mode='duplicate-key')
    def test_unknown_field_is_typed_host_failure(self): self.refused(lambda p:p.update(untrusted_override=True))
    def test_unknown_nested_field_is_typed_host_failure(self): self.refused(lambda p:p['runtime'].update(override=True))
    def test_boolean_recorded_exit_is_typed_host_failure(self): self.refused(lambda p:p.update(pytest_exit=False))
    def test_duplicate_node_is_typed_host_failure(self):
        self.refused(lambda p:p['selected'].__setitem__(-1,p['selected'][0]))
    def test_extra_node_is_typed_host_failure(self):
        self.refused(lambda p:p['selected'].append('foreign::extra'))
    def test_missing_phase_is_typed_host_failure(self): self.refused(lambda p:p['reports'].pop())
    def test_duplicate_phase_with_33_rows_is_typed_host_failure(self):
        self.refused(lambda p:p['reports'].__setitem__(0,p['reports'][3].copy()))
    def test_schema_valid_failing_phase_is_typed_host_failure(self):
        self.refused(lambda p:p['reports'][0].update(outcome='failed',detail='synthetic failing setup'))
    def test_wrong_counts_is_typed_host_failure(self): self.refused(lambda p:p['counts'].update(phases=34))
    def test_missing_session_finish_is_typed_host_failure(self): self.refused(lambda p:p.update(session_finished=False))
    def test_nonzero_worker_even_with_valid_receipt_is_typed_host_failure(self): self.refused(worker_exit=9)
    def test_nonzero_recorded_exit_is_typed_host_failure(self): self.refused(lambda p:p.update(pytest_exit=1))
    def test_wrong_source_hash_is_typed_host_failure(self):
        self.refused(lambda p:p['source_files'].__setitem__(self.NODES[0],'f'*64))
    def test_wrong_worker_hash_is_typed_host_failure(self): self.refused(lambda p:p.update(worker_sha256='f'*64))
    def test_wrong_run_binding_is_typed_host_failure(self): self.refused(lambda p:p.update(receipt_run_id='foreign-run'))
    def test_missing_post_gate_completion_is_typed_host_failure(self):
        self.refused(lambda p:p['case_root_cleanup'].update(owned_root_absent=False))
    def test_false_runtime_identity_is_typed_host_failure(self): self.refused(lambda p:p['runtime'].update(uid=0))
    def test_wrong_qualification_is_typed_host_failure(self): self.refused(lambda p:p.update(native_qualified=True))
    def test_symlink_receipt_is_typed_host_failure(self): self.refused(mode='symlink')
    def test_hardlinked_receipt_is_typed_host_failure(self): self.refused(mode='hardlink')
    def test_nonregular_fstat_is_typed_host_failure(self): self.refused(fake_mode='character-device')
    def test_receipt_changes_during_read_is_typed_host_failure(self): self.refused(fake_mode='changed')
    def test_valid_complete_receipt_is_accepted(self):
        code,fixture=self.execute()
        self.assertEqual(code,0)
        self.assertEqual(fixture.receipt['worker_exit'],0)

    def test_host_independently_requires_all_eleven_exact_methods(self):
        expected=self.host.expected_receipt(self.source, 'frozen-inventory', 'normal')
        self.assertEqual(tuple(expected['source_files']),self.NODES)
        self.assertEqual(expected['counts'],dict(selected=11,passed=11,phases=33,deselected=0))

    def omit_added_node(self, node):
        def remove(packet):
            for key in ('expected','selected','passed'):
                packet[key].remove(node)
            packet['reports']=[r for r in packet['reports'] if r['nodeid']!=node]
            packet['source_files'].pop(node)
            packet['counts']=dict(selected=10,passed=10,phases=30,deselected=0)
        code,fixture=self.execute(remove)
        self.assertNotEqual(code,0)
        self.assertEqual(fixture.receipt['worker_admission']['code'],'receipt_inventory')

    def omit_added_call(self, node):
        def remove(packet):
            packet['reports']=[r for r in packet['reports'] if (r['nodeid'],r['when'])!=(node,'call')]
        code,fixture=self.execute(remove)
        self.assertNotEqual(code,0)
        self.assertEqual(fixture.receipt['worker_admission']['code'],'receipt_phases')

    def test_caller_begin_selector_cannot_be_omitted(self): self.omit_added_node(self.NODES[-3])
    def test_idle_commit_selector_cannot_be_omitted(self): self.omit_added_node(self.NODES[-2])
    def test_selected20_selector_cannot_be_omitted(self): self.omit_added_node(self.NODES[-1])
    def test_caller_begin_call_phase_is_required(self): self.omit_added_call(self.NODES[-3])
    def test_idle_commit_call_phase_is_required(self): self.omit_added_call(self.NODES[-2])
    def test_selected20_call_phase_is_required(self): self.omit_added_call(self.NODES[-1])

if __name__ == '__main__': unittest.main()
