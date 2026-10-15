"""Frozen desired inventory/outcome contract, written before the runner."""
import copy
import os
import sys
import hashlib
import importlib.util
import inspect
import json
import re
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
        self.original_manifest=copy.deepcopy(self.manifest)

    def check(self):
        gate=self.gate
        if 'base_manifest' in inspect.signature(gate.inventory).parameters:
            return gate.inventory(self.root,self.manifest,base_manifest=self.original_manifest)
        return gate.inventory(self.root, self.manifest)

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
        self.put(self.replacement, "from product import accept\nclass V2:\n def test_new(self): assert accept('live') == 'live'\n")
        self.put('next/src/product.py', 'def accept(value):\n return value\n')
        self.put(self.record, 'RETIRED = ' + repr({'test_a.A.test_old': self.reason}) + '\n')
        self.manifest = dict(run=[self.original, self.replacement], excluded=[], retirements=[
            dict(record=self.record, sha256=self.digest(self.record),
                 files={p:self.digest(p) for p in (self.original, self.replacement)})])
        self.manifest['retirement_checks']=[dict(
            original={'nodeid':self.old,'assertion':'assert False'},
            replacement={'nodeid':self.new,'assertion':"assert accept('live') == 'live'"},
            mutation={'path':'next/src/product.py','sha256':self.digest('next/src/product.py'),
                      'before':'return value','after':'return None'})]
        self.original_manifest=copy.deepcopy(self.manifest)

    def put(self, path, text):
        target = self.root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def digest(self, path):
        return hashlib.sha256((self.root/path).read_bytes()).hexdigest()

    def check(self):
        gate=self.gate
        if 'base_manifest' in inspect.signature(gate.inventory).parameters:
            return gate.inventory(self.root,self.manifest,base_manifest=self.original_manifest)
        return gate.inventory(self.root, self.manifest)

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
        self.assertFalse(self.check()['ok'],'changed accepted authority needs review; runner remains unexecuted')

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

    def test_worker_method_exclusion_keeps_every_other_case_live(self):
        gate=self.worker([])
        self.assertTrue(hasattr(gate,'excluded'),'worker has no exact unavailable-node contract')
        row={'nodeid':self.old,'reason':'owned unavailable fixture','evidence':{}}
        gate.excluded=[row]
        names=[self.old,self.live,self.new]
        session=types.SimpleNamespace(items=[types.SimpleNamespace(nodeid=n) for n in names])
        gate.pytest_collection_finish(session)
        self.assertEqual([i.nodeid for i in session.items],names[1:])
        self.assertEqual(gate.packet['excluded_methods'],[row])
        self.assertEqual(gate.packet['collected'],names)
        self.assertEqual(gate.packet['deselected'],[])

    def test_worker_requires_excluded_method_exactly_once(self):
        for names in ([self.live],[self.old,self.old,self.live]):
            gate=self.worker([])
            self.assertTrue(hasattr(gate,'excluded'))
            gate.excluded=[{'nodeid':self.old,'reason':'owned unavailable fixture','evidence':{}}]
            session=types.SimpleNamespace(items=[types.SimpleNamespace(nodeid=n) for n in names])
            with self.assertRaisesRegex(ValueError,'^method_exclusion_collection$'):
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

    def test_host_method_exclusion_receipt_cannot_receive_pass_credit(self):
        subject=self.gate
        self.assertIn('excluded',inspect.signature(subject.receipt_valid).parameters)
        row={'nodeid':self.old,'reason':'owned unavailable fixture','evidence':{}}
        packet=self.packet([self.live]);packet.update(excluded_methods=[row],collected=[self.old,self.live])
        self.assertTrue(subject.receipt_valid(packet,0,excluded=[row]))
        forged=self.packet([self.old,self.live]);forged.update(excluded_methods=[row],collected=[self.old,self.old,self.live])
        self.assertFalse(subject.receipt_valid(forged,0,excluded=[row]))

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

    def move(self, manifest, paths, reason='Needs real PostgreSQL through the direct or inherited database fixture.'):
        for path in paths:
            manifest['run'].remove(path)
            manifest['excluded'].append({'path':path,'reason':reason})

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
        self.move(manifest,removed,reason='Needs unavailable native fixture.')
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
        rows=[row for row in checked.get('method_exclusions',[]) if row['nodeid'].split('::')[0] in self.MIXED]
        self.assertEqual(len(rows),3,'only the three actually unavailable mixed-file nodes may be removed')

    def test_all_four_replacements_have_deciding_product_mutations(self):
        checked=self.checked(self.ROOT,self.current())
        self.assertTrue(checked['ok'],checked)
        self.assertEqual(len(checked.get('retirement_checks',[])),4,'named deciding assertions/product mutations missing')

    def test_missing_trusted_base_is_fail_closed(self):
        self.assertFalse(self.checked(self.ROOT,self.current(),{})['ok'],'missing independent base treated as valid')

    def test_changed_replacement_bytes_need_independent_base_review(self):
        root,manifest,base=self.retirement_fixture();binding=manifest['retirements'][0]
        replacement=next(p for p in binding['files'] if p.endswith('_v2.py'))
        path=root/replacement;path.write_text(path.read_text()+'\n# changed replacement identity\n')
        binding['files'][replacement]=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertFalse(self.checked(root,manifest,base)['ok'])

    def test_original_deciding_assertion_cannot_be_forged(self):
        manifest=self.current();manifest['retirement_checks'][0]['original']['assertion']='assert False'
        self.assertFalse(self.checked(self.ROOT,manifest)['ok'])

    def test_deciding_contract_cannot_cross_original_replacement_pairs(self):
        manifest=self.current();checks=manifest['retirement_checks']
        checks[1]['original'],checks[2]['original']=checks[2]['original'],checks[1]['original']
        self.assertFalse(self.checked(self.ROOT,manifest)['ok'])

    def test_method_path_outside_run_is_rejected_before_read(self):
        from unittest.mock import patch
        root,manifest,base=self.retirement_fixture();outside=root/'outside.py'
        outside.write_text('class Wrong:\n def test_hidden(self): assert False\n')
        manifest['method_exclusions']=[{'nodeid':'outside.py::Wrong::test_hidden','reason':'not a RUN method',
            'evidence':{'kind':'gated_env','source':'outside.py','sha256':hashlib.sha256(outside.read_bytes()).hexdigest(),'symbol':'Wrong','keys':['B01_PODMAN']}}]
        observed=[];read=Path.read_text
        def track(path,*args,**kwargs):
            if path.resolve()==outside.resolve():observed.append(str(path))
            return read(path,*args,**kwargs)
        with patch.object(Path,'read_text',track):checked=self.checked(root,manifest,base)
        self.assertFalse(checked['ok']);self.assertEqual(observed,[])

    def test_constant_replacement_is_refused_even_with_matching_base_pins(self):
        root,manifest,base=self.retirement_fixture();binding=manifest['retirements'][0]
        replacement=next(p for p in binding['files'] if p.endswith('_v2.py'))
        path=root/replacement;tree=ast.parse(path.read_text())
        for cls in tree.body:
            if isinstance(cls,ast.ClassDef):
                for node in cls.body:
                    if isinstance(node,ast.FunctionDef) and node.name.startswith('test_'):
                        node.body=ast.parse('self.assertTrue(True)').body
        path.write_text(ast.unparse(tree)+'\n');binding['files'][replacement]=hashlib.sha256(path.read_bytes()).hexdigest()
        base['retirements'][0]['files'][replacement]=binding['files'][replacement]
        for row in manifest['retirement_checks']:
            if row['replacement']['nodeid'].startswith(replacement+'::'):
                row['replacement']['assertion']='self.assertTrue(True)'
        self.assertFalse(self.checked(root,manifest,base)['ok'])


