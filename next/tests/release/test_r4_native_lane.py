"""R4 native lane, budgets, ownership, evidence-only audit and gated RAW emission.

Accepted note v3 (Hale R4-MAP/OWN/BUDGET/TEMPLATE, Vera R4-T1/Q2/Q3). Fixture
level only: a fake engine stands in at the lane's one `_child` seam; no host,
container or native run. Nothing here is native or containment evidence.
"""
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_r3_inspector as r3
from test_unit1_red import product

runner = product('build.runner')
policy = product('build.policy')
engine = product('build.engine')
build_receipts = product('build.receipts')
receipts = product('receipts')
inspector = product('inspector')
contracts = product('contracts')
codec = product('codec')
owned_remove = product('build._owned_remove')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def lane_value(fx, *, cpu=500, memory=1 << 28, disk=1 << 20, absolute=600, caps=None):
    value = {'target': {'module_id': 'api', 'module_version': '0.2.001', 'oci_platform': 'linux/amd64'},
             'determinism': {'epoch': 0, 'environment': {'names': ['PYTHONHASHSEED']}},
             'resources': {'absolute_timeout_sec': absolute, 'cpu_millicores': cpu, 'memory_bytes': memory,
                           'disk_bytes': disk,
                           'phase_timeouts_sec': caps or {'input': 60, 'parser': 60, 'build': 120, 'export': 60,
                                                          'audit': 60, 'cleanup': 60}},
             'builder_requirements': {'architecture': 'amd64', 'engine': {'engine_id': 'podman', 'version': '5.4.0',
                                      'sha256': sha((fx.host / 'podman').read_bytes())},
                                      'toolchain': {'compiler_id': 'cpython', 'version': r3.VERSION, 'sha256': '1' * 64}},
             'admission_policy': {'policy_id': 'fixture', 'policy_sha256': 'e' * 64, 'qualification_scope': 'native'},
             'source': {'inventory': []}, 'dependencies': {}, 'recipe': {}}
    value['recipe'] = {'argv_template': policy.native_build_template(value)}
    return value


def verified(fx, value):
    return contracts.VerifiedBuildInput('0' * 64, contracts.freeze(value['target']), (), contracts.freeze({}),
                                        contracts.freeze(value), True,
                                        contracts.freeze({'release_policy': fx.profile, 'accepted_inputs': {}}),
                                        contracts._SEAL)


HOST = {'os': 'linux', 'arch': 'x86_64', 'uid': 1000, 'rootless': True, 'emulated': False, 'kernel': 'fixture'}


