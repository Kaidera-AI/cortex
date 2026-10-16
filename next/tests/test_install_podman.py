"""X11-c source fixtures: fake ONLY the finite engine/held native observation seam.

No installed owner, engine, F image or host qualification is claimed. Expected
postimages and creation/lifetime witnesses are separate from command replies.
"""
from contextlib import contextmanager
import copy
import importlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cortex_core.install.owner import OwnerRefusal


ID = 'a' * 64
OTHER = 'b' * 64
DIGEST = 'sha256:' + 'c' * 64
SUBJECT = {'identity': {'install': 'one'}, 'effect': {'resource_kind': 'container'},
           'request_ref': {'path': 'request', 'bytes': 1, 'sha256': 'd' * 64}}


def resource(kind):
    common = {'consumers': [], 'transaction': 'attempt-one'}
    if kind == 'volume':
        return dict(common, name='core-data', created_at='2026-10-16T10:00:00Z',
                    driver='local', options={}, mountpoint={'path': '/owner/store/v/data',
                    'dev': 1, 'ino': 22, 'uid': 1001})
    if kind == 'network':
        return dict(common, id=ID, name='private-core', driver='bridge',
                    subnet='10.89.0.0/24', options={}, listeners=[])
    if kind == 'image':
        return dict(common, id=ID, manifest=DIGEST, config='sha256:' + ID, layers=[DIGEST],
                    os='linux', arch='arm64', source='release-one', version='0.2.001', tags=[])
    return dict(common, id=ID, name='core', image=ID, role='core',
                argv=['/opt/cortex/bin/postgres', '-D', '/var/lib/cortex/core', '-c',
                'config_file=/run/cortex/postgresql.conf', '-c', 'hba_file=/run/cortex/pg_hba.conf'],
                uid=1001, gid=1001, uid_map=[[1001, 1001, 1]],
                mounts=[{'source': 'core-data', 'target': '/var/lib/cortex/core', 'read_only': False}],
                network=OTHER, ports=[], read_only=True, cap_drop=['ALL'],
                no_new_privileges=True, privileged=False, engine_access=[],
                resources={'cpu_millicores': 2000, 'memory_bytes': 1073741824, 'pids': 64}, running=False,
                ready=False, process=None)