class FixtureEvidenceOracle(Subject, unittest.TestCase):
    """The static proof admits a real required fixture, not an import/reason."""
    checked=InventoryRepairOracle.checked
    def fixture(self, body, extra=''):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        fixture=root/'next/tests/pg_fixture.py';fixture.parent.mkdir(parents=True)
        fixture.write_text('import psycopg\nclass PgFixture:\n def setUp(self): self.db=psycopg.connect("required-test-db")\n')
        suite=root/'next/tests/test_pg.py';suite.write_text(body+extra)
        live=root/'next/tests/test_live.py';live.write_text('def test_live(): assert True\n')
        base={'run':['next/tests/test_pg.py','next/tests/test_live.py'],'excluded':[]}
        evidence={'kind':'postgres_fixture','source':'next/tests/pg_fixture.py',
            'sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),'symbol':'PgFixture.setUp'}
        current={'run':['next/tests/test_live.py'],'excluded':[{'path':'next/tests/test_pg.py','reason':'Needs PostgreSQL.','evidence':evidence}]}
        return root,current,base

    def test_required_inherited_postgres_fixture_is_checkable(self):
        root,manifest,base=self.fixture('from pg_fixture import PgFixture as Required\nclass PgTests(Required):\n def test_body(self): assert self.db\n')
        checked=self.checked(root,manifest,base)
        self.assertTrue(checked['ok'],checked)
        if 'transitions' in checked:self.assertTrue(checked['transitions'][0]['evidence_valid'])

    def test_unused_postgres_import_and_plain_case_are_not_proof(self):
        root,manifest,base=self.fixture('import psycopg\ndef test_body(): assert False\n')
        self.assertFalse(self.checked(root,manifest,base)['ok'])

    def test_fixture_proof_cannot_hide_mixed_plain_method(self):
        root,manifest,base=self.fixture('from pg_fixture import PgFixture\nclass PgTests(PgFixture):\n def test_body(self): assert self.db\n','def test_plain(): assert False\n')
        self.assertFalse(self.checked(root,manifest,base)['ok'])

    def test_stale_fixture_hash_and_overridden_setup_refuse(self):
        root,manifest,base=self.fixture('from pg_fixture import PgFixture\nclass PgTests(PgFixture):\n def setUp(self): pass\n def test_body(self): assert True\n')
        self.assertFalse(self.checked(root,manifest,base)['ok'])
        manifest['excluded'][0]['evidence']['sha256']='0'*64
        self.assertFalse(self.checked(root,manifest,base)['ok'])

    def test_dead_super_call_does_not_require_the_fixture(self):
        root,manifest,base=self.fixture('from pg_fixture import PgFixture\nclass PgTests(PgFixture):\n def setUp(self):\n  if False: super().setUp()\n def test_body(self): assert True\n')
        self.assertFalse(self.checked(root,manifest,base)['ok'])

    def test_stale_proof_hash_on_required_fixture_refuses(self):
        root,manifest,base=self.fixture('from pg_fixture import PgFixture\nclass PgTests(PgFixture):\n def test_body(self): assert self.db\n')
        manifest['excluded'][0]['evidence']['sha256']='0'*64
        self.assertFalse(self.checked(root,manifest,base)['ok'])

    def test_anchor_fixture_must_actually_connect(self):
        root,manifest,base=self.fixture('from pg_fixture import PgFixture\nclass PgTests(PgFixture):\n def test_body(self): assert True\n')
        path=root/'next/tests/pg_fixture.py';path.write_text('import psycopg\nclass PgFixture:\n def setUp(self): pass\n')
        manifest['excluded'][0]['evidence']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertFalse(self.checked(root,manifest,base)['ok'])

    def test_unrelated_setup_call_cannot_prove_parent_fixture(self):
        root,manifest,base=self.fixture('from pg_fixture import PgFixture\nclass PgTests(PgFixture):\n def setUp(self): unrelated.setUp()\n def test_body(self): assert True\n')
        self.assertFalse(self.checked(root,manifest,base)['ok'])


    def shadow_fixture(self, form):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        real=root/'next/tests/runtime_grants/support.py';real.parent.mkdir(parents=True)
        real.write_text('import psycopg\nclass Fixture:\n def setUp(self): self.db=psycopg.connect("required-test-db")\n')
        shadow=root/'next/tests/shadow/support.py';shadow.parent.mkdir(parents=True)
        shadow.write_text('class Fixture:\n def setUp(self): pass\n')
        suite='next/tests/shadow/test_shadow.py'
        body=('from support import Fixture\nclass TestShadow(Fixture):\n def test_offline(self): assert 2+2==4\n'
              if form=='from' else
              'import support as chosen\nclass TestShadow:\n def setUp(self): chosen.Fixture.setUp(self)\n def test_offline(self): assert 2+2==4\n')
        (root/suite).write_text(body)
        evidence={'kind':'postgres_fixture','source':str(real.relative_to(root)),
                  'sha256':hashlib.sha256(real.read_bytes()).hexdigest(),'symbol':'Fixture.setUp'}
        return root,suite,real,shadow,evidence

    def captured_support_origin(self, root, suite):
        # Execute only the unchanged worker's path setup, never a suite/helper.
        scripts=root/'.github/scripts';scripts.mkdir(parents=True,exist_ok=True)
        for name in ('pytest_next_worker.py','pytest_next_offline.py','pytest_u13_worker.py'):
            shutil.copyfile(SCRIPT.with_name(name),scripts/name)
        temporary=tempfile.TemporaryDirectory(dir=root);self.addCleanup(temporary.cleanup)
        source="""import json,os,runpy,sys
from pathlib import Path
from importlib.machinery import PathFinder
worker=Path(sys.argv[1]);suite=sys.argv[2];case=sys.argv[3]
sys.path[0]=str(worker.parent)
class Captured(BaseException): pass
def trace(frame,event,arg):
 if (event=='line' and frame.f_code.co_filename==str(worker)
     and frame.f_code.co_name=='main' and 'directories' in frame.f_locals
     and 'PYTHONPATH' in os.environ):
  spec=PathFinder.find_spec('support',sys.path)
  print(json.dumps({'origin':spec.origin if spec else None,'suite_imported':False}))
  raise Captured()
 return trace
sys.argv=[str(worker),'--suite',suite,'--output',case+'.json','--temporary',case]
sys.settrace(trace)
try: runpy.run_path(str(worker),run_name='__main__')
except Captured: pass
finally: sys.settrace(None)
"""
        env={key:os.environ[key] for key in ('PATH','LANG','LC_ALL') if key in os.environ}
        env['PYTHONDONTWRITEBYTECODE']='1'
        result=subprocess.run([sys.executable,'-B','-c',source,str(scripts/'pytest_next_worker.py'),
                               suite,str(Path(temporary.name)/'case')],cwd=root,env=env,
                              capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        observed=json.loads(result.stdout);self.assertFalse(observed['suite_imported'])
        return Path(observed['origin']).resolve()

    def test_actual_non_pg_shadow_cannot_be_excluded_by_pinned_pg_anchor(self):
        for form in ('from','delegation'):
            with self.subTest(form=form):
                root,suite,real,shadow,evidence=self.shadow_fixture(form)
                self.assertEqual(self.captured_support_origin(root,suite),shadow.resolve())
                self.assertFalse(self.gate.fixture_evidence(root,suite,evidence))
                # Change actual scope, not file-creation order. R2 still refuses ambiguity.
                reversal=str(real.parent.relative_to(root)/'test_reversal.py')
                (root/reversal).write_text((root/suite).read_text())
                self.assertEqual(self.captured_support_origin(root,reversal),real.resolve())
                self.assertFalse(self.gate.fixture_evidence(root,reversal,evidence))

    def test_uniquely_named_required_fixture_remains_admissible(self):
        for form in ('from','delegation'):
            with self.subTest(form=form):
                root,suite,real,shadow,evidence=self.shadow_fixture(form)
                self.assertFalse(self.gate.fixture_evidence(root,suite,evidence))
                shadow.unlink()  # Only the owned shadow changes; suite/binding stay exact.
                self.assertEqual(self.captured_support_origin(root,suite),real.resolve())
                self.assertTrue(self.gate.fixture_evidence(root,suite,evidence))


class SemanticOutcomeOracle(Subject,unittest.TestCase):
    def test_product_mutation_kill_requires_body_assertion_and_locus_observation(self):
        subject=self.gate
        self.assertTrue(callable(getattr(subject,'proof_killed',None)),'deciding mutation receipt validator absent')
        node='next/tests/test_a.py::A::test_deciding';path='next/src/product.py';sha='a'*64
        packet={'expected':[node],'selected':[node],'collected':[node],'deselected':[],
            'pytest_exit':1,'gate_exit':1,'reports':[{'nodeid':node,'when':phase,
                'outcome':'failed' if phase=='call' else 'passed','wasxfail':False,
                'assertion':phase=='call'} for phase in ('setup','call','teardown')],
            'observed_product':{'path':path,'sha256':sha,'source_unchanged':True,'loaded_source_matches':True,'calls':[{'qualname':'accept'}]}}
        self.assertTrue(subject.proof_killed(packet,1,node,path,sha))
        variants=[]
        for field in ('calls','loaded_source_matches','source_unchanged'):
            changed=copy.deepcopy(packet);changed['observed_product'][field]=[] if field=='calls' else False;variants.append(changed)
        for when in ('setup','teardown'):
            changed=copy.deepcopy(packet);changed['reports'][1]['when']=when;variants.append(changed)
        for value in ('skipped','passed'):
            changed=copy.deepcopy(packet);changed['reports'][1]['outcome']=value;variants.append(changed)
        changed=copy.deepcopy(packet);changed['reports'][1]['assertion']=False;variants.append(changed)
        for changed in variants:self.assertFalse(subject.proof_killed(changed,1,node,path,sha))


class ConsolidationOracle(Subject, unittest.TestCase):
    ROOT = SCRIPT.parents[2]
    MAIN = '66cba6a1f8a2a3787844e168ab5415cb96738a0d'
    # Exact original cohort absorbed at6adde71da4436465a8a09f7d4a6e56701dc66347.
    ORIGINAL_P1_IDS = (
        'next/tests/install/test_contracts.py::ContractOracle::test_all_three_complete_kinds_roundtrip',
        'next/tests/install/test_contracts.py::ContractOracle::test_closed_top_and_nested_fields',
        'next/tests/install/test_contracts.py::ContractOracle::test_complete_executed_tuple_deciding_signal_and_time_negatives',
        'next/tests/install/test_contracts.py::ContractOracle::test_journal_sequence_and_original_identity',
        'next/tests/install/test_contracts.py::ContractOracle::test_non_string_kind_is_a_typed_shape_refusal',
        'next/tests/install/test_contracts.py::ContractOracle::test_original_ref_digest_size_and_real_build_A_filename',
        'next/tests/install/test_contracts.py::ContractOracle::test_original_ref_rejects_non_json_digest_equality_objects',
        'next/tests/install/test_contracts.py::ContractOracle::test_paths_roots_ids_and_manual_parent_rules',
        'next/tests/install/test_contracts.py::ContractOracle::test_prior_observation_null_journal_requires_exact_evidence',
        'next/tests/install/test_contracts.py::ContractOracle::test_public_export_gate_and_schema_identity',
        'next/tests/install/test_contracts.py::ContractOracle::test_raw_noncanonical_duplicate_float_bom_unicode_and_limits',
        'next/tests/install/test_contracts.py::ContractOracle::test_ready_requires_actual_bound_descriptor_connection_checks_and_state',
        'next/tests/install/test_contracts.py::ContractOracle::test_receipt_binds_original_request_not_its_self_hash',
        'next/tests/install/test_contracts.py::ContractOracle::test_receipt_cross_identity_decides_in_body',
        'next/tests/install/test_contracts.py::ContractOracle::test_request_specific_size_bound_with_otherwise_complete_canonical_shape',
        'next/tests/install/test_contracts.py::ContractOracle::test_single_schema_unknown_field_decides_in_body',
        'next/tests/install/test_contracts.py::ContractOracle::test_termination_variants_and_invalid_sentinels',
        'next/tests/install/test_contracts.py::ContractOracle::test_versions_kinds_boolean_and_string_byte_bounds',
        'next/tests/install/test_planner.py::PlannerOracle::test_admitted_client_carrier_rejects_bad_argv_before_input_reader',
        'next/tests/install/test_planner.py::PlannerOracle::test_config_gap_and_opaque_dependency_failure_are_sanitized',
        'next/tests/install/test_planner.py::PlannerOracle::test_default_module_and_closed_carrier',
        'next/tests/install/test_planner.py::PlannerOracle::test_empty_or_external_profile_is_never_install_permission',
        'next/tests/install/test_planner.py::PlannerOracle::test_inherited_environment_and_wrong_operator_prevent_config_read',
        'next/tests/install/test_planner.py::PlannerOracle::test_mutating_operations_are_never_given_a_reader_or_capability',
        'next/tests/install/test_planner.py::PlannerOracle::test_no_test_double_or_second_port_imports_in_product',
        'next/tests/install/test_planner.py::PlannerOracle::test_original_member_reobserved_after_full_plan',
        'next/tests/install/test_planner.py::PlannerOracle::test_original_refs_and_entrypoint_are_cross_bound',
        'next/tests/install/test_planner.py::PlannerOracle::test_paired_manual_and_kos_use_same_shared_path_and_bytes',
        'next/tests/install/test_planner.py::PlannerOracle::test_production_unbound_precedes_request_schema_environment_and_readers',
        'next/tests/install/test_planner.py::PlannerOracle::test_real_UNBOUND_envelopes_and_GAP_refuse_before_port_or_config_reader',
        'next/tests/install/test_planner.py::PlannerOracle::test_root_original_reobserved_after_root_witness_measurement',
        'next/tests/install/test_planner.py::PlannerOracle::test_root_path_uid_mode_link_and_shared_root_drift',
        'next/tests/install/test_planner.py::PlannerOracle::test_wrong_target_uuid_operation_and_scope_refuse',
        'next/tests/install/test_read_meter.py::ComposedReadMeter::test_same_sealed_reader_counts_include_rc2_alias_on_all_four_positives',
        'next/tests/install/test_trust_repair.py::TrustRepairOracle::test_declared_root_alias_refuses_on_all_routes_and_targets',
        'next/tests/install/test_trust_repair.py::TrustRepairOracle::test_equal_identity_in_other_backend_and_zero_placeholders_remain_readonly',
        'next/tests/install/test_trust_repair.py::TrustRepairOracle::test_readonly_external_resources_remain_observations',
        'next/tests/install/test_trust_repair.py::TrustRepairOracle::test_readonly_prior_resource_relabel_refuses_plan_and_verify',
        'next/tests/install/test_tuple_revision.py::TupleRevisionOracle::test_current_lock_configs_and_three_descriptors_match_fixed_package_bytes',
        'next/tests/install/test_tuple_revision.py::TupleRevisionOracle::test_current_lock_with_old_config_operations_is_operations_mismatch',
        'next/tests/install/test_tuple_revision.py::TupleRevisionOracle::test_current_operations_with_stale_descriptors_is_contract_mismatch',
        'next/tests/install/test_tuple_revision.py::TupleRevisionOracle::test_every_member_and_original_request_receipt_journal_reference_is_closed',
        'next/tests/install/test_tuple_revision.py::TupleRevisionOracle::test_exact_current_tuple_admits_both_targets_without_effect_authority',
        'next/tests/install/test_tuple_revision.py::TupleRevisionOracle::test_fixture_index_binds_current_revision_and_original_history',
        'next/tests/install/test_tuple_revision.py::TupleRevisionOracle::test_historical_fixture_bytes_remain_exact_and_nonauthoritative',
        'next/tests/install/test_tuple_revision.py::TupleRevisionOracle::test_old_tuple_is_bad_shape_on_both_targets',
        'next/tests/install/test_tuple_revision.py::TupleRevisionOracle::test_wrong_schema_hash_or_any_member_size_is_contract_mismatch',
    )

    def test_initial_main_without_manifest_uses_exact_prior_policy(self):
        try:actual=self.gate.base_manifest_at(self.ROOT,self.MAIN)
        except ValueError as error:self.fail('named bootstrap main refused: '+str(error))
        expected=json.loads(subprocess.check_output(['git','show',InventoryRepairOracle.BASE+':.github/next-offline-suites.json'],cwd=self.ROOT))
        self.assertEqual(actual,expected)

    def test_unknown_or_invalid_base_never_bootstraps(self):
        for ref in ('0'*40,'../main','main'):
            with self.subTest(ref=ref),self.assertRaises(ValueError):self.gate.base_manifest_at(self.ROOT,ref)

    def test_current_main_files_have_no_inventory_holes(self):
        gate=self.gate
        checked=gate.inventory(self.ROOT,json.loads((self.ROOT/'.github/next-offline-suites.json').read_text()))
        self.assertTrue(checked['ok'],checked)
        self.assertIn('next/docs/runtime/rc0/test_oracle.py',checked['run'])
        for prefix in ('runtime/','runtime_driver_free/','runtime_egress/','runtime_factory/','runtime_identity/','runtime_keyword/','driver_privacy/'):
            found={str(p.relative_to(self.ROOT)) for p in (self.ROOT/'next/tests'/prefix).glob('test_*.py')}
            self.assertTrue(found)
            self.assertTrue(found<=set(checked['run']),(prefix,found-set(checked['run'])))

    def assert_install_coverage(self, root, manifest):
        gate=self.gate;nodes=[]
        for file in sorted((root/'next/tests/install').glob('test_*.py')):
            path=str(file.relative_to(root));self.assertIn(path,manifest['run'],'install file outside RUN: '+path)
            nodes.extend(gate.named_nodes(path,ast.parse(file.read_text())))
        self.assertEqual(len(nodes),47);self.assertEqual(len(set(nodes)),47)
        self.assertFalse(any(row['nodeid'] in nodes for row in manifest['method_exclusions']))

    def test_absorbed_p1_is_all_47_actual_methods(self):
        manifest=json.loads((self.ROOT/'.github/next-offline-suites.json').read_text())
        self.assert_install_coverage(self.ROOT,manifest)
        self.assertNotIn('  p1-install-oracle:',(self.ROOT/'.github/workflows/ci.yml').read_text())

    def install_fixture(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);destination=root/'next/tests/install'
        destination.parent.mkdir(parents=True)
        shutil.copytree(self.ROOT/'next/tests/install',destination,
                        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        manifest=json.loads((self.ROOT/'.github/next-offline-suites.json').read_text())
        return root,manifest

    def nonoriginal_fixture(self):
        root,manifest=self.install_fixture()
        path='next/tests/install/test_adapter_contract.py'
        node=path+'::LifecycleAdapterOracle::test_P2A14_default_binding_gap_precedes_candidate_access'
        self.assertNotIn(node,self.ORIGINAL_P1_IDS)
        original_hashes={p:hashlib.sha256((root/p).read_bytes()).hexdigest()
                         for p in {n.split('::')[0] for n in self.ORIGINAL_P1_IDS}}
        gate=self.gate
        before=sorted(n for p in (root/'next/tests/install').glob('test_*.py')
                      for n in gate.named_nodes(str(p.relative_to(root)),ast.parse(p.read_text())))
        file=root/path;text=file.read_text();declaration='class LifecycleAdapterOracle(unittest.TestCase):'
        self.assertIn(declaration,text)
        text='import os\n'+text.replace(declaration,
            "@unittest.skipUnless(os.environ.get('P1_NONORIGINAL_FIXTURE') == '1', 'Synthetic control fixture')\n"+declaration,1)
        file.write_text(text)
        after=sorted(n for p in (root/'next/tests/install').glob('test_*.py')
                     for n in gate.named_nodes(str(p.relative_to(root)),ast.parse(p.read_text())))
        self.assertEqual(before,after)
        for p,pin in original_hashes.items():self.assertEqual(hashlib.sha256((root/p).read_bytes()).hexdigest(),pin)
        row={'nodeid':node,'reason':'Synthetic whole-install exclusion control.',
             'evidence':{'kind':'gated_env','source':path,'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
                         'symbol':'LifecycleAdapterOracle','keys':['P1_NONORIGINAL_FIXTURE']}}
        self.assertTrue(gate.fixture_evidence(root,path,row['evidence'],node),'control needs independently valid evidence')
        return root,manifest,row

    def test_all_current_install_methods_admitted_without_exclusions(self):
        root,manifest,row=self.nonoriginal_fixture()
        self.assert_install_coverage(root,manifest)

    def test_nonoriginal_install_method_exclusion_is_refused(self):
        root,manifest,row=self.nonoriginal_fixture()
        self.assert_install_coverage(root,manifest)  # Its positive twin must be independently valid.
        run=manifest['run'][:];manifest['method_exclusions'].append(row)
        self.assertEqual(manifest['run'],run)
        with self.assertRaisesRegex(AssertionError,'current install method excluded: '+re.escape(row['nodeid'])):
            self.assert_install_coverage(root,manifest)
        manifest['method_exclusions'].remove(row)
        self.assert_install_coverage(root,manifest)

    def test_original_identity_cannot_be_replaced_at_same_count(self):
        root,manifest=self.install_fixture();node=self.ORIGINAL_P1_IDS[0]
        path,cls,name=node.split('::');file=root/path
        file.write_text(file.read_text().replace('def '+name+'(', 'def test_same_count_replacement(',1))
        with self.assertRaisesRegex(AssertionError,'original P1 nodes missing'):
            self.assert_install_coverage(root,manifest)

    def test_original_install_file_must_stay_in_run(self):
        root,manifest=self.install_fixture();manifest['run'].remove(self.ORIGINAL_P1_IDS[0].split('::')[0])
        with self.assertRaisesRegex(AssertionError,'install file outside RUN'):
            self.assert_install_coverage(root,manifest)

    def test_original_install_method_exclusion_is_refused(self):
        root,manifest=self.install_fixture();node=self.ORIGINAL_P1_IDS[0]
        manifest['method_exclusions'].append({'nodeid':node})
        with self.assertRaisesRegex(AssertionError,'current install method excluded: '+re.escape(node)):
            self.assert_install_coverage(root,manifest)

    def test_unlisted_install_growth_is_refused(self):
        root,manifest=self.install_fixture();(root/'next/tests/install/test_extra.py').write_text('def test_extra(): assert True\n')
        with self.assertRaisesRegex(AssertionError,'install file outside RUN'):
            self.assert_install_coverage(root,manifest)

    def test_listed_install_growth_preserves_original_cohort(self):
        root,manifest=self.install_fixture();path='next/tests/install/test_extra.py'
        (root/path).write_text('def test_extra(): assert True\n');manifest['run'].append(path)
        self.assert_install_coverage(root,manifest)

    def test_duplicate_current_install_identity_is_refused(self):
        root,manifest=self.install_fixture();path='next/tests/install/test_extra.py'
        (root/path).write_text('def test_extra(): assert True\ndef test_extra(): assert True\n');manifest['run'].append(path)
        with self.assertRaisesRegex(AssertionError,'duplicate current install IDs'):
            self.assert_install_coverage(root,manifest)

    def test_original_install_retirement_binding_is_refused(self):
        root,manifest=self.install_fixture();path=self.ORIGINAL_P1_IDS[0].split('::')[0]
        manifest['retirements'].append({'files':{path:'0'*64}})
        with self.assertRaisesRegex(AssertionError,'original P1 file bound for retirement'):
            self.assert_install_coverage(root,manifest)

    def test_dual_python_uses_shared_hashed_test_tools(self):
        workflow=(self.ROOT/'.github/workflows/next-offline.yml').read_text()
        self.assertIn("python: ['3.13.7', '3.14.0']",workflow)
        self.assertIn('next/requirements-ci-test.lock',workflow)
        self.assertIn('next-offline-receipts-${{ matrix.python }}',workflow)
        self.assertIn('--require-hashes',workflow)
        extra=(self.ROOT/'.github/next-offline-requirements.lock').read_text()
        for package in ('pytest','iniconfig','packaging','pluggy','pygments'):
            self.assertFalse(any(line.startswith(package+'==') for line in extra.splitlines()),package)
        self.assertIn('numpy==2.3.4',extra);self.assertIn('psycopg==3.2.9',extra)

    def test_mixed_postgres_class_does_not_hide_plain_class(self):
        root,manifest,base=FixtureEvidenceOracle.fixture(self,'from pg_fixture import PgFixture\nclass PgTests(PgFixture):\n def test_body(self): assert self.db\nclass Plain:\n def test_offline(self): assert True\n')
        row=manifest['excluded'][0];evidence=row['evidence'];path=row['path']
        gate=self.gate
        self.assertTrue(gate.fixture_evidence(root,path,evidence,path+'::PgTests::test_body'))
        self.assertFalse(gate.fixture_evidence(root,path,evidence,path+'::Plain::test_offline'))
        self.assertFalse(gate.fixture_evidence(root,path,evidence))

    def test_probe_and_loopback_exclusions_are_exact_and_source_bound(self):
        gate=self.gate;m=json.loads((self.ROOT/'.github/next-offline-suites.json').read_text())
        rows=[r for r in m['method_exclusions'] if '/c11_availability/' in r['nodeid']]
        self.assertEqual(len(rows),4)
        for row in rows:
            path=row['nodeid'].split('::')[0]
            self.assertTrue(gate.fixture_evidence(self.ROOT,path,row['evidence'],row['nodeid']),row)
            wrong=copy.deepcopy(row['evidence']);wrong['sha256']='0'*64
            self.assertFalse(gate.fixture_evidence(self.ROOT,path,wrong,row['nodeid']))
        probe=next(r for r in rows if r['evidence']['kind']=='postgres_probe_fixture')
        offline='next/tests/c11_availability/test_probe_bound.py::ProbeBoundTests::test_twenty_slow_callers_share_one_deadline_and_leave_no_thread'
        wrong=copy.deepcopy(probe['evidence']);wrong['symbol']='ProbeBoundTests.test_twenty_slow_callers_share_one_deadline_and_leave_no_thread'
        wrong['source']=offline.split('::')[0];wrong['sha256']=hashlib.sha256((self.ROOT/wrong['source']).read_bytes()).hexdigest()
        self.assertFalse(gate.fixture_evidence(self.ROOT,wrong['source'],wrong,offline))

    def test_unconditional_module_setup_delegation_requires_actual_connection(self):
        root,manifest,base=FixtureEvidenceOracle.fixture(self,'import pg_fixture as pg\nclass Delegate:\n def setUp(self): pg.PgFixture.setUp(self)\nclass PgTests(Delegate):\n def test_body(self): assert self.db\n')
        self.assertTrue(InventoryRepairOracle.checked(self,root,manifest,base)['ok'])
        file=root/'next/tests/test_pg.py'
        file.write_text(file.read_text().replace('pg.PgFixture.setUp(self)','unrelated.PgFixture.setUp(self)'))
        self.assertFalse(InventoryRepairOracle.checked(self,root,manifest,base)['ok'])


class UnbuiltUnitOracle(Subject, unittest.TestCase):
    ROOT=SCRIPT.parents[2]
    NODE='next/tests/test_future.py::test_future'
    LIVE='next/tests/test_future.py::test_live'

    def declaration(self):
        return {'nodeid':self.NODE,'reason':'E1 not built; waits RC3b; owner nemo',
            'evidence':{'kind':'unbuilt_unit','source':'next/tests/test_future.py','sha256':'a'*64,'symbol':'test_future'}}

    def packet(self,live=True):
        row=self.declaration();nodes=[self.NODE]+([self.LIVE] if live else [])
        p={'expected':list(nodes),'selected':list(nodes),'collected':list(nodes),'deselected':[],
           'pytest_exit':1,'gate_exit':0,'retired':[],'excluded_methods':[row],
           'reports':[{'nodeid':n,'when':w,'outcome':'failed' if n==self.NODE and w=='call' else 'passed',
             'wasxfail':False,'assertion':n==self.NODE and w=='call',
             'exception_type':'test_future.SurfaceAbsent' if n==self.NODE and w=='call' else ''}
              for n in nodes for w in ('setup','call','teardown')]}
        helper=getattr(self.gate,'declaration_observations',None)
        self.assertTrue(callable(helper),'shared executed-unavailable witness absent')
        p['declared_unavailable']=helper(p,[row])
        return p

    def admitted(self,p):return self.gate.receipt_valid(p,0,excluded=[self.declaration()])

    def test_expected_body_failure_executes_but_gets_no_pass_credit(self):
        p=self.packet();self.assertTrue(self.admitted(p));w=p['declared_unavailable'][0]
        self.assertTrue(w['executed']);self.assertTrue(w['valid']);self.assertEqual(w['pass_credit'],0)
        self.assertIn(self.NODE,p['selected']);self.assertEqual(p['reports'][1]['outcome'],'failed')

    def test_all_declared_suite_is_valid_without_fake_live_pass(self):
        p=self.packet(live=False);self.assertTrue(self.admitted(p));self.assertFalse(any(r['outcome']=='passed' and r['when']=='call' for r in p['reports']))

    def test_now_passing_declaration_is_red_and_still_zero_credit(self):
        p=self.packet();p['reports'][1].update(outcome='passed',assertion=False,exception_type='');p['pytest_exit']=0
        p['declared_unavailable']=self.gate.declaration_observations(p,[self.declaration()])
        self.assertFalse(self.admitted(p));self.assertEqual(p['declared_unavailable'][0]['pass_credit'],0)

    def test_wrong_exception_phase_skip_or_deselection_is_red(self):
        original=self.packet()
        for kind in ('setup','teardown','skip','wrong_type','missing','duplicate','deselected','selected-missing','duplicate-expected','null-type','witness'):
            p=copy.deepcopy(original)
            if kind in ('setup','teardown'):p['reports'][0 if kind=='setup' else 2]['outcome']='failed'
            elif kind=='skip':p['reports'][1]['outcome']='skipped'
            elif kind=='wrong_type':p['reports'][1]['exception_type']='builtins.AssertionError'
            elif kind=='missing':p['reports'].pop(1)
            elif kind=='duplicate':p['reports'].append(p['reports'][1])
            elif kind=='deselected':p['deselected']=[self.NODE]
            elif kind=='selected-missing':p['selected'].remove(self.NODE)
            elif kind=='duplicate-expected':p['expected'].append(self.NODE);p['selected'].append(self.NODE);p['collected'].append(self.NODE)
            elif kind=='null-type':p['reports'][1]['exception_type']=None
            p['declared_unavailable']=self.gate.declaration_observations(p,[self.declaration()])
            if kind=='witness':p['declared_unavailable'][0]['pass_credit']=1
            self.assertFalse(self.admitted(p),kind)

    def test_ordinary_c06_style_failure_is_not_waived(self):
        p=self.packet();p['reports'][4].update(outcome='failed',assertion=True,exception_type='builtins.AssertionError')
        self.assertFalse(self.admitted(p))

    def test_only_five_ruling_rows_are_approved_and_removal_is_allowed(self):
        g=self.gate;m=json.loads((self.ROOT/'.github/next-offline-suites.json').read_text())
        rows=[x for x in m['method_exclusions'] if x['evidence']['kind']=='unbuilt_unit']
        self.assertEqual(len(rows),5)
        self.assertTrue(all(x['reason']=='E1 not built; waits RC3b; owner nemo' for x in rows))
        self.assertTrue(g.inventory(self.ROOT,m)['ok'])
        changed=copy.deepcopy(m);next(x for x in changed['method_exclusions'] if x['evidence']['kind']=='unbuilt_unit')['reason']='forged'
        self.assertFalse(g.inventory(self.ROOT,changed)['ok'])
        changed=copy.deepcopy(m);changed['method_exclusions']=[x for x in changed['method_exclusions'] if x not in rows]
        self.assertTrue(g.inventory(self.ROOT,changed)['ok'],'E1 landing must be able to remove declarations')

    def test_real_worker_now_passing_declared_node_fails_gate(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        scripts=root/'.github/scripts';scripts.mkdir(parents=True)
        for name in ('pytest_next_offline.py','pytest_next_worker.py','pytest_u13_worker.py'):
            shutil.copyfile(SCRIPT.parent/name,scripts/name)
        suite=root/'next/tests/test_future.py';suite.parent.mkdir(parents=True)
        row=self.declaration();row['nodeid']='next/tests/test_future.py::test_future'
        results=[]
        for label,body in [('unbuilt',"raise SurfaceAbsent('not built')"),('now-passing','pass')]:
            suite.write_text('class SurfaceAbsent(AssertionError): pass\ndef test_future(): '+body+'\n')
            output=root/(label+'.json')
            env={k:v for k,v in os.environ.items() if not any(x in k.upper() for x in ('TOKEN','SECRET','PASSWORD','DSN','API_KEY'))}
            env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTEST_ADDOPTS='',PYTHONDONTWRITEBYTECODE='1')
            proc=subprocess.run([sys.executable,'-B',str(scripts/'pytest_next_worker.py'),'--suite','next/tests/test_future.py',
                '--output',str(output),'--temporary',str(root/(label+'-case')),'--excluded-json',json.dumps([row])],env=env,cwd=root,capture_output=True,text=True,timeout=30)
            self.assertTrue(output.is_file(),proc.stdout+proc.stderr)
            packet=json.loads(output.read_text());results.append((proc,packet))
            destination=os.environ.get('CORTEX_NEXT_ORACLE_OUTPUT')
            if destination:
                target=Path(destination);target.mkdir(parents=True,exist_ok=True)
                (target/(label+'.json')).write_text(json.dumps(packet,indent=2)+'\n');(target/(label+'.txt')).write_text(proc.stdout+proc.stderr)
        proc,packet=results[0];self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr);self.assertEqual(packet['pytest_exit'],1)
        proc,packet=results[1];self.assertEqual(proc.returncode,1,proc.stdout+proc.stderr);self.assertEqual(packet['pytest_exit'],0);self.assertEqual(packet['gate_exit'],1)
        self.assertEqual(packet['declared_unavailable'][0]['pass_credit'],0);self.assertEqual(packet['deselected'],[])
        self.assertEqual(packet['collected'],packet['selected']);self.assertTrue(packet['declared_unavailable'][0]['executed'])


class ExecutionPartitionOracle(Subject, unittest.TestCase):
    def test_real_workers_preserve_complete_ids_and_fail_closed_on_lost_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);scripts=root/'.github/scripts';scripts.mkdir(parents=True)
            for name in ('pytest_next_offline.py','pytest_next_worker.py','pytest_u13_worker.py'):
                shutil.copyfile(SCRIPT.parent/name,scripts/name)
            suite='next/tests/test_partition.py';path=root/suite;path.parent.mkdir(parents=True)
            path.write_text("import unittest\ndef test_factory(): pass\nclass Cases(unittest.TestCase): pass\ndef body(self): self.assertEqual(2+2,4)\nfor i in range(8): setattr(Cases,'test_'+str(i),body)\n")
            env=dict(os.environ,PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTEST_ADDOPTS='',PYTHONDONTWRITEBYTECODE='1')
            def run(label,extra):
                out=root/(label+'.json')
                result=subprocess.run([sys.executable,'-B',str(scripts/'pytest_next_worker.py'),'--suite',suite,
                    '--output',str(out),'--temporary',str(root/(label+'-tmp')),*extra],cwd=root,env=env,capture_output=True,text=True,timeout=30)
                self.assertTrue(out.is_file(),result.stdout+result.stderr)
                return result.returncode,json.loads(out.read_text())
            code,baseline=run('baseline',[]);self.assertEqual(code,0)
            parts=[run('part-'+str(i),['--partition-index',str(i),'--partition-count','4']) for i in range(4)]
            merge=getattr(self.gate,'merge_partitions',None);self.assertTrue(callable(merge),'BODY RED: complete execution partition reconciliation absent')
            combined=merge([p for c,p in parts],[c for c,p in parts],4)
            self.assertEqual(combined['selected'],baseline['selected']);self.assertEqual(len(combined['selected']),9)
            self.assertTrue(self.gate.receipt_valid(combined,0));self.assertEqual(len(combined['reports']),27)
            for fault in ('missing-part','duplicate-part','missing-node','missing-phase','changed-collection','changed-source','killed','skip','empty'):
                packets=copy.deepcopy([p for c,p in parts]);codes=[c for c,p in parts]
                if fault=='missing-part':packets.pop();codes.pop()
                elif fault=='duplicate-part':packets[1]=copy.deepcopy(packets[0])
                elif fault=='missing-node':packets[0]['selected'].pop()
                elif fault=='missing-phase':packets[0]['reports'].pop()
                elif fault=='changed-collection':packets[1]['full_collected'].pop()
                elif fault=='changed-source':packets[1]['source_sha256']='f'*64
                elif fault=='killed':codes[0]=124
                elif fault=='skip':packets[0]['reports'][1]['outcome']='skipped'
                elif fault=='empty':packets=[];codes=[]
                self.assertIsNone(merge(packets,codes,4),fault)

    def test_manifest_partitions_only_admitted_full_suites(self):
        root=SCRIPT.parents[2];m=json.loads((root/'.github/next-offline-suites.json').read_text())
        suite='next/tests/release/test_original_member_bridge.py';m['partitions']={suite:4}
        self.assertTrue(self.gate.inventory(root,m)['ok'])
        for value in ({suite:True},{suite:0},{suite:1},{suite:17},{'next/missing.py':4},[],{suite:'4'}):
            changed=copy.deepcopy(m);changed['partitions']=value
            self.assertFalse(self.gate.inventory(root,changed)['ok'],repr(value))

    def test_bridge_diagnostics_do_not_start_async_watchdog(self):
        tree=ast.parse((SCRIPT.parent/'pytest_next_worker.py').read_text())
        self.assertFalse(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
            and n.func.attr=='dump_traceback_later' for n in ast.walk(tree)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