class Engine:
    """The fake engine at runner._child: records argv/timeouts, charges the account."""

    def __init__(self, lane, *, ps=None, fail=None, layer_media=r3.LAYER, clock=None, costs=None, chatty=None,
                 replace_store=False):
        self.lane, self.calls, self.timeouts = lane, [], []
        self.ps = list(ps or [[], []])
        self.fail, self.layer_media, self.clock, self.costs = fail, layer_media, clock, costs or {}
        self.chatty, self.replace_store = chatty or {}, replace_store

    def verb(self, argv):
        return 'save' if argv[8:10] == ['image', 'save'] else 'inspect' if argv[8:10] == ['image', 'inspect'] else argv[8]

    def __call__(self, argv, cwd, environment, timeout, account, stem):
        verb = self.verb(argv)
        self.calls.append(verb)
        self.timeouts.append((verb, timeout))
        cost = self.costs.get(verb, 0)
        if self.clock is not None:
            if cost > timeout:
                self.clock.now += timeout + runner.KILL_SECONDS
                raise r3.subprocess.TimeoutExpired(argv, timeout)
            self.clock.now += cost
        if self.fail == verb:
            raise r3.subprocess.TimeoutExpired(argv, timeout)
        stdout = self.respond(verb, argv, cwd)
        stdout += self.chatty.get(verb, b'')
        account.take(len(stdout))
        files = {}
        for name, raw in (('stdout', stdout), ('stderr', b''), ('journal', b'{}\n')):
            path = Path(str(stem) + {'stdout': '.out', 'stderr': '.err', 'journal': '.operation.jsonl'}[name])
            path.write_bytes(raw)
            files[name] = path
        return 0, stdout, files

    def respond(self, verb, argv, cwd):
        if verb == 'inspect':
            return json.dumps([{'Id': self.lane.base_config}]).encode()
        if verb == 'info':
            return json.dumps({'store': {'graphRoot': argv[3]}, 'host': {'security': {'rootless': True}},
                               'version': {'Version': '5.4.0'}}).encode()
        if verb == 'build':
            (Path(cwd) / 'image.iid').write_text('sha256:' + 'a' * 64)
            if self.replace_store:
                store = Path(argv[3])
                store.rename(store.with_name(store.name + '-held'))
                store.mkdir(mode=0o700)
                (store / 'foreign-owned-marker').write_text('same uid, different inode\n')
            return b''
        if verb == 'save':
            out = Path(argv[argv.index('--output') + 1])
            archive, *_ = r3.image()
            members, outer = artifacts_members(archive)
            out.mkdir()
            for name, raw in members.items():
                path = out / name
                path.parent.mkdir(parents=True, exist_ok=True)
                body = raw
                if name.startswith('blobs/') and MANIFEST_MARK in raw and self.layer_media != r3.LAYER:
                    body = raw.replace(r3.LAYER.encode(), self.layer_media.encode())
                path.write_bytes(body)
            if self.layer_media != r3.LAYER:
                # keep the descriptor chain consistent so only the media type differs
                rewrite_layout(out)
            return b''
        if verb == 'ps':
            ids = self.ps.pop(0) if self.ps else []
            return json.dumps([{'Id': value} for value in ids]).encode()
        if verb == 'unshare':
            script = argv.index(owned_remove.__file__)
            owned_remove.main(argv[script + 1:])
            return b''
        return b''


MANIFEST_MARK = r3.MANIFEST.encode()


def artifacts_members(archive):
    return product('artifacts').tar_members(archive, len(archive))


def rewrite_layout(out):
    index = json.loads((out / 'index.json').read_text())
    for blob in sorted((out / 'blobs' / 'sha256').iterdir()):
        raw = blob.read_bytes()
        if MANIFEST_MARK in raw:
            new = blob.with_name(sha(raw))
            blob.rename(new)
            index['manifests'][0]['digest'] = 'sha256:' + sha(raw)
            index['manifests'][0]['size'] = len(raw)
    (out / 'index.json').write_text(json.dumps(index))


class Lane:
    def __init__(self, case, **value_options):
        self.fx = r3.Fixture()
        case.addCleanup(self.fx.close)
        self.closure = self.fx.admit()
        self.value = lane_value(self.fx, **value_options)
        base_archive, _, self.base_config, _ = r3.image()
        self.fx.write(self.fx.root / 'base.tar', base_archive, 0o444)
        self.base = {'root': self.fx.root, 'row': {'path': 'base.tar', 'bytes': len(base_archive),
                                                   'sha256': sha(base_archive), 'mode': 0o444},
                     'config_sha256': self.base_config}
        self.parent, self.outputs, self.evidence = (self.fx.top / name for name in ('lanes', 'outputs', 'evidence'))
        for path in (self.parent, self.outputs, self.evidence):
            path.mkdir(mode=0o700)

    def materialize(self, dest):
        (Path(dest) / 'Dockerfile').write_text('FROM scratch\n')

    def run(self, fake, *, clock=None, slot='A'):
        with patch.object(runner, '_child', fake):
            return runner.native_lane(slot, verified(self.fx, self.value), self.materialize, self.base, self.closure,
                                      HOST, self.parent, self.outputs, self.evidence, clock=clock)

    def roots(self):
        return sorted(p.name for p in self.parent.iterdir())


