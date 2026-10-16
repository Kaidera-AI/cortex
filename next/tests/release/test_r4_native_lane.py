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


def lane_value(fx, *, cpu=500, memory=1 << 28, disk=1 << 20, absolute=600, caps=None, base_config='0' * 64):
    value = {'target': {'module_id': 'api', 'module_version': '0.2.001', 'oci_platform': 'linux/amd64'},
             'determinism': {'epoch': 0, 'environment': {'names': ['PYTHONHASHSEED']},
                             'python': {'applicable': True, 'optimization': 0, 'final_root': '/app'}},
             'resources': {'absolute_timeout_sec': absolute, 'cpu_millicores': cpu, 'memory_bytes': memory,
                           'disk_bytes': disk,
                           'phase_timeouts_sec': caps or {'input': 60, 'parser': 60, 'build': 120, 'export': 60,
                                                          'audit': 60, 'cleanup': 60}},
             'builder_requirements': {'architecture': 'amd64', 'engine': {'engine_id': 'podman', 'version': '5.4.0',
                                      'sha256': sha((fx.host / 'podman').read_bytes())},
                                      'toolchain': {'compiler_id': 'cpython', 'version': r3.VERSION, 'sha256': '1' * 64}},
             'admission_policy': {'policy_id': 'fixture', 'policy_sha256': 'e' * 64, 'qualification_scope': 'native'},
             'source': {'inventory': []}, 'dependencies': {'base_images': [{'config_digest': base_config}]},
             'recipe': {}}
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
        base_archive, _, self.base_config, _ = r3.image()
        self.value = lane_value(self.fx, base_config=self.base_config, **value_options)
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
        caps = {'input': 30, 'parser': 60, 'build': 120, 'export': 60, 'audit': 60, 'cleanup': 30}
        lane = Lane(self, absolute=400, caps=caps)
        fake = Engine(lane, clock=clock, costs={'load': 9, 'inspect': 100, 'ps': 9, 'unshare': 1000})
        with patch.object(time, 'monotonic', clock):
            record = lane.run(fake, clock=clock)
        timeouts = dict(fake.timeouts[:2])
        # the rest of the 30 s input window minus the inspect child's own 10 s kill wait
        self.assertAlmostEqual(timeouts['inspect'], 11)
        self.assertEqual(record['result'], 'RED')
        self.assertLessEqual(clock.now, 400)
        self.assertEqual([c['exit'] for c in record['phases']['input']['children']], [0, None])


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
                  'argv': ['x'], 'children': [{'argv': ['nested']}], 'verdicts': [{'cache': 'a', 'equal': True}]}
        judge = runner._EvidenceInspection(lane.closure, lane.fx.profile, None, None)
        with patch.object(inspector, 'collect_inspection_evidence', return_value=record) as collect, \
                patch.object(inspector, '_session', side_effect=AssertionError('session path')), \
                patch.object(inspector, 'admit_session', side_effect=AssertionError('session path')), \
                patch.object(inspector, 'admit_inspection', side_effect=AssertionError('session path')):
            judge.check([{'cache': 'a'}])
            self.assertEqual(collect.call_count, 1)
            self.assertEqual([c['argv'] for c in judge.children], [['x'], ['nested']])  # call record, then real nested
            self.assertTrue({'started_at_unix_ns', 'finished_at_unix_ns', 'stdout', 'stderr', 'journal'} <= set(judge.children[0]))
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


ALLOWLIST = {'mounts': ['/', '/cortex-request.json', '/dev', '/proc', '/sys'], 'environment': ['HOME', 'PATH', 'container']}


def probe_closure(lane):
    """The test-only purpose=probe asset (R6-ASSET-001): the same image shape with the
    probe script at the inspector path, admitted through the SAME port, same engine."""
    if 'probe' not in vars(lane):
        script = Path(product('_probe').__file__).read_bytes()
        fx = lane.fx
        root = fx.top / 'probe-owned'
        root.mkdir(mode=0o755)
        archive, manifest, config, diff_id = r3.image(entries=r3.layer_entries(script=script))
        fx.write(root / 'inspector.tar', archive, 0o444)
        descriptor = fx.make_descriptor()
        descriptor.update(archive={'path': 'inspector.tar', 'bytes': len(archive), 'sha256': sha(archive), 'mode': 0o444},
                          manifest_sha256=manifest, config_sha256=config)
        descriptor['members'][1] = dict(role='inspector', **fx.row(inspector.STARTUP[4], script, 0o644))
        raw = codec.canonical_bytes(descriptor)
        lane.probe = inspector.admit_closure(raw, sha(raw), fx.profile, root, candidate_roots=[fx.candidate], script=script)
        lane.images = {lane.closure.image_digest: (fx.config, fx.diff_id), lane.probe.image_digest: (config, diff_id)}
    return lane.probe