class Engine:
    """Mechanical engine + actual-fact collector double; never a verifier PASS."""
    def __init__(self, kind='container', action='create'):
        self.kind = kind
        after = resource(kind)
        before = None if action in ('create', 'load') else copy.deepcopy(after)
        if action == 'remove':
            after = None
        self.plan = {'kind': kind, 'action': action, 'name': (after or before).get('name', ID),
            'before': before, 'after': after, 'ownership': 'created_by_this_transaction',
            'data_class': 'ephemeral', 'inverse': action == 'remove',
            'subject': copy.deepcopy(SUBJECT), 'deadline_ns': 1000,
            'oci': '/owner/originals/core.oci', 'image_pair': [resource('image'),
                dict(resource('image'), id=OTHER, config='sha256:' + OTHER)], 'role': 'core', 'signal': 'TERM',
            'helper': 'inspect', 'c2_ns': 600, 'dependents': []}
        self.state = copy.deepcopy(before)
        self.now = 1
        self.held = False
        self.calls = []
        self.reads = 0
        self.on_run = None
        self.on_observe = None
        self.creation_fault = None
        self.execution_fault = None
        self.execution = None
        self.reply = {'returncode': 0, 'stdout': b'', 'stderr': b'',
            'lifetime': {'boot': 'boot-one', 'pid': 81, 'start': 9, 'uid': 1001},
            'remaining': [], 'pipes_closed': True, 'reaped': True}
        self.binding = {'executable': '/owner/bin/podman', 'root': '/owner/store',
            'runroot': '/owner/run', 'tmpdir': '/owner/tmp', 'home': '/owner/home',
            'owner_uid': 1001, 'peer_uid': 1002, 'owner_groups': [1001],
            'peer_groups': [1002], 'backend_uid': 1001, 'boot': 'boot-one',
            'store_identity': [1, 2, 1001], 'namespace': 'user:one',
            'exclusion': {'holder': 'owner-one', 'generation': 3, 'participants': [1001],
                          'bypass_uids': []},
            'owner_lifetime': {'boot': 'boot-one', 'pid': 80, 'start': 8, 'uid': 1001},
            'clock_epoch': 'clock-one', 'target_arch': 'arm64', 'source': 'release-one',
            'version': '0.2.001', 'core_executable': '/opt/cortex/bin/postgres', 'authority': ['policy-one', 'consent-one', 'head-one', 7],
            'closure': [{'path': '/owner/bin/podman', 'dev': 1, 'ino': 3,
                         'uid': 0, 'writable_by': [], 'sha256': 'e' * 64}],
            'environment': {'HOME': '/owner/home', 'XDG_RUNTIME_DIR': '/owner/run',
                            'CONTAINERS_CONF': '/owner/originals/containers.conf',
                            'CONTAINERS_STORAGE_CONF': '/owner/originals/storage.conf'}}
        self.plan['subject']['effect']['resource_kind'] = kind
        for path in [*self.binding['environment'].values(), self.plan['oci']]:
            if path in (self.binding['home'], self.binding['runroot']):
                continue
            self.binding['closure'].append(dict(self.binding['closure'][0], path=path,
                ino=len(self.binding['closure']) + 4))
        if action == 'helper':
            self.plan['role'] = 'api'
            for v in (self.plan['before'], self.plan['after']):
                v.update(role='api', name='api', image=OTHER,
                    argv=['/opt/cortex/python/bin/python3', '-I', '-B', '-m', 'cortex_core.runtime',
                          'api', '--config', '/run/cortex/instance.json',
                          '--runtime-lock', '/run/cortex/runtime.lock.json'],
                    ports=['127.0.0.1:8000:8000'],
                    mounts=[{'source': 'api-config', 'target': '/run/cortex/instance.json', 'read_only': True}],
                    running=True, process={'boot': 'boot-one', 'pid': 90, 'start': 10, 'uid': 1001})
            self.plan['name'] = 'api'
            self.state = copy.deepcopy(self.plan['before'])
        self.plan['dependencies'] = []
        self.plan['image_inventory'] = [copy.deepcopy(before)] if kind == 'image' and before is not None else []
        self.images = copy.deepcopy(self.plan['image_inventory'])
        self.related = {}
        self.absent = set()
        if kind == 'container':
            v = self.plan['after'] or self.plan['before']
            refs = [('image', copy.deepcopy(self.plan['image_pair'][0 if v['role'] == 'core' else 1])),
                    ('network', dict(resource('network'), id=OTHER))]
            refs += [('volume', dict(resource('volume'), name=m['source'])) for m in v['mounts']]
            for depkind, record in refs:
                if self.plan['before'] is not None:
                    record['consumers'] = [self.plan['before']['id']]
                self.plan['dependencies'].append({'kind': depkind, 'resource': copy.deepcopy(record)})
                self.related[(depkind, record.get('id', record.get('name')))] = copy.deepcopy(record)

    @contextmanager
    def hold(self):
        assert not self.held
        self.held = True
        try:
            yield self
        finally:
            self.held = False

    def current(self):
        assert self.held
        return copy.deepcopy(self.binding), self.now

    def observe(self, kind, target, *, deadline_ns):
        assert self.held and deadline_ns == self.plan['deadline_ns']
        self.reads += 1
        if self.on_observe:
            self.on_observe(self)
        if (kind, target) in self.absent:
            return None
        if kind == 'image_inventory':
            return copy.deepcopy(self.images)
        if kind != self.kind:
            return copy.deepcopy(self.related.get((kind, target)))
        return copy.deepcopy(self.state)

    def witnesses(self):
        assert self.held
        witness = {'creation': {'before_absent': self.plan['before'] is None,
            'subject': self.plan['subject'], 'resource': copy.deepcopy(self.state),
            'exclusion': copy.deepcopy(self.binding['exclusion'])},
            'cleanup': copy.deepcopy(self.reply), 'execution': copy.deepcopy(self.execution)}
        if self.creation_fault:
            self.creation_fault(witness)
        if self.execution_fault:
            self.execution_fault(witness)
        return witness

    def run(self, argv, *, env, deadline_ns, stdout_limit, stderr_limit):
        assert self.held and deadline_ns == self.plan['deadline_ns']
        assert stdout_limit == stderr_limit == 65536
        self.calls.append((tuple(argv), dict(env), deadline_ns))
        self.state = copy.deepcopy(self.plan['after'])
        self.execution = {'argv': list(argv), 'subject': copy.deepcopy(self.plan['subject']),
                          'lifetime': copy.deepcopy(self.reply['lifetime']), 'deadline_ns': deadline_ns}
        try:
            if self.on_run:
                self.on_run(self)
        finally:
            # Actual resource attachments survive a lost engine reply too.
            if self.kind == 'container':
                for v in self.related.values():
                    consumers = set(v['consumers'])
                    if self.plan['before'] is not None:
                        consumers.discard(self.plan['before']['id'])
                    if self.state is not None:
                        consumers.add(self.state['id'])
                    v['consumers'] = sorted(consumers)
            elif self.kind == 'image':
                changed_id = (self.plan['before'] or self.plan['after'])['id']
                self.images = [v for v in self.images if v['id'] != changed_id]
                if self.state is not None:
                    self.images.append(copy.deepcopy(self.state))
        return copy.deepcopy(self.reply)