class Template(unittest.TestCase):
    """Hale R4-TEMPLATE-001: one canonical suffix; anything else refuses before any engine op."""

    def test_canonical_template_binds_admitted_values(self):
        lane = Lane(self)
        template = policy.native_build_template(lane.value)
        self.assertEqual(template[:6], ['build', '--network=none', '--pull=never', '--no-cache', '--layers=false', '--force-rm'])
        self.assertIn('--platform=linux/amd64', template)
        self.assertEqual(template[-3:], ['--file=Dockerfile', '--iidfile=image.iid', '.'])
        self.assertEqual(template[template.index('--cpu-quota') + 1], '500000')

    def test_unknown_duplicate_contradictory_remote_or_changed_values_refuse_before_any_engine_op(self):
        base = policy.native_build_template(Lane(self).value)
        changes = {'unknown': base[:-1] + ['--cap-add=SYS_ADMIN', '.'],
                   'duplicate': base[:2] + ['--network=none'] + base[2:],
                   'contradictory': base[:-1] + ['--network=host', '.'],
                   'remote': base[:-1] + ['https://example.invalid/context.tar'],
                   'iidfile': [t if t != '--iidfile=image.iid' else '--iidfile=/tmp/x.iid' for t in base],
                   'resources': [t if t != '500000' else '900000' for t in base]}
        for name, template in changes.items():
            with self.subTest(case=name):
                lane = Lane(self)
                lane.value['recipe']['argv_template'] = template
                fake = Engine(lane)
                with self.assertRaises(ValueError) as caught:
                    lane.run(fake)
                self.assertEqual(str(caught.exception), 'native_recipe_uncontained')
                self.assertEqual(fake.calls, [])
                self.assertEqual(lane.roots(), [])


class Map(unittest.TestCase):
    """Hale R4-MAP-001: phases are aggregates of bound children; one finally cleanup."""

    def test_phases_bind_ordered_children_and_build_child_is_prefix_plus_template(self):
        lane = Lane(self)
        record = lane.run(Engine(lane))
        self.assertEqual(list(record['phases']), ['input', 'parser', 'build', 'export', 'audit', 'cleanup'])
        build = record['phases']['build']['children']
        self.assertEqual(len(build), 1)
        prefix = [str(lane.closure.engine), '--remote=false', '--root', record['identities']['store_id'],
                  '--runroot', record['identities']['runroot_id'], '--tmpdir']
        self.assertEqual(build[0]['argv'][:7], prefix)
        self.assertEqual(build[0]['argv'][8:], lane.value['recipe']['argv_template'])
        self.assertEqual(build[0]['cwd'], record['identities']['source_id'])
        self.assertEqual([c['argv'][8] for c in record['phases']['input']['children']], ['load', 'image'])
        for phase in record['phases']:
            self.assertTrue((lane.evidence / 'A' / (phase + '.children.json')).is_file())

    def test_second_child_timeout_fails_the_phase_and_cleanup_runs_once(self):
        lane = Lane(self)
        fake = Engine(lane, fail='inspect')
        record = lane.run(fake)
        self.assertEqual(record['result'], 'RED')
        self.assertEqual(record['lifecycle']['result'], 'RED')
        self.assertEqual(fake.calls, ['load', 'inspect', 'ps', 'ps', 'unshare'])
        self.assertEqual(fake.calls.count('unshare'), 1)

    def test_compressed_layer_refuses_before_any_archive_is_written(self):
        lane = Lane(self)
        record = lane.run(Engine(lane, layer_media=r3.LAYER + '+gzip'))
        self.assertEqual(record['result'], 'RED')
        self.assertNotIn('archive', record['outputs'])
        self.assertNotIn('audit', record['phases'])