def observations(lane):
    limits = lane.fx.profile['inspector']['limits']
    return {'network': {'probe': 'network', 'attempted': '1.1.1.1:443', 'interfaces': ['lo'], 'errno': 'ENETUNREACH'},
            'rootfs_write': {'probe': 'rootfs_write', 'target': '/opt/cortex-inspector/canary', 'errno': 'EROFS', 'written': False},
            'tmpfs': {'probe': 'tmpfs', 'target': '/tmp/cortex-probe', 'errno': 'EROFS', 'written': False, 'tmpfs_on_tmp': False},
            'pids': {'probe': 'pids', 'errno': 'EAGAIN', 'pids_max': limits['pids'], 'pids_current_at_denial': limits['pids'],
                     'pids_events_max_delta': 1, 'children': limits['pids'] - 2, 'reaped': True},
            'memory': {'probe': 'memory', 'memory_max': limits['memory_bytes'], 'attempted_bytes': 2 * limits['memory_bytes'],
                       'child_signal': 9, 'oom_kill_delta': 1},
            'mounts_env': {'probe': 'mounts_env', 'mounts': list(ALLOWLIST['mounts']),
                           'environment': list(ALLOWLIST['environment'])}}


class Wall:
    """time.time_ns plus an offset the fake engine advances (a run held to its deadline)."""

    def __init__(self):
        self.real, self.offset = time.time_ns, 0

    def __call__(self):
        return self.real() + self.offset


class QEngine:
    """The fake engine at inspector._engine for the R6 rows: real streams, the real
    controller core for the inspector image, a configured observation for the probe
    image; `time` is held to its deadline (or ends early), `survivor` leaves a container."""

    def __init__(self, lane, wall, *, observe=None, run_exit=None, time_mode='deadline', survivor=None, reply=None,
                 image_ids=None):
        self.lane, self.wall, self.calls = lane, wall, []
        self.observe = dict(observations(lane), **(observe or {}))
        self.run_exit, self.time_mode, self.survivor, self.reply = run_exit or {}, time_mode, survivor, reply
        self.image_ids, self.current = image_ids or {}, None

    def __call__(self, argv, environment, session, timeout, limit):
        verb = argv[8]
        self.calls.append(verb)
        code, out = self.respond(verb, argv, timeout)
        files = {}
        for key, raw in (('stdout', out), ('stderr', b''), ('journal', b'{"fixture":true}\n')):
            path = Path(session) / ('fixture-' + str(len(self.calls)) + '.' + key)
            path.write_bytes(raw)
            files[key] = path
        return code, out, files

    def respond(self, verb, argv, timeout):
        if verb == 'image':
            config, diff_id = self.lane.images[argv[-1]]
            config = self.image_ids.get(argv[-1], config)
            return 0, json.dumps([{'Id': config, 'Os': 'linux', 'Architecture': 'amd64', 'RootFS': {'Layers': [diff_id]}}]).encode()
        if verb == 'run':
            image = argv[argv.index('--entrypoint') + 2]
            request = Path(next(a for a in argv if a.startswith('type=bind,src=')).split(',')[1][4:]).read_bytes()
            if image == self.lane.closure.image_digest:
                value = json.loads(r3.core.respond(request))
                value['abi']['machine'] = 'x86_64'
                reply = codec.canonical_bytes(value)
                return 0, self.reply(reply, request) if self.reply else reply
            self.current = probe = json.loads(request)['probe']
            if probe == 'time':
                if self.time_mode == 'early':
                    return 1, b''
                self.wall.offset += int(timeout * 10 ** 9) + 5 * 10 ** 8
                raise r3.subprocess.TimeoutExpired(list(argv), timeout)
            return self.run_exit.get(probe, 0), codec.canonical_bytes(self.observe[probe])
        if verb == 'ps':
            return 0, (b'f' * 64 + b'\n') if self.survivor is not None and self.survivor == self.current else b''
        return 0, b''


