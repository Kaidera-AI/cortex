"""Frozen desired inventory/outcome contract, written before the runner."""
import copy
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import types
import unittest
import ast
import shutil
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / 'pytest_next_offline.py'


class Subject:
    @property
    def gate(self):
        self.assertTrue(SCRIPT.is_file(), 'BODY RED: next offline inventory gate is absent')
        spec = importlib.util.spec_from_file_location('next_gate_oracle_subject', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class InventoryOracle(Subject, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('next/tests/test_a.py', 'next/other/b_test.py',
                     'next/docs/runtime/rc0/test_oracle.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('def test_body(): assert True\n')
        self.manifest = {'run': ['next/tests/test_a.py', 'next/docs/runtime/rc0/test_oracle.py'],
                         'excluded': [{'path': 'next/other/b_test.py', 'reason': 'Needs real PostgreSQL.'}]}

    def check(self):
        return self.gate.inventory(self.root, self.manifest)

    def test_complete_partition_including_rc0_oracle(self):
        result = self.check()
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['found']), 3)

    def test_new_unlisted_suite_is_red(self):
        (self.root / 'next/tests/test_new.py').write_text('def test_new(): pass\n')
        result = self.check()
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing'], ['next/tests/test_new.py'])

    def test_removed_run_entry_is_red(self):
        self.manifest['run'].pop(0)
        self.assertEqual(self.check()['missing'], ['next/tests/test_a.py'])
        self.assertFalse(self.check()['ok'])

    def test_removed_exclusion_is_red(self):
        self.manifest['excluded'].clear()
        self.assertFalse(self.check()['ok'])

    def test_stale_run_entry_is_red(self):
        self.manifest['run'].append('next/test_gone.py')
        self.assertEqual(self.check()['stale'], ['next/test_gone.py'])
        self.assertFalse(self.check()['ok'])

    def test_duplicate_or_overlap_is_red(self):
        self.manifest['run'].append(self.manifest['run'][0])
        self.assertFalse(self.check()['ok'])
        self.manifest['run'].pop()
        self.manifest['excluded'].append({'path': self.manifest['run'][0], 'reason': 'Overlap.'})
        self.assertFalse(self.check()['ok'])

    def test_blank_or_multiline_reason_is_red(self):
        for reason in ('', '  ', 'First line\nsecond line'):
            with self.subTest(reason=reason):
                self.manifest['excluded'][0]['reason'] = reason
                self.assertFalse(self.check()['ok'])

    def test_glob_escape_non_suite_and_wrong_types_are_red(self):
        for name in ('next/tests/*.py', '../next/test_a.py', '/next/test_a.py', 'next/helper.py', 42):
            with self.subTest(name=name):
                self.manifest['run'].append(name)
                self.assertFalse(self.check()['ok'])
                self.manifest['run'].pop()
        for value in ({'run': 'next/tests/test_a.py', 'excluded': []},
                      {'run': [], 'excluded': {}}, {'run': [], 'excluded': [42]}):
            self.assertFalse(self.gate.inventory(self.root, value)['ok'])

    def test_empty_run_is_red(self):
        self.manifest = {'run': [], 'excluded': [
            {'path': str(p.relative_to(self.root)), 'reason': 'Excluded.'}
            for p in self.root.rglob('*.py')]}
        self.assertFalse(self.check()['ok'])

    def test_symlink_suite_is_red(self):
        (self.root / 'outside.py').write_text('def test_body(): pass\n')
        (self.root / 'next/test_link.py').symlink_to(self.root / 'outside.py')
        self.manifest['run'].append('next/test_link.py')
        self.assertFalse(self.check()['ok'])


class OutcomeOracle(Subject, unittest.TestCase):
    def setUp(self):
        self.packet = {'expected': ['next/test_a.py::test_a'], 'selected': ['next/test_a.py::test_a'],
                       'deselected': [], 'pytest_exit': 0, 'gate_exit': 0,
                       'reports': [{'nodeid': 'next/test_a.py::test_a', 'when': when,
                                    'outcome': 'passed', 'wasxfail': False}
                                   for when in ('setup', 'call', 'teardown')]}

    def test_complete_positive(self):
        self.assertTrue(self.gate.receipt_valid(self.packet, 0))

    def test_zero_tests_is_red(self):
        self.packet.update(expected=[], selected=[], reports=[])
        self.assertFalse(self.gate.receipt_valid(self.packet, 0))

    def test_skip_xfail_xpass_and_each_failed_phase_are_red(self):
        for phase in range(3):
            for outcome, wasxfail in [('failed', False), ('skipped', False), ('passed', True)]:
                packet = copy.deepcopy(self.packet)
                packet['reports'][phase].update(outcome=outcome, wasxfail=wasxfail)
                self.assertFalse(self.gate.receipt_valid(packet, 0))

    def test_raw_exit_gate_exit_process_exit_must_agree(self):
        for key in ('pytest_exit', 'gate_exit'):
            packet = {**self.packet, key: 1}
            self.assertFalse(self.gate.receipt_valid(packet, 0))
        self.assertFalse(self.gate.receipt_valid(self.packet, 1))
        self.assertFalse(self.gate.receipt_valid(self.packet, -9))

    def test_missing_duplicate_foreign_phases_and_deselection_are_red(self):
        for reports in (self.packet['reports'][:-1], self.packet['reports'] * 2,
                        self.packet['reports'] + [{'nodeid': 'foreign', 'when': 'call',
                                                  'outcome': 'passed', 'wasxfail': False}]):
            self.assertFalse(self.gate.receipt_valid({**self.packet, 'reports': reports}, 0))
        self.assertFalse(self.gate.receipt_valid({**self.packet, 'deselected': ['hidden']}, 0))

    def test_missing_or_malformed_receipt_is_red(self):
        for packet in (None, {}, {'expected': []}, {'expected': [], 'reports': None}):
            self.assertFalse(self.gate.receipt_valid(packet, 0))

    def test_declared_unittest_suite_preserves_outer_controls_not_intentional_probes(self):
        path = SCRIPT.with_name('pytest_next_worker.py')
        spec = importlib.util.spec_from_file_location('declared_suite_subject', path)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        self.assertTrue(callable(getattr(worker, 'declared_cases', None)),
                        'BODY RED: declared unittest load_tests protocol is not honored')
        module = types.ModuleType('declared_suite_fixture')

        class IntentionalProbe(unittest.TestCase):
            def test_negative(self):
                self.fail('negative probe must only run through the outer control')

        class ActualControl(unittest.TestCase):
            def test_control(self):
                result = unittest.TestResult()
                IntentionalProbe('test_negative').run(result)
                self.assertEqual(len(result.failures), 1)
                self.assertFalse(result.errors)

        module.IntentionalProbe, module.ActualControl = IntentionalProbe, ActualControl
        module.load_tests = lambda loader, tests, pattern: loader.loadTestsFromTestCase(ActualControl)
        cases = worker.declared_cases(module)
        self.assertEqual([type(case) for case in cases], [ActualControl])
        result = unittest.TestResult()
        unittest.TestSuite(cases).run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertTrue(result.wasSuccessful())


class RetirementOracle(Subject, unittest.TestCase):
    """Named in-tree authority, retained bytes and mandatory live v2 witnesses."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = 'next/tests/retirement/test_a.py'
        self.replacement = 'next/tests/retirement/test_a_v2.py'
        self.record = 'next/tests/retirement/run_active.py'
        self.reason = 'Kai: named historical checkpoint; replaced by v2.'
        self.old = self.original + '::A::test_old'
        self.live = self.original + '::A::test_live'
        self.new = self.replacement + '::V2::test_new'
        self.put(self.original, 'class A:\n def test_old(self): assert False\n def test_live(self): pass\n')
        self.put(self.replacement, 'class V2:\n def test_new(self): pass\n')
        self.put(self.record, 'RETIRED = ' + repr({'test_a.A.test_old': self.reason}) + '\n')
        self.manifest = dict(run=[self.original, self.replacement], excluded=[], retirements=[
            dict(record=self.record, sha256=self.digest(self.record),
                 files={p:self.digest(p) for p in (self.original, self.replacement)})])

    def put(self, path, text):
        target = self.root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def digest(self, path):
        return hashlib.sha256((self.root/path).read_bytes()).hexdigest()

    def check(self):
        return self.gate.inventory(self.root, self.manifest)

    def refused(self, code):
        checked = self.check()
        self.assertFalse(checked['ok'], 'unproved retirement was credited')
        self.assertIn('retirement: ' + code, checked['invalid'])

    def repin(self, path):
        if path == self.record:
            self.manifest['retirements'][0]['sha256'] = self.digest(path)
        else:
            self.manifest['retirements'][0]['files'][path] = self.digest(path)

    def test_exact_retirement_is_not_run_and_v2_is_mandatory(self):
        checked = self.check()
        self.assertTrue(checked['ok'], checked)
        self.assertEqual([row['nodeid'] for row in checked.get('retired', [])], [self.old])
        row = checked['retired'][0]
        self.assertEqual(row['reason'], self.reason)
        self.assertEqual(row['source_sha256'], self.digest(self.original))
        self.assertEqual(row['record_sha256'], self.digest(self.record))
        self.assertEqual(row['replacement_nodes'], [self.new])
        self.assertEqual(checked.get('replacement_nodes'), {self.replacement:[self.new]})

    def test_record_is_read_without_executing_its_runner(self):
        with (self.root/self.record).open('a') as stream:
            stream.write("raise RuntimeError('must never execute the retirement runner')\n")
        self.repin(self.record)
        self.assertTrue(self.check()['ok'])

    def test_manifest_alone_cannot_name_a_new_retirement(self):
        self.manifest['retirements'][0]['nodes'] = [self.live]
        self.refused('binding_schema')

    def test_missing_binding_is_refused(self):
        self.manifest['retirements'].clear()
        self.refused('record_inventory')

    def test_duplicate_binding_is_refused(self):
        self.manifest['retirements'] *= 2
        self.refused('record_inventory')

    def test_edited_retirement_reason_is_refused(self):
        self.put(self.record, 'RETIRED = ' + repr({'test_a.A.test_old':'changed reason'}) + '\n')
        self.refused('record_hash')

    def test_nonliteral_record_is_refused_even_when_repinned(self):
        self.put(self.record, 'RETIRED = dict(test_a="unproved")\n')
        self.repin(self.record)
        self.refused('record_literal')

    def test_duplicate_literal_key_is_refused(self):
        self.put(self.record, "RETIRED = {'test_a.A.test_old':'first', 'test_a.A.test_old':'second'}\n")
        self.repin(self.record)
        self.refused('record_literal')

    def test_syntax_error_is_typed_not_executed(self):
        self.put(self.record, 'RETIRED = {\n')
        self.repin(self.record)
        self.refused('record_parse')

    def test_retired_node_must_exist_in_retained_original(self):
        self.put(self.record, 'RETIRED = ' + repr({'test_a.A.test_absent':self.reason}) + '\n')
        self.repin(self.record)
        self.refused('original_node')

    def test_original_bytes_cannot_change(self):
        with (self.root/self.original).open('a') as stream: stream.write('# changed\n')
        self.refused('file_hash')

    def test_replacement_must_exist(self):
        (self.root/self.replacement).unlink()
        self.refused('file_hash')

    def test_replacement_bytes_cannot_change(self):
        with (self.root/self.replacement).open('a') as stream: stream.write('# weakened\n')
        self.refused('file_hash')

    def test_all_and_only_original_and_v2_files_must_be_bound(self):
        self.manifest['retirements'][0]['files'].pop(self.original)
        self.refused('file_bindings')

    def test_original_file_with_live_cases_cannot_be_excluded(self):
        self.manifest['run'].remove(self.original)
        self.manifest['excluded'].append(dict(path=self.original, reason='hide live cases'))
        self.refused('live_suite')

    def test_replacement_cannot_be_excluded(self):
        self.manifest['run'].remove(self.replacement)
        self.manifest['excluded'].append(dict(path=self.replacement, reason='hide replacement'))
        self.refused('live_suite')

    def test_replacement_must_declare_named_live_cases(self):
        self.put(self.replacement, 'class V2: pass\n')
        self.repin(self.replacement)
        self.refused('replacement_nodes')

    def worker(self, rows):
        path = SCRIPT.with_name('pytest_next_worker.py')
        spec = importlib.util.spec_from_file_location('retirement_worker_subject', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn('retired', inspect.signature(module.Gate).parameters,
                      'BODY RED: worker has no exact retirement collection contract')
        return module.Gate(self.root/'out.json', False, retired=rows)

    def row(self):
        return dict(nodeid=self.old, reason=self.reason, record=self.record,
                    record_sha256=self.digest(self.record), source_sha256=self.digest(self.original),
                    replacement_nodes=[self.new])

    def test_worker_removes_only_named_historical_case_and_keeps_new_live_case(self):
        gate = self.worker([self.row()])
        names = [self.old, self.live, self.original+'::A::test_new_unretired']
        session = types.SimpleNamespace(items=[types.SimpleNamespace(nodeid=n) for n in names])
        gate.pytest_collection_finish(session)
        self.assertEqual([item.nodeid for item in session.items], names[1:])
        self.assertEqual(gate.packet['collected'], names)
        self.assertEqual(gate.packet['retired'], [self.row()])
        self.assertEqual(gate.packet['deselected'], [])

    def test_worker_refuses_missing_or_duplicate_retired_case(self):
        for names in ([self.live], [self.old, self.old, self.live]):
            gate = self.worker([self.row()])
            session = types.SimpleNamespace(items=[types.SimpleNamespace(nodeid=n) for n in names])
            with self.assertRaisesRegex(ValueError, '^retirement_collection$'):
                gate.pytest_collection_finish(session)

    def packet(self, nodes):
        return dict(expected=nodes[:], selected=nodes[:], deselected=[], pytest_exit=0, gate_exit=0,
            retired=[], collected=nodes[:], reports=[dict(nodeid=node, when=phase,
                outcome='passed', wasxfail=False) for node in nodes for phase in ('setup','call','teardown')])

    def admitted(self, packet, retired=(), replacements=()):
        subject = self.gate
        self.assertIn('retired', inspect.signature(subject.receipt_valid).parameters,
                      'BODY RED: host does not reconcile retirement witnesses')
        return subject.receipt_valid(packet, 0, retired=retired, replacements=replacements)

    def old_packet(self):
        packet = self.packet([self.live])
        packet.update(retired=[self.row()], collected=[self.old, self.live])
        return packet

    def test_host_accepts_complete_retirement_receipt(self):
        self.assertTrue(self.admitted(self.old_packet(), retired=[self.row()]))

    def test_host_refuses_edited_retirement_metadata(self):
        packet = self.old_packet()
        packet['retired'][0]['reason'] = 'forged reason'
        self.assertFalse(self.admitted(packet, retired=[self.row()]))

    def test_host_refuses_unaccounted_collected_case(self):
        packet = self.old_packet()
        packet['collected'].append(self.original+'::A::test_hidden_live')
        self.assertFalse(self.admitted(packet, retired=[self.row()]))

    def test_host_cannot_credit_a_retired_node_as_passed(self):
        packet = self.packet([self.old, self.live])
        packet['retired'] = [self.row()]
        self.assertFalse(self.admitted(packet, retired=[self.row()]))

    def test_host_refuses_a_manifest_free_retirement(self):
        packet = self.old_packet()
        self.assertFalse(self.admitted(packet))

    def test_host_refuses_retired_credit_with_forged_duplicate_collection(self):
        packet = self.packet([self.old, self.live])
        packet.update(retired=[self.row()], collected=[self.old, self.old, self.live])
        self.assertFalse(self.admitted(packet, retired=[self.row()]))

    def test_symlinked_retirement_record_is_rejected_before_read(self):
        from unittest.mock import patch
        outside = self.root/'outside.py'
        outside.write_text('RETIRED = {}\n')
        record = self.root/self.record
        record.unlink()
        record.symlink_to(outside)
        observed = []
        original = Path.read_text
        def tracking(path, *args, **kwargs):
            if path.resolve() == record.resolve(): observed.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', tracking):
            checked = self.check()
        self.assertFalse(checked['ok'])
        self.assertEqual(observed, [], 'rejected retirement symlink must never be read')

    def test_replacement_receipt_must_run_exact_named_cases(self):
        self.assertTrue(self.admitted(self.packet([self.new]), replacements=[self.new]))
        self.assertFalse(self.admitted(self.packet([self.replacement+'::V2::test_other']), replacements=[self.new]))

    def test_replacement_failure_or_skip_is_never_credit(self):
        for outcome in ('failed', 'skipped'):
            packet = self.packet([self.new])
            packet['reports'][1]['outcome'] = outcome
            self.assertFalse(self.admitted(packet, replacements=[self.new]))


class InventoryRepairOracle(Subject, unittest.TestCase):
    """Vera's deciding attacks plus reviewer-independent repair controls."""
    BASE = 'a47a152ba4eb048f4a9ff211b875d59d82a2e066'
    ROOT = SCRIPT.parents[2]
    WRITER = 'next/tests/outbox_inventory/test_outbox_inventory.py'
    MIXED = ('next/tests/benchmarks/test_postgres_cleanup.py',
             'next/tests/benchmarks/test_vector_baseline.py',
             'next/tests/artifact/test_legacy_map.py')

    def base(self):
        return json.loads(subprocess.check_output(['git','show',self.BASE+':.github/next-offline-suites.json'],cwd=self.ROOT))

    def current(self):
        return json.loads((self.ROOT/'.github/next-offline-suites.json').read_text())

    def checked(self, root, manifest, base=None):
        gate=self.gate
        # The exact desired BODY assertions also run against the old two-arg API.
        if 'base_manifest' in inspect.signature(gate.inventory).parameters:
            return gate.inventory(root,manifest,base_manifest=self.base() if base is None else base)
        return gate.inventory(root,manifest)

    def move(self, manifest, paths):
        for path in paths:
            manifest['run'].remove(path)
            manifest['excluded'].append({'path':path,'reason':'Needs real PostgreSQL through the direct or inherited database fixture.'})

    def test_vera_manifest_only_cannot_hide_known_live_writer(self):
        manifest=self.current();self.move(manifest,[self.WRITER])
        checked=self.checked(self.ROOT,manifest)
        self.assertFalse(checked['ok'],'Known live writer removed from RUN by manifest-only edit')
        if 'transitions' in checked:
            self.assertIn(self.WRITER,[row['path'] for row in checked['transitions']])

    def test_vera_manifest_only_cannot_hide_91_live_suites(self):
        manifest=self.current()
        keep={path for row in self.base()['retirements'] for path in row['files']}
        removed=[path for path in self.base()['run'] if path not in keep]
        self.assertEqual(len(removed),91)
        self.move(manifest,removed)
        checked=self.checked(self.ROOT,manifest)
        self.assertFalse(checked['ok'],'91 accepted RUN suites removed by manifest-only edit')
        if 'transitions' in checked:self.assertEqual(len(checked['transitions']),91)

    def test_forged_fixture_import_does_not_hide_writer(self):
        manifest=self.current();self.move(manifest,[self.WRITER])
        manifest['excluded'][-1]['evidence']={'kind':'postgres_fixture','source':self.WRITER,
            'sha256':hashlib.sha256((self.ROOT/self.WRITER).read_bytes()).hexdigest(),'symbol':'InventoryTests.setUp'}
        self.assertFalse(self.checked(self.ROOT,manifest)['ok'])

    def retirement_fixture(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);current=self.current();base=self.base()
        bindings=copy.deepcopy(base['retirements'])
        run=sorted({p for row in bindings for p in row['files']})
        for path in [p for row in bindings for p in [row['record'],*row['files']]]:
            target=root/path;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((self.ROOT/path).read_bytes())
        manifest={'run':run,'excluded':[],'retirements':bindings}
        baseline=copy.deepcopy(manifest)
        if 'retirement_checks' in current:
            manifest['retirement_checks']=copy.deepcopy(current['retirement_checks'])
            for check in manifest['retirement_checks']:
                path=check['mutation']['path'];target=root/path
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((self.ROOT/path).read_bytes())
        return root,manifest,baseline

    def test_repinned_edited_retirement_reason_is_refused(self):
        root,manifest,base=self.retirement_fixture();binding=manifest['retirements'][0]
        path=root/binding['record'];path.write_text(path.read_text().replace('preimplementation-only','forged-approved',1))
        binding['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertFalse(self.checked(root,manifest,base)['ok'],'repinning changed authority cannot approve it')

    def trivial(self, statement):
        root,manifest,base=self.retirement_fixture();binding=manifest['retirements'][0]
        replacement=next(p for p in binding['files'] if p.endswith('_v2.py'))
        path=root/replacement;tree=ast.parse(path.read_text())
        for cls in tree.body:
            if isinstance(cls,ast.ClassDef):
                for node in cls.body:
                    if isinstance(node,ast.FunctionDef) and node.name.startswith('test_'):
                        node.body=ast.parse(statement).body
        path.write_text(ast.unparse(tree)+'\n');binding['files'][replacement]=hashlib.sha256(path.read_bytes()).hexdigest()
        return self.checked(root,manifest,base)

    def test_repinned_two_pass_replacements_are_refused(self):
        self.assertFalse(self.trivial('pass')['ok'],'Vera two-method trivial replacement admitted')

    def test_repinned_constant_assertions_are_refused(self):
        self.assertFalse(self.trivial('self.assertTrue(True)')['ok'],'constant asserts are not semantic coverage')

    def test_all_32_mixed_offline_methods_are_live(self):
        checked=self.checked(self.ROOT,self.current())
        self.assertTrue(checked['ok'],checked)
        self.assertTrue(set(self.MIXED)<=set(checked['run']),'mixed files still hide 32 offline methods')
        rows=checked.get('method_exclusions',[])
        self.assertEqual(len(rows),3,'only the three actually unavailable nodes may be removed')

    def test_all_four_replacements_have_deciding_product_mutations(self):
        checked=self.checked(self.ROOT,self.current())
        self.assertTrue(checked['ok'],checked)
        self.assertEqual(len(checked.get('retirement_checks',[])),4,'named deciding assertions/product mutations missing')

    def test_missing_trusted_base_is_fail_closed(self):
        self.assertFalse(self.checked(self.ROOT,self.current(),{})['ok'],'missing independent base treated as valid')


if __name__ == '__main__':
    unittest.main(verbosity=2)