class Own(unittest.TestCase):
    """Hale R4-OWN-001: discovery, exact-ID removal, absence, identity before deletion."""

    def test_orphan_container_never_journaled_is_discovered_and_removed_by_exact_id(self):
        lane = Lane(self)
        orphan = 'c' * 64
        fake = Engine(lane, ps=[[orphan], []])
        record = lane.run(fake)
        cleanup = record['phases']['cleanup']['children']
        self.assertEqual([c['argv'][8] for c in cleanup], ['ps', 'rm', 'ps', 'unshare'])
        self.assertEqual(cleanup[1]['argv'][9:], ['--force', '--time', '0', orphan])
        self.assertTrue(record['lifecycle']['cleanup']['complete'])
        self.assertEqual(lane.roots(), [])

    def test_container_still_listed_preserves_every_root(self):
        lane = Lane(self)
        fake = Engine(lane, ps=[['d' * 64], ['d' * 64]])
        record = lane.run(fake)
        self.assertNotIn('unshare', fake.calls)
        self.assertFalse(record['lifecycle']['cleanup']['complete'])
        self.assertEqual(len(lane.roots()), 5)

    def test_same_uid_store_replacement_is_never_deleted(self):
        lane = Lane(self)
        fake = Engine(lane, replace_store=True)
        record = lane.run(fake)
        self.assertNotIn('unshare', fake.calls)
        self.assertFalse(record['lifecycle']['cleanup']['complete'])
        markers = list(lane.parent.glob('*-store/foreign-owned-marker'))
        self.assertEqual(len(markers), 1)
        self.assertEqual(record['result'], 'RED')

    def test_namespace_removal_checks_identity_inside_the_deleting_process(self):
        directory = Path(r3.tempfile.mkdtemp(prefix='r4-owned-', dir=r3.private_base()))
        self.addCleanup(r3.shutil.rmtree, directory, True)
        root = directory / 'store'
        root.mkdir()
        held = root.stat()
        root.rename(directory / 'held')
        root.mkdir()
        (root / 'marker').write_text('replacement\n')
        with self.assertRaises(ValueError):
            owned_remove.remove(root, held.st_dev, held.st_ino)
        self.assertTrue((root / 'marker').exists())


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Budget(unittest.TestCase):
    """Hale R4-BUDGET-001: one lane time account and one byte account."""

    def test_phase_window_is_fixed_at_entry_and_children_get_only_the_rest(self):
        clock = Clock()
        deadline = engine.Deadline(100, clock=clock, reserve=20)
        deadline.enter_phase(10)
        clock.now = 9
        self.assertAlmostEqual(deadline.child_timeout(), 1)
        clock.now = 10
        with self.assertRaises(TimeoutError):
            deadline.child_timeout()

    def test_two_sub_cap_children_exceed_the_lane_byte_total_and_cleanup_keeps_its_reserve(self):
        account = engine.OutputAccount(1000, reserve=200)
        account.take(500)
        with self.assertRaises(OverflowError):
            account.take(400)
        with self.assertRaises(OverflowError):
            account.take(1)  # sticky for work
        account.cleanup()
        account.take(150)  # cleanup evidence still fits
        self.assertLessEqual(account.used, 1000)

    def test_lane_bytes_are_cumulative_across_children(self):
        lane = Lane(self, disk=4096)
        fake = Engine(lane, chatty={'load': b'x' * 1500, 'inspect': b'y' * 1500})
        record = lane.run(fake)
        self.assertEqual(record['result'], 'RED')
        self.assertNotIn('info', fake.calls)
        self.assertIn('unshare', fake.calls)  # cleanup still ran inside its byte reserve

    def test_children_of_one_phase_share_its_window_and_cleanup_stays_in_the_reserve(self):
        clock = Clock()
        caps = {'input': 10, 'parser': 60, 'build': 120, 'export': 60, 'audit': 60, 'cleanup': 30}
        lane = Lane(self, absolute=400, caps=caps)
        fake = Engine(lane, clock=clock, costs={'load': 9, 'inspect': 100, 'ps': 9, 'unshare': 1000})
        with patch.object(time, 'monotonic', clock):
            record = lane.run(fake, clock=clock)
        timeouts = dict(fake.timeouts[:2])
        self.assertAlmostEqual(timeouts['inspect'], 1)  # only the rest of the 10 s input window
        self.assertEqual(record['result'], 'RED')
        self.assertLessEqual(clock.now, 400)