def originals(lane, *, negative_same=False, **engine):
    """Every R6 row's originals produced by the REAL collectors (collect_inspection_evidence,
    run_probe -> inspector._Children) through the fake engine; returns (files, rows)."""
    import tempfile
    probe = probe_closure(lane)
    wall = Wall()
    fake = QEngine(lane, wall, **engine)
    work = Path(tempfile.mkdtemp(dir=lane.fx.top))
    (work / runner.QUALIFICATION_DIR).mkdir()
    cases = r3.rows(('m', b'x = 1\n'), ('n', b'y = 2\n'))
    flipped = [dict(cases[0], cache_bytes=cases[0]['cache_bytes'][:-1] + bytes([cases[0]['cache_bytes'][-1] ^ 1])), cases[1]]
    rows = {}
    with patch.object(inspector, '_engine', fake), patch.object(time, 'time_ns', wall):
        for kind, given in (('positive', cases), ('negative', cases if negative_same else flipped)):
            (work / 'q' / kind).mkdir()
            nested = []
            try:
                request = inspector.collect_inspection_evidence(lane.closure, lane.fx.profile, given,
                                                                evidence_dir=work / 'q' / kind, history=nested)['request']
            except ValueError:
                request = inspector._request(lane.closure, lane.fx.profile, given)[0]
            rows[kind] = runner._qualification_row(work, kind, request, nested)
        for name in receipts.PROBES:
            (work / 'q' / name).mkdir()
            fake.current = name
            result = inspector.run_probe(probe, lane.fx.profile, name, evidence_dir=work / 'q' / name)
            rows[name] = runner._qualification_row(work, name, result['request'], result['children'])
    files = {str(p.relative_to(work)): p.read_bytes() for p in work.rglob('*') if p.is_file()}
    return files, rows


def runner_python():
    import sys
    return sys.executable


def qualification(lane, *, files=None, **change):
    descriptor = contracts.thaw(lane.closure.descriptor)['descriptor']
    built, rows = originals(lane)
    doc = {'schema': receipts.QUALIFICATION_SCHEMA, 'architecture': 'amd64', 'platform': 'linux/amd64',
           'host': dict(HOST), 'engine': dict(lane.value['builder_requirements']['engine']),
           'engine_path': str(lane.closure.engine),
           'engine_config': {name: descriptor['engine_config'][name]['sha256'] for name in ('containers_conf', 'storage_conf')},
           'inspector': {'closure_sha256': lane.closure.closure_sha256, 'positive': rows['positive'], 'negative': rows['negative']},
           'probe_image': lane.probe.image_digest, 'containment': {name: rows[name] for name in receipts.PROBES},
           'cleanup_interpreter': runner_python(),
           'cleanup_script': {'path': owned_remove.__file__, 'sha256': sha(Path(owned_remove.__file__).read_bytes())}}
    doc.update(change)
    # the R6 operator places the cited originals in the evidence root
    for name, raw in (built if files is None else files).items():
        target = lane.evidence / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return doc


def pending(lane, **change):
    probe_closure(lane)
    fields = {'probe': lane.probe, 'allowlist': contracts.freeze(ALLOWLIST), **change}
    return inspector._Pending(lane.closure, contracts.freeze(lane.fx.profile), contracts.freeze(HOST), inspector._ADMITTED,
                              **fields)