class PodmanPrimitives(unittest.TestCase):
    def api(self):
        path = Path(__file__).resolve().parents[1] / 'src/cortex_core/install/podman.py'
        self.assertTrue(path.is_file(), 'X11_C_ADAPTER_NOT_DELIVERED')
        return importlib.import_module('cortex_core.install.podman')

    @contextmanager
    def opened(self, engine):
        api = self.api()
        with patch.object(api, '_installed_engine', lambda: engine):
            with api._open() as p:
                yield p

    def execute(self, engine):
        with self.opened(engine) as p:
            p.snapshot()
            p.dispatch(engine.plan['subject'])
            with p.measure(engine.plan['subject']) as measured:
                self.assertTrue(engine.held)
                return measured

    def test_default_binding_gap_has_no_candidate_or_host_access(self):
        api = self.api()
        with patch('subprocess.Popen', side_effect=AssertionError('host effect')), \
             patch('socket.socket', side_effect=AssertionError('socket access')):
            with self.assertRaises(OwnerRefusal) as e:
                with api._open():
                    self.fail('unbound opened')
        self.assertEqual(e.exception.detail, 'required_binding_gap')
        self.assertEqual(api.__all__, [])

    def test_image_load_uses_fixed_absolute_engine_and_owned_oci_no_pull(self):
        e = Engine('image', 'load')
        self.assertEqual(self.execute(e)[0], 'APPLIED')
        argv, env, deadline = e.calls[0]
        self.assertEqual(argv[0], '/owner/bin/podman')
        self.assertIn('--remote=false', argv)
        self.assertEqual(argv[-3:], ('load', '--input', '/owner/originals/core.oci'))
        self.assertNotIn('PATH', env)
        self.assertFalse(set(env) & {'CONTAINER_HOST', 'DOCKER_HOST', 'CONTAINER_CONNECTION'})
        self.assertEqual(deadline, 1000)

    def test_container_create_has_security_mount_network_resources_and_immutable_image(self):
        e = Engine()
        self.assertEqual(self.execute(e)[0], 'APPLIED')
        argv = e.calls[0][0]
        for flag in ('--pull=never', '--read-only', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges', '--user=1001:1001',
                     '--network=' + OTHER, '--pids-limit=64', '--cpu-period', '--cpu-quota', '--memory'):
            self.assertIn(flag, argv)
        self.assertIn('sha256:' + ID, argv)
        self.assertEqual(argv[argv.index('--cpu-quota') + 1], '2000000')
        self.assertEqual(argv[argv.index('--memory') + 1], '1073741824')
        self.assertNotIn('compose', argv)

    def test_volume_create_observes_mountpoint_custody_and_creation_witness(self):
        e = Engine('volume')
        self.assertEqual(self.execute(e)[0], 'APPLIED')
        self.assertEqual(e.calls[0][0][-4:], ('volume', 'create', '--driver=local', 'core-data'))
        e = Engine('volume');e.on_run = lambda e: e.state['mountpoint'].update(ino=999)
        self.assertEqual(self.execute(e)[0], 'UNKNOWN')

    def test_network_create_is_private_and_full_id_membership_observed(self):
        e = Engine('network')
        self.assertEqual(self.execute(e)[0], 'APPLIED')
        argv = e.calls[0][0]
        self.assertIn('--internal', argv)
        self.assertIn('--subnet=10.89.0.0/24', argv)
        e = Engine('network');e.on_run = lambda e: e.state.update(id=OTHER)
        self.assertEqual(self.execute(e)[0], 'UNKNOWN')

    def test_helper_process_uses_exact_container_fixed_vector_and_cleanup_lifetime(self):
        e = Engine('container', 'helper');e.plan['after'] = copy.deepcopy(e.plan['before'])
        self.assertEqual(self.execute(e)[0], 'APPLIED')
        argv = e.calls[0][0]
        self.assertEqual(argv[-14:], (ID, '/opt/cortex/python/bin/python3', '-I', '-B',
            '-m', 'cortex_core.runtime', 'inspect', '--config', '/run/cortex/inspection.json',
            '--runtime-lock', '/run/cortex/runtime.lock.json', '--request-ref',
            '/run/cortex/readiness-request.json', '--json'))
        e = Engine('container', 'helper');e.reply['remaining'] = [81]
        self.assertEqual(self.execute(e)[0], 'UNKNOWN')

    def test_changed_backend_or_authority_before_dispatch_causes_zero_effects(self):
        for key, value in [('boot', 'new'), ('store_identity', [1, 99, 1001]),
                           ('authority', ['policy-two', 'consent-one', 'head-one', 7]),
                           ('namespace', 'different')]:
            e = Engine()
            with self.subTest(key=key), self.opened(e) as p:
                p.snapshot();e.binding[key] = value
                with self.assertRaises(OwnerRefusal):p.dispatch(e.plan['subject'])
                self.assertEqual(e.calls, [])

    def test_owner_exclusion_and_fixed_closure_faults_refuse_before_observation(self):
        faults = [lambda b: b.update(owner_uid=0), lambda b: b.update(peer_uid=1001),
            lambda b: b['peer_groups'].append(1001), lambda b: b.update(backend_uid=1002),
            lambda b: b['exclusion'].update(bypass_uids=[1002]),
            lambda b: b['exclusion'].update(participants=[1001, 1002]),
            lambda b: b.update(executable='podman'),
            lambda b: b['closure'][0].update(writable_by=[1002]),
            lambda b: b['environment'].update(CONTAINER_HOST='unix:///evil')]
        for fault in faults:
            e = Engine();fault(e.binding)
            with self.subTest(fault=fault), self.assertRaises(OwnerRefusal):
                with self.opened(e):self.fail('unsafe backend opened')
            self.assertEqual((e.calls, e.reads), ([], 0))

    def test_volume_ABA_and_new_consumers_before_remove_never_dispatch(self):
        for mutate in [lambda r: r.update(created_at='later'),
                       lambda r: r['mountpoint'].update(ino=777),
                       lambda r: r.update(consumers=[OTHER])]:
            e = Engine('volume', 'remove')
            with self.subTest(mutate=mutate), self.opened(e) as p:
                p.snapshot();mutate(e.state)
                with self.assertRaises(OwnerRefusal):p.dispatch(e.plan['subject'])
                self.assertEqual(e.calls, [])

    def test_full_identity_replacements_never_remove_by_name_or_short_id(self):
        for kind in ['image', 'network', 'container']:
            e = Engine(kind, 'remove')
            with self.subTest(kind=kind), self.opened(e) as p:
                p.snapshot();e.state['id'] = OTHER
                with self.assertRaises(OwnerRefusal):p.dispatch(e.plan['subject'])
                self.assertEqual(e.calls, [])

    def test_shared_external_and_data_resources_retained(self):
        for field, value in [('ownership', 'external_read_only'),
                              ('ownership', 'verified_owned_continuation'),
                              ('data_class', 'authoritative'), ('inverse', False)]:
            e = Engine('volume', 'remove');e.plan[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(OwnerRefusal):
                self.execute(e)
            self.assertEqual(e.calls, [])
        e = Engine('volume', 'remove');e.plan['before']['consumers'] = [OTHER]
        e.state = copy.deepcopy(e.plan['before'])
        with self.assertRaises(OwnerRefusal):self.execute(e)
        self.assertEqual(e.calls, [])

    def test_exact_unshared_inverse_never_force_prune_or_kill_retained_core(self):
        for kind in ['volume', 'network', 'image', 'container']:
            e = Engine(kind, 'remove')
            self.assertEqual(self.execute(e)[0], 'APPLIED')
            argv = e.calls[0][0]
            self.assertEqual(argv[-1], 'core-data' if kind == 'volume' else ID)
            self.assertFalse({'--force', '-f', 'prune'} & set(argv))

    def test_wrong_image_pair_platform_source_and_missing_digest_refused(self):
        for field, value in [('arch', 'amd64'), ('source', 'other'), ('version', 'other'),
                             ('manifest', 'latest'), ('layers', [])]:
            e = Engine('image', 'load');e.plan['after'][field] = value
            with self.subTest(field=field), self.assertRaises(OwnerRefusal):self.execute(e)
            self.assertEqual(e.calls, [])

    def test_container_escape_security_and_listener_faults_zero_dispatch(self):
        for field, value in [('uid', 0), ('cap_drop', []), ('no_new_privileges', False),
                             ('read_only', False), ('engine_access', ['/run/podman.sock']),
                             ('privileged', True), ('ports', ['5432:5432']),
                             ('argv', ['/bin/sh']), ('resources', {})]:
            e = Engine();e.plan['after'][field] = value
            with self.subTest(field=field), self.assertRaises(OwnerRefusal):self.execute(e)
            self.assertEqual(e.calls, [])

    def test_deadline_equality_no_effect_and_no_fresh_grace(self):
        e = Engine()
        with self.opened(e) as p:
            p.snapshot();e.now = 1000
            with self.assertRaises(OwnerRefusal):p.dispatch(e.plan['subject'])
        self.assertEqual(e.calls, [])
        e = Engine();e.on_run = lambda e: setattr(e, 'now', 1000)
        self.assertEqual(self.execute(e)[0], 'UNKNOWN')
        self.assertEqual(e.calls[0][2], 1000)

    def test_core_stop_retained_if_dependents_not_measured_gone_by_C2(self):
        e = Engine('container', 'stop');e.plan['dependents'] = [OTHER];e.now = 601
        with self.assertRaises(OwnerRefusal):self.execute(e)
        self.assertEqual(e.calls, [])

    def test_lost_reply_independent_exact_postimage_still_APPLIED(self):
        e = Engine();e.on_run = lambda e: (_ for _ in ()).throw(TimeoutError())
        self.assertEqual(self.execute(e)[0], 'APPLIED')
        self.assertEqual(len(e.calls), 1)

    def test_exact_preimage_and_finite_cleanup_NOT_APPLIED_no_retry(self):
        e = Engine();e.on_run = lambda e: setattr(e, 'state', None)
        e.reply['returncode'] = 125
        with self.opened(e) as p:
            p.snapshot();p.dispatch(e.plan['subject'])
            with p.measure(e.plan['subject']) as result:self.assertEqual(result[0], 'NOT_APPLIED')
            with self.assertRaises(OwnerRefusal):p.dispatch(e.plan['subject'])
        self.assertEqual(len(e.calls), 1)

    def test_partial_missing_mixed_cleanup_witness_always_UNKNOWN(self):
        faults = [lambda e: e.state.update(id=OTHER),
            lambda e: e.reply.update(remaining=[81]), lambda e: e.reply.update(reaped=False),
            lambda e: e.reply.update(pipes_closed=False), lambda e: e.reply.update(lifetime=None),
            lambda e: e.reply.update(stdout=b'x'*65537),
            lambda e: e.binding.update(clock_epoch='changed'),
            lambda e: e.binding['exclusion'].update(generation=4)]
        for fault in faults:
            e = Engine();e.on_run = fault
            with self.subTest(fault=fault):self.assertEqual(self.execute(e)[0], 'UNKNOWN')
            self.assertEqual(len(e.calls), 1)

    def test_measurement_without_dispatch_never_manufactures_APPLIED(self):
        e = Engine('container', 'helper')
        with self.opened(e) as p:
            p.snapshot()
            with p.measure(e.plan['subject']) as result:self.assertEqual(result[0], 'UNKNOWN')
        self.assertEqual(e.calls, [])

    def test_measurement_never_dispatches_and_guard_survives_publication_failure(self):
        e = Engine()
        with self.assertRaisesRegex(RuntimeError, 'publication lost'):
            with self.opened(e) as p:
                p.snapshot();p.dispatch(e.plan['subject'])
                before = len(e.calls)
                with p.measure(e.plan['subject']) as result:
                    self.assertEqual(result[0], 'APPLIED');self.assertTrue(e.held)
                    raise RuntimeError('publication lost')
        self.assertFalse(e.held);self.assertEqual(len(e.calls), before)

    def test_subject_mismatch_no_effect_and_no_cross_attempt_measurement(self):
        e = Engine();other = copy.deepcopy(SUBJECT);other['identity']['install'] = 'other'
        with self.opened(e) as p:
            p.snapshot()
            with self.assertRaises(OwnerRefusal):p.dispatch(other)
            with p.measure(other) as result:self.assertEqual(result[0], 'UNKNOWN')
        self.assertEqual(e.calls, [])

    def test_held_plan_is_immutable_after_open(self):
        e = Engine()
        with self.opened(e) as p:
            p.snapshot();e.plan['after']['argv'] = ['/bin/sh']
            p.dispatch(SUBJECT)
            self.assertNotIn('/bin/sh', e.calls[0][0])
            with p.measure(SUBJECT) as result:self.assertEqual(result[0], 'UNKNOWN')

    def test_new_generated_resource_identity_is_observed_not_predicted(self):
        for kind in ['volume', 'network', 'container']:
            e = Engine(kind)
            actual = resource(kind)
            if kind == 'volume':
                e.plan['after']['created_at'] = None
                e.plan['after']['mountpoint'].update(dev=None, ino=None)
            else:
                e.plan['after']['id'] = None
            e.on_run = lambda e, actual=actual: setattr(e, 'state', copy.deepcopy(actual))
            with self.subTest(kind=kind):self.assertEqual(self.execute(e)[0], 'APPLIED')

    def test_missing_or_wrong_creation_original_is_UNKNOWN(self):
        for fault in [lambda w: w.update(creation=None),
                      lambda w: w['creation'].update(before_absent=False),
                      lambda w: w['creation']['subject']['identity'].update(install='foreign'),
                      lambda w: w['creation']['exclusion'].update(generation=999)]:
            e = Engine();e.creation_fault = fault
            with self.subTest(fault=fault):self.assertEqual(self.execute(e)[0], 'UNKNOWN')

    def test_helper_exit_code_cannot_replace_independent_execution_witness(self):
        for fault in [lambda w: w.update(execution=None),
                      lambda w: w['execution'].update(argv=['/bin/sh']),
                      lambda w: w['execution'].update(deadline_ns=2000),
                      lambda w: w['execution']['lifetime'].update(start=999),
                      lambda w: w['execution']['subject']['identity'].update(install='foreign')]:
            e = Engine('container', 'helper');e.execution_fault = fault
            with self.subTest(fault=fault):self.assertEqual(self.execute(e)[0], 'UNKNOWN')

    def test_core_stop_waits_until_C2_and_measured_noncore_absence(self):
        e = Engine('container', 'stop')
        e.plan['before'].update(running=True, process={'boot': 'boot-one', 'pid': 90, 'start': 10, 'uid': 1001})
        e.state = copy.deepcopy(e.plan['before']);e.now = 599
        with self.assertRaises(OwnerRefusal):self.execute(e)
        self.assertEqual(e.calls, [])
        e.now = 601
        self.assertEqual(self.execute(e)[0], 'APPLIED')
        self.assertEqual(e.calls[0][0][-3:], ('kill', '--signal=TERM', ID))

    def test_start_cannot_attest_unchanged_stopped_container(self):
        e = Engine('container', 'start')
        with self.assertRaises(OwnerRefusal):self.execute(e)
        self.assertEqual(e.calls, [])

    def test_wrong_kind_subject_refused_before_observation(self):
        e = Engine('volume');e.plan['subject']['effect']['resource_kind'] = 'network'
        with self.assertRaises(OwnerRefusal):
            with self.opened(e):self.fail('wrong effect projection accepted')
        self.assertEqual((e.calls, e.reads), ([], 0))

    def test_source_original_and_engine_config_descriptors_must_stay_held(self):
        for path in ['/owner/originals/core.oci', '/owner/originals/containers.conf',
                     '/owner/originals/storage.conf']:
            e = Engine('image', 'load')
            e.binding['closure'] = [x for x in e.binding['closure'] if x['path'] != path]
            with self.subTest(path=path), self.assertRaises(OwnerRefusal):self.execute(e)
            self.assertEqual((e.calls, e.reads), ([], 0))

    def test_guard_revoked_during_observe_prevents_dispatch(self):
        e = Engine()
        with self.opened(e) as p:
            p.snapshot();e.on_observe = lambda e: e.binding['exclusion'].update(generation=4)
            with self.assertRaises(OwnerRefusal):p.dispatch(e.plan['subject'])
        self.assertEqual(e.calls, [])

    def test_publication_revocation_propagates_and_closed_primitive_cannot_run(self):
        e = Engine()
        with self.opened(e) as p:
            p.snapshot();p.dispatch(e.plan['subject'])
            with self.assertRaises(OwnerRefusal):
                with p.measure(e.plan['subject']) as result:
                    self.assertEqual(result[0], 'APPLIED');e.binding['authority'][0] = 'revoked'
        with self.assertRaises(OwnerRefusal):p.dispatch(e.plan['subject'])
        self.assertEqual(len(e.calls), 1)

    def test_referenced_image_network_and_volume_replacement_prevents_create(self):
        for kind in ['image', 'network', 'volume']:
            e = Engine()
            with self.subTest(kind=kind), self.opened(e) as p:
                p.snapshot()
                key = next(k for k in e.related if k[0] == kind)
                if kind == 'volume':e.related[key]['mountpoint']['ino'] = 999
                else:e.related[key]['id'] = 'f'*64
                with self.assertRaises(OwnerRefusal):p.dispatch(e.plan['subject'])
            self.assertEqual(e.calls, [])

    def test_missing_reference_originals_prevent_create_and_foreign_consumer_is_UNKNOWN(self):
        e = Engine();e.plan['dependencies'] = []
        with self.assertRaises(OwnerRefusal):self.execute(e)
        self.assertEqual(e.calls, [])
        e = Engine()
        def foreign(e):
            key = next(k for k in e.related if k[0] == 'volume')
            e.related[key]['consumers'] = ['f'*64]
        e.on_run = foreign
        self.assertEqual(self.execute(e)[0], 'UNKNOWN')

    def test_resource_replacement_during_publication_prevents_verified_exit(self):
        e = Engine()
        with self.opened(e) as p:
            p.snapshot();p.dispatch(e.plan['subject'])
            with self.assertRaises(OwnerRefusal):
                with p.measure(e.plan['subject']) as result:
                    self.assertEqual(result[0], 'APPLIED');e.state['id'] = OTHER

    def test_core_last_uses_actual_dependent_absence_not_empty_plan_shortcut(self):
        e = Engine('container', 'stop');e.now = 601
        e.plan['before'].update(running=True, process={'boot': 'boot-one', 'pid': 90, 'start': 10, 'uid': 1001})
        e.state = copy.deepcopy(e.plan['before']);e.plan['dependents'] = [OTHER]
        e.absent.add(('container', OTHER))
        self.assertEqual(self.execute(e)[0], 'APPLIED')

    def test_network_attach_detach_requires_exact_stopped_container_and_full_id(self):
        for action in ['connect', 'disconnect']:
            e = Engine('network', action)
            before, after = ([] if action == 'connect' else [OTHER]), ([OTHER] if action == 'connect' else [])
            e.plan['before']['consumers'] = before;e.plan['after']['consumers'] = after
            e.state = copy.deepcopy(e.plan['before'])
            c = dict(resource('container'), id=OTHER,
                     network=None if action == 'connect' else ID)
            e.plan['dependencies'] = [{'kind': 'container', 'resource': copy.deepcopy(c)}]
            e.related[('container', OTHER)] = c
            e.on_run = lambda e, action=action: e.related[('container', OTHER)].update(network=ID if action == 'connect' else None)
            with self.subTest(action=action):self.assertEqual(self.execute(e)[0], 'APPLIED')
            self.assertEqual(e.calls[0][0][-4:], ('network', action, ID, OTHER))
        e = Engine('network', 'disconnect');e.plan['before']['consumers'] = [OTHER]
        e.plan['after']['consumers'] = [];e.state = copy.deepcopy(e.plan['before'])
        with self.assertRaises(OwnerRefusal):self.execute(e)
        self.assertEqual(e.calls, [])

    def test_finite_migrate_and_core_init_reuse_exact_RC0_vectors(self):
        for mode in ['migrate', 'core-init']:
            e = Engine('container', 'helper');e.plan['role'] = 'helper';e.plan['helper'] = mode
            e.binding.update(core_initdb='/opt/cortex/bin/initdb', bootstrap_role='bootstrap_one',
                             operation_id='00000000-0000-4000-8000-000000000001')
            if mode == 'migrate':
                argv = ['/opt/cortex/python/bin/python3', '-I', '-B', '-m', 'cortex_core.runtime',
                    'migrate', '--config', '/run/cortex/migration.json', '--runtime-lock',
                    '/run/cortex/runtime.lock.json', '--operation-id', e.binding['operation_id']]
            else:
                argv = ['/opt/cortex/bin/initdb', '-D', '/var/lib/cortex/core', '--username',
                    'bootstrap_one', '--encoding=UTF8', '--locale-provider=libc', '--locale=C',
                    '--auth-local=scram-sha-256', '--auth-host=scram-sha-256', '--data-checksums',
                    '--pwfile=/run/cortex/bootstrap/initdb-password', '--no-instructions']
            for r in [e.plan['before'], e.plan['after']]:
                r.update(role='helper', running=False, process=None, argv=argv, ports=[])
                if mode == 'core-init':
                    r['image'] = ID
            if mode == 'core-init':
                e.plan['dependencies'][0]['resource'] = dict(resource('image'), consumers=[ID])
                del e.related[('image', OTHER)]
                e.related[('image', ID)] = copy.deepcopy(e.plan['dependencies'][0]['resource'])
            e.state = copy.deepcopy(e.plan['before'])
            with self.subTest(mode=mode):self.assertEqual(self.execute(e)[0], 'APPLIED')
            self.assertEqual(e.calls[0][0][-4:], ('start', '--attach', '--sig-proxy=false', ID))

    def test_malformed_internal_binding_and_plan_refuse_without_raw_error(self):
        for fault in [lambda e: e.binding.update(owner_groups=1),
                      lambda e: e.plan.update(image_pair=[{}, {}]),
                      lambda e: e.plan.update(after={'id': None})]:
            e = Engine();fault(e)
            with self.subTest(fault=fault), self.assertRaises(OwnerRefusal):
                with self.opened(e):self.fail('malformed binding accepted')
            self.assertEqual(e.calls, [])

    def test_absolute_other_tools_are_not_admitted_Podman(self):
        for name in ['docker', 'sudo', 'sh', 'podman-compose']:
            e = Engine();e.binding['executable'] = '/owner/bin/' + name
            e.binding['closure'][0]['path'] = e.binding['executable']
            with self.subTest(name=name), self.assertRaises(OwnerRefusal):
                with self.opened(e):self.fail('other executable admitted')
            self.assertEqual((e.calls, e.reads), ([], 0))

    def test_image_load_preserves_complete_shared_inventory_and_tags(self):
        e = Engine('image', 'load')
        old = dict(resource('image'), id='f'*64, config='sha256:'+'f'*64, tags=['shared:tag'],
                   arch='amd64', source='older-source', version='older-version')
        e.plan['image_inventory'] = [copy.deepcopy(old)];e.images = [copy.deepcopy(old)]
        self.assertEqual(self.execute(e)[0], 'APPLIED')
        for fault in [lambda e: e.images[0].update(tags=['overwritten:tag']),
                      lambda e: e.images.clear()]:
            e = Engine('image', 'load')
            e.plan['image_inventory'] = [copy.deepcopy(old)];e.images = [copy.deepcopy(old)]
            e.on_run = fault
            with self.subTest(fault=fault):self.assertEqual(self.execute(e)[0], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