class Audit(unittest.TestCase):
    """Vera R4-T1: evidence-only producer audit; real checks; runtime missing refuses."""

    def test_audit_never_touches_the_r01_session_and_runtime_stays_missing(self):
        lane = Lane(self)
        with patch.object(inspector, 'admit_inspection', side_effect=AssertionError('session path')) as admit, \
                patch.object(inspector, '_session', side_effect=AssertionError('session path')) as session, \
                patch.object(inspector, 'admit_session', side_effect=AssertionError('session path')) as grant:
            record = lane.run(Engine(lane))
        self.assertEqual((admit.call_count, session.call_count, grant.call_count), (0, 0, 0))
        self.assertEqual(record['classes']['runtime'][0], 'missing')
        self.assertEqual(record['result'], 'RED')

    def test_evidence_judge_uses_only_the_evidence_call(self):
        lane = Lane(self)
        record = {'closure_sha256': lane.closure.closure_sha256, 'request_sha256': '1' * 64, 'reply_sha256': '2' * 64,
                  'argv': ['x'], 'verdicts': [{'cache': 'a', 'equal': True}]}
        judge = runner._EvidenceInspection(lane.closure, lane.fx.profile, None, None)
        with patch.object(inspector, 'collect_inspection_evidence', return_value=record) as collect, \
                patch.object(inspector, '_session', side_effect=AssertionError('session path')), \
                patch.object(inspector, 'admit_session', side_effect=AssertionError('session path')), \
                patch.object(inspector, 'admit_inspection', side_effect=AssertionError('session path')):
            judge.check([{'cache': 'a'}])
            self.assertEqual(collect.call_count, 1)
            unequal = dict(record, verdicts=[{'cache': 'a', 'equal': False}])
            collect.return_value = unequal
            with self.assertRaises(ValueError) as caught:
                judge.check([{'cache': 'a'}])
        self.assertEqual(str(caught.exception), 'python_compilation_policy')
        self.assertEqual(judge.records, [record, unequal])

    def test_failed_image_check_fails_the_metadata_classes(self):
        lane = Lane(self)
        record = lane.run(Engine(lane))
        self.assertTrue(all(record['classes'][name][0] in ('pass', 'failed') for name in runner.METADATA_CLASSES))
        self.assertNotEqual(record['result'], 'PASS')


def qualification(lane, **change):
    descriptor = contracts.thaw(lane.closure.descriptor)['descriptor']
    doc = {'schema': receipts.QUALIFICATION_SCHEMA, 'architecture': 'amd64', 'platform': 'linux/amd64',
           'host': dict(HOST), 'engine': dict(lane.value['builder_requirements']['engine']),
           'engine_path': str(lane.closure.engine),
           'engine_environment': sorted(['CONTAINERS_CONF', 'CONTAINERS_STORAGE_CONF', 'HOME', 'PATH', 'TMPDIR',
                                         'XDG_RUNTIME_DIR']),
           'engine_config': {name: descriptor['engine_config'][name]['sha256'] for name in ('containers_conf', 'storage_conf')},
           'inspector': {'closure_sha256': lane.closure.closure_sha256,
                         'positive': {'status': 'pass', 'request_sha256': '1' * 64, 'reply_sha256': '2' * 64},
                         'negative': {'status': 'pass', 'request_sha256': '3' * 64, 'reply_sha256': '4' * 64}},
           'containment': {name: {'status': 'pass'} for name in receipts.PROBES}}
    doc.update(change)
    return doc