class Emission(unittest.TestCase):
    """Vera Q3: conditional RAW emission replaces the unconditional refusal."""

    def complete_lanes(self, make):
        lane = Lane(self)
        doc = make(lane)
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
        lane, result = self.complete_lanes(qualification)
        self.assertEqual(result['result'], 'RAW')
        self.assertIsNone(result['verified'])
        value = json.loads(result['receipt'])
        self.assertEqual(value['qualification_scope'], 'native')
        product('codec').schema_validate(value, 'build-receipt.schema.json')
        self.assertNotEqual(value['builds']['A']['observed_builder']['store_id'],
                            value['builds']['B']['observed_builder']['store_id'])

    def test_missing_failed_or_mismatched_records_keep_the_emission_refusal(self):
        def missing(lane):
            files = dict(originals(lane)[0])
            del files['q/positive.request']
            return qualification(lane, files=files)
        bad = {'missing original': missing,
               'other arch': lambda lane: qualification(lane, architecture='arm64', platform='linux/arm64'),
               'other engine': lambda lane: qualification(lane, engine={**lane.value['builder_requirements']['engine'], 'sha256': 'f' * 64}),
               'probe children missing': lambda lane: qualification(lane, containment={
                   **{n: r for n, r in originals(lane)[1].items() if n in receipts.PROBES},
                   'pids': {'request': 'q/pids.request', 'children': None}})}
        for name, make in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError) as caught:
                    self.complete_lanes(make)
                self.assertEqual(str(caught.exception), 'r02_native_emission_unqualified')

    def emit_value(self, change):
        lane, result = self.complete_lanes(qualification)
        value = json.loads(result['receipt'])
        change(value)
        return lambda: build_receipts.emit(verified(lane.fx, lane.value), value['builds'], value['audit'], value['evidence'],
                                           value['custody'], value['cleanup'], lambda path: (lane.outputs / path).read_bytes(),
                                           lambda path: (lane.evidence / path).read_bytes(), None)

    def test_shortened_required_class_set_refuses_even_when_consistent(self):
        def change(value):
            value['audit']['required_classes'] = [c for c in value['audit']['required_classes'] if c != 'runtime']
            for slot in ('A', 'B'):
                del value['audit']['reports'][slot]['runtime']
        with self.assertRaises(ValueError) as caught:
            self.emit_value(change)()
        self.assertEqual(str(caught.exception), 'r02_native_emission_unqualified')

    def test_missing_record_refuses_before_any_reader(self):
        lane, result = self.complete_lanes(qualification)
        value = json.loads(result['receipt'])
        value['evidence']['inventory'] = [r for r in value['evidence']['inventory'] if r['role'] != 'native.qualification']
        reads = []
        def reader(path):
            reads.append(path)
            raise AssertionError('reader called')
        with self.assertRaises(ValueError) as caught:
            build_receipts.emit(verified(lane.fx, lane.value), value['builds'], value['audit'], value['evidence'],
                                value['custody'], value['cleanup'], reader, reader, None)
        self.assertEqual(str(caught.exception), 'r02_native_emission_unqualified')
        self.assertEqual(reads, [])

    def test_required_class_list_must_be_the_full_set_even_with_full_reports(self):
        def change(value):
            value['audit']['required_classes'] = [c for c in value['audit']['required_classes'] if c != 'runtime']
        with self.assertRaises(ValueError) as caught:
            self.emit_value(change)()
        self.assertEqual(str(caught.exception), 'r02_native_emission_unqualified')

    def test_incomplete_top_level_cleanup_alone_refuses(self):
        for name, change in {'flag': lambda v: v['cleanup'].__setitem__('complete', False),
                             'remaining': lambda v: v['cleanup'].__setitem__('remaining_owned_resources', ['x'])}.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError) as caught:
                    self.emit_value(change)()
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
    """Vera Q2 + Cox R4-R01-001/002: results re-derived from originals, inside the
    verifier only, bound to this verification; one session."""

    def pending(self, lane):
        return pending(lane)

    def builds(self, lane):
        return {slot: {'observed_builder': {'engine': lane.value['builder_requirements']['engine']}} for slot in ('A', 'B')}

    def match(self, lane, pending=None, files=None, **change):
        files = originals(lane)[0] if files is None else files
        doc = qualification(lane, files=files, **change)
        inventory = [{'role': 'native.qualification.original', 'path': p, 'media_type': 'application/octet-stream',
                      'bytes': len(raw), 'sha256': sha(raw)} for p, raw in files.items()]
        return receipts._match_qualification(doc, pending or self.pending(lane), lane.value, self.builds(lane),
                                             dict(files), inventory, 'd' * 64, 'e' * 64)

    def refuses(self, fn, *args, **kw):
        with patch.object(inspector, '_session', side_effect=AssertionError('session')) as session:
            with self.assertRaises(ValueError) as caught:
                fn(*args, **kw)
        self.assertEqual(session.call_count, 0)
        return str(caught.exception)

    def test_matched_originals_mint_exactly_one_session(self):
        lane = Lane(self)
        pending = self.pending(lane)
        files = originals(lane)[0]
        matched, found = self.match(lane, pending, files=files)
        # every original the collectors produced is consumed: requests, child records, streams
        self.assertEqual(set(found), set(files))
        self.assertLess(len(receipts.qualification_paths(qualification(lane))), len(found))
        self.assertIsInstance(inspector.admit_session(pending, matched), inspector.InspectionSession)

    def test_results_are_rederived_from_the_originals(self):
        lane = Lane(self)

        def unequal(reply, request):
            value = json.loads(reply)
            value['verdicts'][1]['equal'] = False
            return codec.canonical_bytes(value)

        def child(files, kind, index, change):
            files = dict(files)
            children = json.loads(files['q/' + kind + '.children.json'])
            change(children[index])
            files['q/' + kind + '.children.json'] = codec.canonical_bytes(children)
            return files

        good = originals(lane)[0]
        cases = {'positive not equal': originals(lane, reply=unequal)[0],
                 'negative unchanged': originals(lane, negative_same=True)[0],
                 'probe allowed': originals(lane, observe={'network': {**observations(lane)['network'], 'errno': 'CONNECTED'}})[0],
                 'probe exit': originals(lane, run_exit={'rootfs_write': 3})[0],
                 'wrong flags': child(good, 'positive', 2, lambda c: c.__setitem__(
                     'argv', [t if t != '--network=none' else '--network=host' for t in c['argv']])),
                 'wrong environment': child(good, 'tmpfs', 2, lambda c: c.__setitem__('environment_names', ['HOME', 'PATH']))}
        swapped = dict(good)
        swapped['q/positive.children.json'], swapped['q/negative.children.json'] = \
            swapped['q/negative.children.json'], swapped['q/positive.children.json']
        cases['swapped replies'] = swapped
        missing = dict(good)
        del missing['q/tmpfs.children.json']
        cases['missing original'] = missing
        for name, files in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.refuses(self.match, lane, files=files), 'native_inspector_execution_unqualified')

    def test_context_identity_mismatches_refuse(self):
        lane = Lane(self)
        for change in ({'engine_path': '/usr/bin/other'}, {'inspector': {**qualification(lane)['inspector'], 'closure_sha256': '9' * 64}},
                       {'host': {**HOST, 'uid': 2000}}, {'engine_config': {'containers_conf': '8' * 64, 'storage_conf': '7' * 64}},
                       {'probe_image': lane.closure.image_digest}):
            with self.subTest(change=sorted(change)):
                self.assertEqual(self.refuses(self.match, lane, **change), 'native_inspector_execution_unqualified')

    def test_a_match_cannot_be_reused_for_another_verification(self):
        lane = Lane(self)
        matched, _ = self.match(lane)
        other = self.pending(lane)
        self.refuses(inspector.admit_session, other, matched)
        forged = receipts.MatchedQualification(other, 'd' * 64, 'e' * 64, lane.closure.closure_sha256, object())
        for bad in (None, forged, {'closure_sha256': lane.closure.closure_sha256}):
            self.refuses(inspector.admit_session, other, bad)

    def test_fixture_scope_input_never_reaches_a_session(self):
        lane = Lane(self)
        value = dict(lane.value, admission_policy={**lane.value['admission_policy'], 'qualification_scope': 'fixture'})
        fixture = contracts.VerifiedBuildInput('0' * 64, contracts.freeze(value['target']), (), contracts.freeze({}),
                                               contracts.freeze(value), False,
                                               contracts.freeze({'release_policy': lane.fx.profile, 'accepted_inputs': {}}),
                                               contracts._SEAL)
        host = dict(HOST)
        self.assertEqual(self.refuses(inspector.preflight, fixture, {'inspector_capability': lane.closure, 'inspector_host': host}),
                         'native_inspector_execution_unqualified')
        self.assertFalse(hasattr(receipts, 'match_qualification'))

    def test_raw_bytes_or_evidence_are_never_sessions(self):
        lane = Lane(self)
        for bad in (b'{"schema":"cortex.build-receipt.v1"}', qualification(lane), {'verdicts': []}):
            self.refuses(inspector.admit_session, bad, None)

    def test_native_byte_entry_has_no_early_session_and_verify_uses_preflight_once(self):
        source = Path(receipts.__file__).read_text()
        self.assertNotIn('admit_inspection', source)
        self.assertEqual(source.count('pending = preflight(verified_input, policy)'), 1)
        self.assertEqual(source.count('inspection = admit_session(pending, matched)'), 1)


class ChildBinding(unittest.TestCase):
    """Hale R4-MAP-001(c) + v3 tamper controls on the owner helper."""

    def receipt(self):
        lane = Lane(self)
        inventory = [{'path': 'app/module.py', 'bytes': 5, 'sha256': '0' * 64, 'mode': 420, 'uid': 1001, 'gid': 1001, 'mtime': 0}]
        with patch.object(product('artifacts'), 'inspect_oci', return_value=((), inventory)), \
                patch.object(runner, '_runtime_smoke', return_value=('pass', 'fixture contract')), \
                patch.object(runner, '_child', Engine(lane, ps=[[], [], [], []])):
            result = runner.native_lanes(verified(lane.fx, lane.value), lane.materialize, lane.base, lane.closure, HOST,
                                         lane.parent, lane.outputs, lane.evidence, codec.canonical_bytes(qualification(lane)))
        value = json.loads(result['receipt'])
        measured = {row['path']: (lane.evidence / row['path']).read_bytes() for row in value['evidence']['inventory']}
        doc = json.loads(measured['qualification.json'])
        return lane, value, measured, doc

    def tamper(self, lane, value, measured, phase, change):
        build = value['builds']['A']
        op = next(op for op in build['operations'] if op['phase'] == phase)
        path = op['evidence']['path'][:-5] + '.children.json'
        children = json.loads(measured[path])
        change(children)
        raw = codec.canonical_bytes(children)
        measured = dict(measured, **{path: raw})
        inventory = [dict(row, bytes=len(raw), sha256=sha(raw)) if row['path'] == path else row
                     for row in value['evidence']['inventory']]
        return build, measured, inventory

    def test_unmodified_children_bind_and_tampering_refuses(self):
        lane, value, measured, doc = self.receipt()
        receipts._native_children(value['builds']['A'], measured, value['evidence']['inventory'], doc, lane.value)
        cases = {'root': ('build', lambda c: c[0]['argv'].__setitem__(3, '/elsewhere-store')),
                 'cwd': ('build', lambda c: c[0].__setitem__('cwd', '/elsewhere')),
                 'environment': ('input', lambda c: c[0].__setitem__('environment_names', ['HOME'])),
                 'result': ('parser', lambda c: c[0].__setitem__('exit', 1)),
                 'tmpdir': ('input', lambda c: c[0]['argv'].__setitem__(7, '/foreign-tmp')),
                 'empty input': ('input', lambda c: c.clear()),
                 'reordered input': ('input', lambda c: c.reverse()),
                 'stream digest': ('export', lambda c: c[0]['stdout'].__setitem__('sha256', '0' * 64)),
                 'missing metadata': ('export', lambda c: c[0].pop('journal'))}
        for name, (phase, change) in cases.items():
            with self.subTest(case=name):
                build, tampered, inventory = self.tamper(lane, value, measured, phase, change)
                with self.assertRaises(ValueError) as caught:
                    receipts._native_children(build, tampered, inventory, doc, lane.value)
                self.assertEqual(str(caught.exception), 'native_child_evidence')