class Emission(unittest.TestCase):
    """Vera Q3: conditional RAW emission replaces the unconditional refusal."""

    def complete_lanes(self, doc):
        lane = Lane(self)
        inventory = [{'path': 'app/module.py', 'bytes': 5, 'sha256': '0' * 64, 'mode': 420, 'uid': 1001, 'gid': 1001, 'mtime': 0}]
        with patch.object(product('artifacts'), 'inspect_oci', return_value=((), inventory)), \
                patch.object(runner, '_runtime_smoke', return_value=('pass', 'fixture contract')), \
                patch.object(runner, '_child', Engine(lane, ps=[[], [], [], []])), \
                patch.object(build_receipts, 'verify_shared_receipt', side_effect=AssertionError('verified')) as verify:
            result = runner.native_lanes(verified(lane.fx, lane.value), lane.materialize, lane.base, lane.closure, HOST,
                                         lane.parent, lane.outputs, lane.evidence, codec.canonical_bytes(doc))
        self.assertEqual(verify.call_count, 0)
        return lane, result

    def test_complete_lanes_and_records_give_raw_bytes_only(self):
        lane = Lane(self)
        lane, result = self.complete_lanes(qualification(lane))
        self.assertEqual(result['result'], 'RAW')
        self.assertIsNone(result['verified'])
        value = json.loads(result['receipt'])
        self.assertEqual(value['qualification_scope'], 'native')
        product('codec').schema_validate(value, 'build-receipt.schema.json')
        self.assertNotEqual(value['builds']['A']['observed_builder']['store_id'],
                            value['builds']['B']['observed_builder']['store_id'])

    def test_missing_failed_or_mismatched_records_keep_the_emission_refusal(self):
        lane = Lane(self)
        bad = {'failed probe': qualification(lane, containment={**qualification(lane)['containment'], 'pids': {'status': 'fail'}}),
               'other arch': qualification(lane, architecture='arm64', platform='linux/arm64'),
               'other engine': qualification(lane, engine={**lane.value['builder_requirements']['engine'], 'sha256': 'f' * 64}),
               'probe only': qualification(lane, inspector={**qualification(lane)['inspector'],
                                                            'positive': {'status': 'missing', 'request_sha256': '1' * 64, 'reply_sha256': '2' * 64}})}
        for name, doc in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError) as caught:
                    self.complete_lanes(doc)
                self.assertEqual(str(caught.exception), 'r02_native_emission_unqualified')

    def test_runtime_missing_never_reaches_emission(self):
        lane = Lane(self)
        with patch.object(runner, '_child', Engine(lane, ps=[[], [], [], []])), \
                patch.object(build_receipts, 'emit', side_effect=AssertionError('emitted')) as emit:
            result = runner.native_lanes(verified(lane.fx, lane.value), lane.materialize, lane.base, lane.closure, HOST,
                                         lane.parent, lane.outputs, lane.evidence, codec.canonical_bytes(qualification(lane)))
        self.assertEqual(result['result'], 'RED')
        self.assertEqual(emit.call_count, 0)


class Order(unittest.TestCase):
    """Vera Q2: one session, only after the measured records match (R01 commit)."""

    def pending(self, lane):
        return inspector._Pending(lane.closure, contracts.freeze(lane.fx.profile), contracts.freeze(HOST), inspector._ADMITTED)

    def builds(self, lane):
        return {slot: {'observed_builder': {'engine': lane.value['builder_requirements']['engine']}} for slot in ('A', 'B')}

    def test_matched_records_mint_exactly_one_session(self):
        lane = Lane(self)
        matched = receipts.match_qualification(qualification(lane), self.pending(lane), lane.value, self.builds(lane))
        session = inspector.admit_session(self.pending(lane), matched)
        self.assertIsInstance(session, inspector.InspectionSession)

    def test_wrong_or_missing_records_cannot_create_a_session(self):
        lane = Lane(self)
        for change in ({'engine_path': '/usr/bin/other'}, {'inspector': {**qualification(lane)['inspector'], 'closure_sha256': '9' * 64}},
                       {'host': {**HOST, 'uid': 2000}}, {'engine_config': {'containers_conf': '8' * 64, 'storage_conf': '7' * 64}}):
            with self.subTest(change=sorted(change)):
                with self.assertRaises(ValueError) as caught:
                    receipts.match_qualification(qualification(lane, **change), self.pending(lane), lane.value, self.builds(lane))
                self.assertEqual(str(caught.exception), 'native_inspector_execution_unqualified')
        forged = receipts.MatchedQualification(lane.closure.closure_sha256, 'amd64', object())
        for bad in (None, forged, {'closure_sha256': lane.closure.closure_sha256}):
            with self.assertRaises(ValueError):
                inspector.admit_session(self.pending(lane), bad)

    def test_raw_bytes_or_evidence_are_never_sessions(self):
        lane = Lane(self)
        for bad in (b'{"schema":"cortex.build-receipt.v1"}', qualification(lane), {'verdicts': []}):
            with self.assertRaises(ValueError):
                inspector.admit_session(bad, None)

    def test_native_byte_entry_has_no_early_session_and_verify_uses_preflight_once(self):
        source = Path(receipts.__file__).read_text()
        entry = source[source.index('def verify_native_receipt_bytes'):]
        self.assertNotIn('admit_inspection', entry)
        self.assertNotIn('admit_inspection', source)
        self.assertEqual(source.count('pending = preflight(verified_input, policy)'), 1)
        self.assertEqual(source.count('inspection = admit_session(pending, matched)'), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