class OwnerRound2(unittest.TestCase):
    """Cox's R01 round-2 deciding REDs, ported from his owner packet (cox-r4-r01-r2-20261016):
    every case must stop before the ordinary native verifier's session boundary."""

    class Reached(Exception):
        pass

    def candidate(self):
        lane, value, measured, doc = ChildBinding.receipt(self)
        checked = verified(lane.fx, lane.value)
        policy = {'receipt_sha256': codec.sha256(codec.canonical_bytes(value)),
                  'policy_sha256': lane.value['admission_policy']['policy_sha256'], 'input_sha256': checked.input_sha256,
                  'qualification_scope': 'native', 'inspector_capability': lane.closure, 'inspector_host': HOST,
                  'probe_capability': lane.probe, 'containment_allowlist': ALLOWLIST,
                  'required_classes': list(receipts.CLASSES)}
        return lane, value, measured, doc, checked, policy

    def sessions(self, candidate):
        lane, value, measured, doc, checked, policy = candidate
        for row in value['evidence']['inventory']:
            raw = measured[row['path']]
            row.update(bytes=len(raw), sha256=codec.sha256(raw))
        index = value['evidence']['index']
        raw = codec.canonical_bytes({'inventory': value['evidence']['inventory']})
        measured[index['path']] = raw
        index.update(bytes=len(raw), sha256=codec.sha256(raw))
        raw = codec.canonical_bytes(value)
        policy['receipt_sha256'] = codec.sha256(raw)
        def no_output(*args):
            raise AssertionError('candidate outputs read before the session boundary')
        with patch.object(inspector, '_session', side_effect=self.Reached) as session:
            try:
                receipts.verify_native_receipt_bytes(raw, checked, no_output, measured.__getitem__, policy)
            except (self.Reached, contracts.ReceiptRefusal):
                pass
        return session.call_count

    def change_request(self, c, change):
        for kind in ('positive', 'negative'):
            path = 'q/' + kind + '.request'
            request = json.loads(c[2][path])
            change(request)
            raw = codec.canonical_bytes(request)
            c[2][path] = raw
            # the run's reply stream answers the changed request (the join stays intact)
            children = json.loads(c[2]['q/' + kind + '.children.json'])
            stream = children[2]['stdout']
            reply = json.loads(c[2][stream['path']])
            reply['request_sha256'] = codec.sha256(raw)
            c[2][stream['path']] = codec.canonical_bytes(reply)
            stream.update(bytes=len(c[2][stream['path']]), sha256=codec.sha256(c[2][stream['path']]))
            c[2]['q/' + kind + '.children.json'] = codec.canonical_bytes(children)

    def children(self, c, phase):
        op = next(op for op in c[1]['builds']['A']['operations'] if op['phase'] == phase)
        path = op['evidence']['path'][:-5] + '.children.json'
        return op, path, json.loads(c[2][path])

    def test_control_corrected_candidate_reaches_one_session(self):
        self.assertEqual(self.sessions(self.candidate()), 1)

    def test_malformed_request_row_cannot_admit(self):
        c = self.candidate()
        self.change_request(c, lambda request: request['rows'][0].pop('source_b64'))
        self.assertEqual(self.sessions(c), 0)

    def test_foreign_request_abi_cannot_admit(self):
        c = self.candidate()
        self.change_request(c, lambda request: request.__setitem__('abi', {'version': '0.0.0', 'magic': '00000000'}))
        self.assertEqual(self.sessions(c), 0)

    def test_request_optimization_outside_profile_cannot_admit(self):
        c = self.candidate()
        self.change_request(c, lambda request: request.__setitem__('optimization', 2))
        self.assertEqual(self.sessions(c), 0)

    def test_filtered_cleanup_discovery_cannot_admit(self):
        c = self.candidate()
        _, path, rows = self.children(c, 'cleanup')
        rows[0]['argv'] = rows[0]['argv'][:8] + ['ps', '--all', '--filter', 'name=foreign', '--format', 'json']
        c[2][path] = codec.canonical_bytes(rows)
        self.assertEqual(self.sessions(c), 0)

    def test_foreign_cleanup_deleter_cannot_admit(self):
        c = self.candidate()
        _, path, rows = self.children(c, 'cleanup')
        rows[-1]['argv'][9] = '/untrusted/python'
        rows[-1]['argv'][13] = '/untrusted/foreign-remove.py'
        c[2][path] = codec.canonical_bytes(rows)
        self.assertEqual(self.sessions(c), 0)

    def test_foreign_full_audit_commands_cannot_admit(self):
        c = self.candidate()
        op, path, _ = self.children(c, 'audit')
        source = json.loads(c[2]['A/input.children.json'])[0]
        forged = []
        for verb in ['run', 'load', 'image', 'run', 'rm', 'ps', 'unshare']:
            child = json.loads(json.dumps(source))
            child.update(started_at_unix_ns=op['started_at_unix_ns'], finished_at_unix_ns=op['finished_at_unix_ns'],
                         cwd='/foreign')
            child['argv'] = ['/foreign/engine', '--remote=true', '--root', '/foreign/store', '--runroot', '/foreign/run',
                             '--tmpdir', '/foreign/tmp', verb, '--privileged', 'sha256:' + 'f' * 64]
            forged.append(child)
        c[2][path] = codec.canonical_bytes(forged)
        self.assertEqual(self.sessions(c), 0)


class AuditBinding(unittest.TestCase):
    """Hale R4-MAP-001(c) / Cox round 2: a REAL audit group (evidence call + six nested
    R3 children) binds by full command, session, request and reply join."""

    def receipt(self):
        lane = Lane(self)
        fake = r3.FakeEngine(lane.fx)

        def engine(argv, environment, session, timeout, limit):
            code, out = fake(argv, environment, session, timeout, limit)
            files = {}
            for key, raw in (('stdout', out), ('stderr', b''), ('journal', b'{"fixture":true}\n')):
                path = Path(session) / ('fixture-' + str(len(fake.calls)) + '.' + key)
                path.write_bytes(raw)
                files[key] = path
            return code, out, files

        def inspect(*args, inspection, **kwargs):
            inspection.check(r3.rows(('a', b'x = 1\n')))
            return (), [{'path': 'app/module.py', 'bytes': 5, 'sha256': '0' * 64, 'mode': 420, 'uid': 1001, 'gid': 1001, 'mtime': 0}]
        with patch.object(product('artifacts'), 'inspect_oci', inspect), patch.object(inspector, '_engine', engine), \
                patch.object(runner, '_runtime_smoke', return_value=('pass', 'fixture contract')), \
                patch.object(runner, '_child', Engine(lane, ps=[[], [], [], []])):
            result = runner.native_lanes(verified(lane.fx, lane.value), lane.materialize, lane.base, lane.closure, HOST,
                                         lane.parent, lane.outputs, lane.evidence, codec.canonical_bytes(qualification(lane)))
        self.assertEqual(result['result'], 'RAW')
        value = json.loads(result['receipt'])
        measured = {row['path']: (lane.evidence / row['path']).read_bytes() for row in value['evidence']['inventory']}
        pending = inspector._Pending(lane.closure, contracts.freeze(lane.fx.profile), contracts.freeze(HOST), inspector._ADMITTED)
        return lane, value, measured, json.loads(measured['qualification.json']), pending

    def bind(self, lane, value, measured, doc, pending, change=None):
        build = value['builds']['A']
        inventory = value['evidence']['inventory']
        if change:
            op = next(op for op in build['operations'] if op['phase'] == 'audit')
            path = op['evidence']['path'][:-5] + '.children.json'
            children = json.loads(measured[path])
            change(children)
            raw = codec.canonical_bytes(children)
            measured = dict(measured, **{path: raw})
            inventory = [dict(row, bytes=len(raw), sha256=sha(raw)) if row['path'] == path else row for row in inventory]
        return receipts._native_children(build, measured, inventory, doc, lane.value, pending)

    def test_real_audit_group_binds_and_every_tamper_refuses(self):
        lane, value, measured, doc, pending = self.receipt()
        self.bind(lane, value, measured, doc, pending)
        def flag(c):
            c[0]['argv'] = [t if t != '--network=none' else '--network=host' for t in c[0]['argv']]
            c[3]['argv'] = list(c[0]['argv'])
        cases = {'plan flag': flag,
                 'nested load': lambda c: c[1]['argv'].__setitem__(-1, '/elsewhere/image.tar'),
                 'nested rm name': lambda c: c[4]['argv'].__setitem__(-1, 'cortex-inspector-foreign'),
                 'request stream': lambda c: c[0].__setitem__('request', dict(c[0]['stderr'])),
                 'reply join': lambda c: c[3].__setitem__('stdout', dict(c[3]['stderr'])),
                 'cwd': lambda c: c[2].__setitem__('cwd', '/elsewhere'),
                 'group cut': lambda c: c.pop()}
        for name, change in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError) as caught:
                    self.bind(lane, value, measured, doc, pending, change)
                self.assertEqual(str(caught.exception), 'native_child_evidence')
        with self.assertRaises(ValueError):
            receipts._native_children(value['builds']['A'], measured, value['evidence']['inventory'], doc, lane.value, None)

    def test_request_row_with_an_unknown_key_refuses(self):
        lane = Lane(self)
        files, _ = originals(lane)
        request = json.loads(files['q/positive.request'])
        request['rows'][0]['extra'] = 'x'
        raw = codec.canonical_bytes(request)
        with self.assertRaises(ValueError):
            receipts._closed_request(raw, lane.closure, contracts.thaw(lane.closure.descriptor)['descriptor'],
                                     lane.fx.profile, lane.value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
