"""R3 inspector port, contained-run contract and shared pyc core (fixture level).

Accepted note v2 (Vera R3-T1/T2, Hale H1-H5). No container, host or native run:
a fake engine stands in at the one `_engine` seam. Native execution stays
unqualified; nothing here is native or containment evidence.
"""
import base64
import hashlib
import importlib.util
import importlib._bootstrap_external as bootstrap
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_unit1_red import product

inspector = product('inspector')
artifacts = product('artifacts')
core = product('_pyc_check')
engine = product('build.engine')
codec = product('codec')

VERSION = '%d.%d.%d' % sys.version_info[:3]
MAGIC = importlib.util.MAGIC_NUMBER.hex()
TAG = 'cpython-%d%d' % sys.version_info[:2]
PYTHON = b'\x7fELF fixture interpreter, never executed'
STDLIB = b'# fixture stdlib marker\n'
LOADER = b'\x7fELF fixture loader, never executed'
SCRIPT = Path(core.__file__).read_bytes() if core else b''  # RED on a tree without the core
LIMITS = dict(input_bytes=1 << 20, output_bytes=1 << 16, object_count=4096, depth=8, seconds=30,
              memory_bytes=1 << 28, pids=16, cpu_millicores=500)
INDEX = 'application/vnd.oci.image.index.v1+json'
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYER = 'application/vnd.oci.image.layer.v1.tar'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def tar(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for path, kind, body, mode in entries:
            info = tarfile.TarInfo(path)
            info.mode, info.mtime = mode, 0
            if kind == 'dir':
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == 'link':
                info.type, info.linkname = tarfile.SYMTYPE, body
                archive.addfile(info)
            else:
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def layer_entries(script=SCRIPT, extra=()):
    return [('opt', 'dir', b'', 0o755), ('opt/cortex-inspector', 'dir', b'', 0o755),
            ('opt/cortex-inspector/bin', 'dir', b'', 0o755),
            ('opt/cortex-inspector/bin/python', 'file', PYTHON, 0o755),
            ('opt/cortex-inspector/inspect.py', 'file', script, 0o644),
            ('usr', 'dir', b'', 0o755), ('usr/lib', 'dir', b'', 0o755),
            ('usr/lib/ld-linux.so', 'file', LOADER, 0o755),
            ('usr/lib/python', 'dir', b'', 0o755), ('usr/lib/python/os.py', 'file', STDLIB, 0o644), *extra]


def image(*, config_config=None, arch='amd64', entries=None, extra_config=None):
    layer = tar(entries if entries is not None else layer_entries())
    config = {'architecture': arch, 'os': 'linux', 'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + sha(layer)]},
              'config': dict(inspector.CLOSED_CONFIG) if config_config is None else config_config}
    config.update(extra_config or {})
    config_raw = json.dumps(config, sort_keys=True).encode()
    manifest = {'schemaVersion': 2, 'mediaType': MANIFEST,
                'config': {'mediaType': CONFIG, 'digest': 'sha256:' + sha(config_raw), 'size': len(config_raw)},
                'layers': [{'mediaType': LAYER, 'digest': 'sha256:' + sha(layer), 'size': len(layer)}]}
    manifest_raw = json.dumps(manifest, sort_keys=True).encode()
    index = {'schemaVersion': 2, 'mediaType': INDEX, 'manifests': [{'mediaType': MANIFEST, 'digest': 'sha256:' + sha(manifest_raw),
             'size': len(manifest_raw), 'platform': {'os': 'linux', 'architecture': arch}}]}
    outer = [('oci-layout', 'file', b'{"imageLayoutVersion":"1.0.0"}', 0o644),
             ('index.json', 'file', json.dumps(index).encode(), 0o644),
             ('blobs', 'dir', b'', 0o755), ('blobs/sha256', 'dir', b'', 0o755)]
    outer += [('blobs/sha256/' + sha(body), 'file', body, 0o644) for body in (layer, config_raw, manifest_raw)]
    return tar(outer), sha(manifest_raw), sha(config_raw), 'sha256:' + sha(layer)


def private_base():
    for base in (Path.home(), Path(__file__).resolve().parent):
        try:
            inspector._private_root(base.resolve(), ())
            return base.resolve()
        except (ValueError, OSError):
            continue
    raise unittest.SkipTest('no private base directory for custody fixtures')


class Fixture:
    """Owned private root, host engine/config files and one accepted descriptor."""

    def __init__(self, **image_options):
        self.top = Path(tempfile.mkdtemp(prefix='r3-inspector-', dir=private_base()))
        os.chmod(self.top, 0o700)
        self.root, self.host, self.candidate = self.top / 'owned', self.top / 'host', self.top / 'candidate'
        for path in (self.root, self.host, self.candidate):
            path.mkdir(mode=0o755)
        self.archive, self.manifest, self.config, self.diff_id = image(**image_options)
        self.write(self.root / 'inspector.tar', self.archive, 0o444)
        self.write(self.host / 'podman', b'#!fixture engine, never executed\n', 0o755)
        self.write(self.host / 'containers.conf', b'[containers]\n', 0o644)
        self.write(self.host / 'storage.conf', b'[storage]\n', 0o644)
        self.profile = {'schema': 'cortex.release-policy.v1', 'target': {'oci_platform': 'linux/amd64'},
                        'inspector': {'scope': 'native', 'execution': 'contained', 'python_caches': 'CHECKED_HASH',
                                      'closure_id': 'inspector-amd64', 'argv': list(inspector.STARTUP), 'environment': {},
                                      'optimization': 0, 'limits': dict(LIMITS)}}
        self.descriptor = self.make_descriptor()

    @staticmethod
    def write(path, raw, mode):
        path.write_bytes(raw)
        os.chmod(path, mode)

    def row(self, path, raw, mode):
        return {'path': str(path), 'bytes': len(raw), 'sha256': sha(raw), 'mode': mode}

    def make_descriptor(self):
        return {'schema': 'cortex.inspector-closure.v1', 'id': 'inspector-amd64', 'platform': 'linux/amd64',
                'python': {'version': VERSION, 'magic': MAGIC},
                'archive': {'path': 'inspector.tar', 'bytes': len(self.archive), 'sha256': sha(self.archive), 'mode': 0o444},
                'manifest_sha256': self.manifest, 'config_sha256': self.config, 'symlinks': {},
                'members': [dict(role='interpreter', **self.row(inspector.STARTUP[0], PYTHON, 0o755)),
                            dict(role='inspector', **self.row(inspector.STARTUP[4], SCRIPT, 0o644)),
                            dict(role='stdlib', **self.row('/usr/lib/python/os.py', STDLIB, 0o644)),
                            dict(role='loader', **self.row('/usr/lib/ld-linux.so', LOADER, 0o755))],
                'engine': self.row(self.host / 'podman', (self.host / 'podman').read_bytes(), 0o755),
                'engine_config': {'containers_conf': self.row(self.host / 'containers.conf', b'[containers]\n', 0o644),
                                  'storage_conf': self.row(self.host / 'storage.conf', b'[storage]\n', 0o644)}}

    def raw(self, descriptor=None):
        raw = codec.canonical_bytes(descriptor or self.descriptor)
        self.profile['inspector']['closure_sha256'] = sha(raw)
        return raw

    def admit(self, descriptor=None):
        raw = self.raw(descriptor)
        return inspector.admit_closure(raw, sha(raw), self.profile, self.root, candidate_roots=[self.candidate])

    def close(self):
        shutil.rmtree(self.top, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def refuses(self, reason, fn, *args, **kw):
        with self.assertRaises(ValueError) as caught:
            fn(*args, **kw)
        self.assertEqual(str(caught.exception), reason)


class Port(Base):
    """v1 RED 1-5, Vera T1-a/b/d, Hale H4."""

    def test_admits_only_when_every_member_matches_the_verified_layers(self):
        closure = self.fx.admit()
        self.assertIs(closure._admission, inspector._ADMITTED)
        self.assertEqual(closure.image_digest, 'sha256:' + self.fx.config)
        self.assertNotEqual(self.fx.manifest, self.fx.config)  # M and C are different objects (H4)
        self.assertEqual(len(closure.members), 4)

    def test_member_missing_or_different_bytes_or_mode_refuses(self):
        for change in ({'sha256': '0' * 64}, {'bytes': 1}, {'mode': 0o600}, {'path': '/usr/lib/python/absent.py'}):
            with self.subTest(change=change):
                descriptor = self.fx.make_descriptor()
                descriptor['members'][2].update(change)
                self.refuses('inspector_image_member_unbound', self.fx.admit, descriptor)

    def test_member_behind_a_whiteout_or_symlink_hop_refuses(self):
        for extra in ([('usr/lib/.wh.python', 'file', b'', 0o644)],):
            with self.subTest(extra=extra):
                fx = Fixture(entries=layer_entries(extra=extra))
                self.addCleanup(fx.close)
                self.refuses('inspector_image_member_unbound', fx.admit)
        entries = [e for e in layer_entries() if not e[0].startswith('usr/lib/python')]
        entries += [('usr/lib/real', 'dir', b'', 0o755), ('usr/lib/real/os.py', 'file', STDLIB, 0o644),
                    ('usr/lib/python', 'link', 'real', 0o777)]
        fx = Fixture(entries=entries)
        self.addCleanup(fx.close)
        descriptor = fx.make_descriptor()
        descriptor['symlinks'] = {'usr/lib/python': 'real'}
        self.refuses('inspector_image_member_unbound', fx.admit, descriptor)

    def test_manifest_or_config_identity_or_platform_mismatch_refuses(self):
        for change in ({'manifest_sha256': '1' * 64}, {'config_sha256': '2' * 64}):
            with self.subTest(change=change):
                descriptor = dict(self.fx.make_descriptor(), **change)
                self.refuses('inspector_image_member_unbound', self.fx.admit, descriptor)
        fx = Fixture(arch='arm64')
        self.addCleanup(fx.close)
        self.refuses('inspector_image_member_unbound', fx.admit)

    def test_archive_replaced_after_acceptance_refuses(self):
        raw = self.fx.raw()
        other, *_ = image(arch='amd64', entries=layer_entries(extra=[('etc', 'dir', b'', 0o755)]))
        os.chmod(self.fx.root / 'inspector.tar', 0o644)
        (self.fx.root / 'inspector.tar').write_bytes(other[:len(self.fx.archive)].ljust(len(self.fx.archive), b'\0'))
        os.chmod(self.fx.root / 'inspector.tar', 0o444)
        self.refuses('inspector_closure_changed', inspector.admit_closure, raw, sha(raw), self.fx.profile,
                     self.fx.root, candidate_roots=[self.fx.candidate])

    def test_host_engine_custody_drift_refuses(self):
        raw = self.fx.raw()
        os.chmod(self.fx.host / 'podman', 0o700)
        self.fx.write(self.fx.host / 'podman', b'#!replaced engine\n', 0o755)
        with self.assertRaises(ValueError):
            inspector.admit_closure(raw, sha(raw), self.fx.profile, self.fx.root, candidate_roots=[self.fx.candidate])

    def test_inspect_script_must_be_this_repository_core(self):
        fx = Fixture(entries=layer_entries(script=SCRIPT + b'# drift\n'))
        self.addCleanup(fx.close)
        descriptor = fx.make_descriptor()
        descriptor['members'][1].update(bytes=len(SCRIPT) + 8, sha256=sha(SCRIPT + b'# drift\n'))
        self.refuses('inspector_image_member_unbound', fx.admit, descriptor)

    def test_T1a_wrapper_entrypoint_refuses_before_any_run(self):
        config = dict(inspector.CLOSED_CONFIG, Entrypoint=['/bin/sh', '-c', 'exec "$@"', 'wrapper'])
        fx = Fixture(config_config=config)
        self.addCleanup(fx.close)
        with patch.object(inspector, '_engine', side_effect=AssertionError('engine ran')) as run:
            self.refuses('inspector_image_member_unbound', fx.admit)
        self.assertEqual(run.call_count, 0)

    def test_T1b_volumes_healthcheck_env_cmd_refuse(self):
        for extra in ({'Volumes': {'/opt/cortex-inspector': {}}}, {'Healthcheck': {'Test': ['CMD', 'true']}},
                      {'Env': ['PYTHONPATH=/x']}, {'Cmd': ['-c', 'pass']}, {'Labels': {'a': 'b'}}):
            with self.subTest(extra=extra):
                fx = Fixture(config_config=dict(inspector.CLOSED_CONFIG, **extra))
                self.addCleanup(fx.close)
                self.refuses('inspector_image_member_unbound', fx.admit)

    def test_owner_root_inside_candidate_tree_refuses(self):
        raw = self.fx.raw()
        self.refuses('candidate_inspector_authority', inspector.admit_closure, raw, sha(raw), self.fx.profile,
                     self.fx.root, candidate_roots=[self.fx.top])


class Abi(Base):
    """v1 RED 4 and Vera's ABI reading."""

    def test_unsupported_or_mismatched_magic_refuses_before_any_read(self):
        for python in ({'version': '2.7.18', 'magic': MAGIC}, {'version': VERSION, 'magic': '00000000'}):
            with self.subTest(python=python):
                descriptor = dict(self.fx.make_descriptor(), python=python)
                with patch.object(inspector, '_read_member', side_effect=AssertionError('read')) as read:
                    self.refuses('inspector_abi_mismatch', self.fx.admit, descriptor)
                self.assertEqual(read.call_count, 0)

    def test_admission_requires_toolchain_and_target_to_match_the_closure(self):
        closure = self.fx.admit()
        other = '3.12.0' if not VERSION.startswith('3.12') else '3.13.0'
        verified = SimpleNamespace(metadata={'release_policy': self.fx.profile},
                                   document={'builder_requirements': {'toolchain': {'version': other}},
                                             'target': {'oci_platform': 'linux/amd64'}})
        self.refuses('inspector_abi_mismatch', inspector.admit_inspection, verified, {'inspector_capability': closure})

    def test_controller_byte_checks_use_the_admitted_table_not_its_own_magic(self):
        source = b'x = 1\n'
        import _imp
        magic = bytes.fromhex(core.MAGIC['3.12'])
        raw = magic + (3).to_bytes(4, 'little') + _imp.source_hash(int.from_bytes(magic, 'little'), source) + b'opaque 3.12 body'
        value = cache_value('3.12.4')
        row = artifacts.pyc_row('app/__pycache__/module.cpython-312.pyc', raw, {'app/module.py': source}, value)
        self.assertEqual(row['final_filename'], '/app/module.py')


def pyc(source, filename, *, magic=None, body=None):
    code = compile(source, filename, 'exec', dont_inherit=True, optimize=0)
    raw = bootstrap._code_to_hash_pyc(code, importlib.util.source_hash(source), checked=True)
    raw = (magic or raw[:4]) + raw[4:]
    raw = bytes(raw)
    return raw if body is None else raw[:16] + body


def cache_value(version=VERSION):
    return {'determinism': {'python': {'applicable': True, 'optimization': 0, 'final_root': '/app'}},
            'builder_requirements': {'toolchain': {'version': version}}}


def rows(*sources):
    value, result = cache_value(), []
    for name, source in sources:
        cache = 'app/__pycache__/%s.%s.pyc' % (name, TAG)
        contents = {'app/%s.py' % name: source}
        result.append(artifacts.pyc_row(cache, pyc(source, '/app/%s.py' % name), contents, value))
    return result


class Plan(Base):
    """v1 RED 6, Hale H1, Vera T1-c."""

    def test_golden_contained_argv_and_explicit_engine_environment(self):
        closure = self.fx.admit()
        request = codec.canonical_bytes({'rows': []})
        plan = inspector.prepare_plan(closure, self.fx.profile, request)
        session = str(plan.session)
        prefix = [str(self.fx.host / 'podman'), '--remote=false', '--root', session + '/store', '--runroot', session + '/run',
                  '--tmpdir', session + '/tmp']
        expected = prefix + ['run', '--name', plan.name, '--cidfile', session + '/cid', '--pull=never', '--rm', '--read-only',
                             '--read-only-tmpfs=false', '--image-volume=ignore', '--no-healthcheck', '--log-driver=none',
                             '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user', '65534:65534',
                             '--workdir=/', '--unsetenv-all', '--pids-limit', '16', '--cpu-period', '1000000',
                             '--cpu-quota', '500000', '--memory', str(1 << 28), '--memory-swap', str(1 << 28),
                             '--mount', 'type=bind,src=' + session + '/request/request.json,dst=/cortex-request.json,ro=true,relabel=private',
                             '--entrypoint', '/opt/cortex-inspector/bin/python', 'sha256:' + self.fx.config,
                             '-I', '-S', '-B', '/opt/cortex-inspector/inspect.py']
        self.assertEqual(list(plan.argv), expected)
        self.assertEqual(list(plan.argv[-4:]), list(inspector.startup_argv(self.fx.profile)[1:]))
        self.assertEqual(dict(plan.environment), {
            'CONTAINERS_CONF': str(self.fx.host / 'containers.conf'), 'CONTAINERS_STORAGE_CONF': str(self.fx.host / 'storage.conf'),
            'HOME': session + '/home', 'PATH': '/usr/bin:/bin', 'TMPDIR': session + '/tmp', 'XDG_RUNTIME_DIR': session + '/xdg'})
        self.assertEqual(plan.argv.count('--mount'), 1)
        self.assertTrue(plan.name.startswith('cortex-inspector-') and len(plan.name) == 49)
        self.assertEqual(plan.session.parent, closure.root)

    def test_constructed_plans_and_unadmitted_closures_never_mint(self):
        closure = self.fx.admit()
        forged = inspector.InspectorClosure(*[getattr(closure, n) for n in (
            'policy_sha256', 'closure_sha256', 'root', 'members', 'image_digest', 'engine', 'engine_sha256')], inspector._SEAL)
        self.refuses('inspector_image_member_unbound', inspector.prepare_plan, forged, self.fx.profile, b'{}\n')
        self.refuses('inspector_image_member_unbound', inspector.InspectorPlan, (), (), 1, 1, 1, 1, 1)

    def test_T1c_engine_config_drift_refuses_the_run(self):
        closure = self.fx.admit()
        request = codec.canonical_bytes({'rows': []})
        plan = inspector.prepare_plan(closure, self.fx.profile, request)
        os.chmod(self.fx.host / 'containers.conf', 0o600)
        self.fx.write(self.fx.host / 'containers.conf', b'[containers]\nvolumes=["/x:/opt/cortex-inspector"]\n', 0o644)
        engine_calls = []
        with patch.object(inspector, '_engine', side_effect=lambda argv, *a: engine_calls.append(argv) or (0, b'')):
            self.refuses('inspector_closure_metadata', inspector.run_inspection, closure, plan, request)
        self.assertFalse(any('load' in call for call in engine_calls))
        self.assertTrue(any('rm' in call for call in engine_calls))


class FakeEngine:
    """Simulates the engine at the one seam; records every operation."""

    def __init__(self, fx, *, reply=None, run_exit=0, fail=None, inspect=None, ps=b''):
        self.fx, self.reply, self.run_exit, self.fail, self.inspect, self.ps = fx, reply, run_exit, fail, inspect, ps
        self.calls = []

    def __call__(self, argv, environment, session, timeout, output_limit):
        verb = argv[8]
        self.calls.append(verb)
        if self.fail == verb:
            raise subprocess.TimeoutExpired(list(argv), timeout)
        if verb == 'load':
            return 0, b''
        if verb == 'image':
            value = self.inspect or [{'Id': self.fx.config, 'Os': 'linux', 'Architecture': 'amd64',
                                      'RootFS': {'Layers': [self.fx.diff_id]}}]
            return 0, json.dumps(value).encode()
        if verb == 'run':
            src = next(a for a in argv if a.startswith('type=bind,src=')).split(',')[1][4:]
            request = Path(src).read_bytes()
            reply = core.respond(request)
            value = json.loads(reply)
            value['abi']['machine'] = 'x86_64'  # the fixture stands in for the amd64 host
            reply = codec.canonical_bytes(value)
            if self.reply:
                reply = self.reply(reply, request)
            return self.run_exit, reply
        if verb == 'ps':
            return 0, self.ps
        return 0, b''


class Contract(Base):
    """Hale H5 and v1 RED 7/10: one core, exact request-bound verdicts."""

    def session(self):
        return inspector._session(self.fx.admit(), self.fx.profile)

    def test_session_positive_through_the_contained_transport_seam(self):
        fake = FakeEngine(self.fx)
        with patch.object(inspector, '_engine', fake):
            self.session().check(rows(('a', b'x = 1\n'), ('b', b'y = 2.5\n')))
        self.assertEqual(fake.calls, ['load', 'image', 'run', 'rm', 'ps', 'unshare'])
        self.assertEqual([p for p in self.fx.root.iterdir() if p.name.startswith('session-')], [])

    def test_unequal_cache_refuses_as_compilation_policy(self):
        value = rows(('a', b'x = 1\n'))
        value[0]['cache_bytes'] = pyc(b'x = 2\n', '/app/a.py')[:8] + value[0]['cache_bytes'][8:16] + pyc(b'x = 2\n', '/app/a.py')[16:]
        with patch.object(inspector, '_engine', FakeEngine(self.fx)):
            self.refuses('python_compilation_policy', self.session().check, value)

    def test_reply_rows_reordered_duplicate_extra_missing_or_typed_wrong_refuse(self):
        def edit(change):
            def apply(reply, request):
                value = json.loads(reply)
                change(value)
                return codec.canonical_bytes(value)
            return apply
        cases = {'reordered': lambda v: v['verdicts'].reverse(),
                 'duplicate': lambda v: v['verdicts'].__setitem__(1, dict(v['verdicts'][0])),
                 'extra': lambda v: v['verdicts'].append({'cache': 'app/extra.pyc', 'equal': True}),
                 'missing': lambda v: v['verdicts'].pop(),
                 'int_not_bool': lambda v: v['verdicts'][0].__setitem__('equal', 1),
                 'extra_key': lambda v: v['verdicts'][0].__setitem__('reason', None),
                 'stale_request': lambda v: v.__setitem__('request_sha256', '0' * 64),
                 'unknown_top': lambda v: v.__setitem__('note', 'x')}
        for name, change in cases.items():
            with self.subTest(case=name):
                with patch.object(inspector, '_engine', FakeEngine(self.fx, reply=edit(change))):
                    self.refuses('inspector_run_incomplete', self.session().check, rows(('a', b'x = 1\n'), ('b', b'y = 2\n')))

    def test_one_verdict_cannot_cover_two_caches(self):
        def single(reply, request):
            value = json.loads(reply)
            value['verdicts'] = value['verdicts'][:1]
            return codec.canonical_bytes(value)
        with patch.object(inspector, '_engine', FakeEngine(self.fx, reply=single)):
            self.refuses('inspector_run_incomplete', self.session().check, rows(('a', b'x = 1\n'), ('b', b'x = 1\n')))

    def test_reply_abi_must_equal_the_admitted_closure(self):
        def other(reply, request):
            value = json.loads(reply)
            value['abi']['machine'] = 'aarch64'
            return codec.canonical_bytes(value)
        with patch.object(inspector, '_engine', FakeEngine(self.fx, reply=other)):
            self.refuses('inspector_abi_mismatch', self.session().check, rows(('a', b'x = 1\n')))

    def test_request_rows_must_be_unique_sorted_and_nonempty(self):
        pair = rows(('a', b'x = 1\n'), ('b', b'y = 2\n'))
        for bad in ([], list(reversed(pair)), [pair[0], pair[0]]):
            with self.subTest(n=len(bad)):
                with patch.object(inspector, '_engine', side_effect=AssertionError('engine ran')):
                    self.refuses('inspector_run_incomplete', self.session().check, bad)

    def test_source_cache_substitution_is_unequal(self):
        value = rows(('a', b'x = 1\n'), ('b', b'y = 2\n'))
        value[0]['source_bytes'], value[1]['source_bytes'] = value[1]['source_bytes'], value[0]['source_bytes']
        with patch.object(inspector, '_engine', FakeEngine(self.fx)):
            self.refuses('python_compilation_policy', self.session().check, value)

    def test_orphan_cache_refuses_in_the_controller(self):
        raw = pyc(b'x = 1\n', '/app/a.py')
        self.refuses('python_source_hash_policy', artifacts.pyc_row, 'app/__pycache__/a.%s.pyc' % TAG, raw, {}, cache_value())

    def test_vfs_checked_hash_requires_a_session_and_never_decodes(self):
        source = Path(product('vfs').__file__).read_text()
        self.assertIn("require(inspection is not None, 'native_inspector_execution_unqualified')", source)
        self.assertIn('inspection.check([pyc_row(', source)
        for word in ('import marshal', 'marshal.load', 'compile(', '_pyc_check'):
            self.assertNotIn(word, source)


class Core(unittest.TestCase):
    """v1 RED 7: the one core, in-process and as the standalone inspect.py."""

    def check(self, raw, source=b'x = 1\n', filename='/app/m.py'):
        core.check(raw, source, filename, 0)

    def test_equal_cache_passes_and_failures_have_the_frozen_reasons(self):
        source = b'x = 1\n'
        self.check(pyc(source, '/app/m.py'))
        cases = {'python_bytecode_shape': pyc(source, '/app/m.py') + b'trailing',
                 'python_final_filename_policy': pyc(source, '/tmp/builder/m.py'),
                 'python_compilation_policy': pyc(source, '/app/m.py', body=pyc(b'x = 2\n', '/app/m.py')[16:]),
                 'python_verifier_compiler_unqualified': b'\0\0\0\0' + pyc(source, '/app/m.py')[4:]}
        for reason, raw in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError) as caught:
                    self.check(raw)
                self.assertEqual(str(caught.exception), reason)

    def test_deep_code_constant_chain_keeps_the_original_shape_refusal(self):
        # cox R01-MOVE-001: the parent's recursive filename walk refused a 997-level
        # chain of nested code constants as python_bytecode_shape (RecursionError).
        source = b'x = 1\n'
        code = compile(source, '/app/module.py', 'exec', dont_inherit=True, optimize=0)
        chain = code
        for _ in range(997):
            chain = code.replace(co_consts=(chain,))
        import marshal
        raw = pyc(source, '/app/module.py')[:16] + marshal.dumps(chain)
        for check in (lambda: core.check(raw, source, '/app/module.py', 0),
                      lambda: artifacts._check_pyc('app/__pycache__/module.%s.pyc' % TAG, raw, {'app/module.py': source}, cache_value())):
            with self.assertRaises(ValueError) as caught:
                check()
            self.assertEqual(str(caught.exception), 'python_bytecode_shape')

    def test_fixture_path_delegates_to_the_same_core(self):
        source = b'x = 1\n'
        raw = pyc(source, '/app/m.py', body=pyc(b'x = 2\n', '/app/m.py')[16:])
        with patch.object(core, 'check', wraps=core.check) as shared:
            with self.assertRaises(ValueError):
                artifacts._check_pyc('app/__pycache__/m.%s.pyc' % TAG, raw, {'app/m.py': source}, cache_value())
        self.assertEqual(shared.call_count, 1)
        tree = Path(artifacts.__file__).read_text()
        self.assertNotIn('def _compiled_fields_equal', tree)
        self.assertNotIn('marshal.load', tree)
        self.assertNotIn('compile(', tree)

    def test_core_runs_standalone_isolated_without_cortex_core(self):
        source = b'x = 1\n'
        request = json.dumps({'schema': core.REQUEST_SCHEMA, 'policy_sha256': '0' * 64, 'closure_sha256': '0' * 64,
                              'abi': {'version': VERSION, 'magic': MAGIC}, 'optimization': 0,
                              'rows': [{'cache': 'c', 'source': 's', 'final_filename': '/app/m.py',
                                        'cache_b64': base64.b64encode(pyc(source, '/app/m.py')).decode(),
                                        'source_b64': base64.b64encode(source).decode()}]}).encode()
        program = ('import sys\nnamespace={"__name__":"inspector_core"}\n'
                   'exec(compile(open(sys.argv[1],"rb").read(),sys.argv[1],"exec"),namespace)\n'
                   'assert "cortex_core" not in sys.modules\n'
                   'sys.stdout.buffer.write(namespace["respond"](sys.stdin.buffer.read()))\n')
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', program, core.__file__], input=request,
                                capture_output=True, timeout=30, env={})
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertEqual(reply['verdicts'], [{'cache': 'c', 'equal': True}])
        self.assertEqual(reply['request_sha256'], sha(request))

    def test_core_never_executes_a_code_object(self):
        tree = Path(core.__file__).read_text()
        for word in ('exec(', 'eval(', 'FunctionType', 'marshal.loads'):
            self.assertNotIn(word, tree)


class Bounds(unittest.TestCase):
    """Hale H3: live output cap in the one adapter; count/depth walk."""

    def run_child(self, code, *, limit, timeout=10, kill_seconds=2):
        directory = Path(tempfile.mkdtemp(prefix='r3-bounds-'))
        self.addCleanup(shutil.rmtree, directory, True)
        started = time.monotonic()
        try:
            engine.run_operation([sys.executable, '-c', code], directory, {}, timeout, directory / 'o', directory / 'e',
                                 output_limit=limit, kill_seconds=kill_seconds)
            error = None
        except BaseException as caught:
            error = caught
        return error, (directory / 'o').read_bytes(), (directory / 'e').read_bytes(), time.monotonic() - started

    def test_stdout_flood_stderr_flood_and_both_stop_at_the_first_excess_byte(self):
        for name, code in {'stdout': 'import sys;sys.stdout.write("x"*100000)',
                           'stderr': 'import sys;sys.stderr.write("x"*100000)',
                           'both': 'import sys\nfor _ in range(200):sys.stdout.write("o"*1000);sys.stderr.write("e"*1000)'}.items():
            with self.subTest(stream=name):
                error, out, err, _ = self.run_child(code, limit=1024)
                self.assertIsInstance(error, OverflowError)
                self.assertLessEqual(len(out) + len(err), 1024)

    def test_exact_bound_passes_and_one_more_byte_fails(self):
        error, out, _, _ = self.run_child('import sys;sys.stdout.write("x"*1024)', limit=1024)
        self.assertIsNone(error)
        self.assertEqual(len(out), 1024)
        error, *_ = self.run_child('import sys;sys.stdout.write("x"*1025)', limit=1024)
        self.assertIsInstance(error, OverflowError)

    def test_timeout_with_term_ignored_is_bounded_by_the_kill_budget(self):
        code = 'import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(60)'
        error, _, _, elapsed = self.run_child(code, limit=1024, timeout=0.5, kill_seconds=1)
        self.assertIsInstance(error, subprocess.TimeoutExpired)
        self.assertLess(elapsed, 4)

    def test_count_and_depth_walk(self):
        self.assertEqual(inspector._shape({'a': [1, {'b': 2}]}), (5, 4))
        self.assertEqual(inspector._shape([]), (1, 1))

    def test_oversized_or_too_deep_request_refuses_before_any_run(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        closure = fx.admit()
        deep = codec.canonical_bytes({'a': {'b': {'c': {'d': {'e': {'f': {'g': {'h': {'i': 1}}}}}}}}})
        big = codec.canonical_bytes({'rows': ['x' * (LIMITS['input_bytes'])]})
        wide = codec.canonical_bytes({'rows': list(range(LIMITS['object_count']))})
        for request in (deep, big, wide, b'{"a":1} trailing'):
            with self.subTest(size=len(request)):
                with self.assertRaises(ValueError) as caught:
                    inspector.prepare_plan(closure, fx.profile, request)
                self.assertEqual(str(caught.exception), 'inspector_run_incomplete')

    def test_reply_over_budget_or_noncanonical_refuses(self):
        record = {'python': {'version': VERSION, 'magic': MAGIC}, 'platform': 'linux/amd64'}
        good = codec.canonical_bytes({'schema': core.RESPONSE_SCHEMA, 'request_sha256': sha(b'r'),
                                      'abi': {'version': VERSION, 'magic': MAGIC, 'machine': 'x86_64'},
                                      'verdicts': [{'cache': 'c', 'equal': True}]})
        self.assertEqual(inspector._reply_verdicts(good, b'r', record, ['c'], LIMITS), [True])
        for bad in (good[:-1], good + b'garbage', good.replace(b',', b', ', 1)):
            with self.assertRaises(ValueError) as caught:
                inspector._reply_verdicts(bad, b'r', record, ['c'], LIMITS)
            self.assertEqual(str(caught.exception), 'inspector_run_incomplete')
        with self.assertRaises(ValueError):
            inspector._reply_verdicts(good, b'r', record, ['c'], dict(LIMITS, output_bytes=len(good) - 1))


class Lifecycle(Base):
    """Hale H2: owned name, forced removal on every path, absence proven."""

    def run_with(self, fake, *, removed=True):
        closure = self.fx.admit()
        session = inspector._session(closure, self.fx.profile)
        with patch.object(inspector, '_engine', fake):
            with self.assertRaises(ValueError) as caught:
                session.check(rows(('a', b'x = 1\n')))
        left = [p for p in self.fx.root.iterdir() if p.name.startswith('session-')]
        if removed:
            self.assertEqual(left, [])
        else:
            # Hale R3-H2-CLEANUP-001: unproven absence preserves the owned roots for recovery.
            self.assertEqual(len(left), 1)
            self.assertTrue((left[0] / 'store').is_dir())
            self.assertNotIn('unshare', fake.calls)
        return str(caught.exception)

    def test_timeout_after_create_still_removes_by_name_and_proves_absence(self):
        fake = FakeEngine(self.fx, fail='run')
        self.assertEqual(self.run_with(fake), 'inspector_run_incomplete')
        self.assertEqual(fake.calls, ['load', 'image', 'run', 'rm', 'ps', 'unshare'])

    def test_nonzero_exit_or_wrong_loaded_image_refuses_and_cleans(self):
        self.assertEqual(self.run_with(FakeEngine(self.fx, run_exit=1)), 'inspector_run_incomplete')
        wrong = [{'Id': '3' * 64, 'Os': 'linux', 'Architecture': 'amd64', 'RootFS': {'Layers': [self.fx.diff_id]}}]
        fake = FakeEngine(self.fx, inspect=wrong)
        self.assertEqual(self.run_with(fake), 'inspector_run_incomplete')
        self.assertNotIn('run', fake.calls)

    def test_container_still_listed_after_removal_is_incomplete(self):
        self.assertEqual(self.run_with(FakeEngine(self.fx, ps=b'abc123\n'), removed=False), 'inspector_run_incomplete')

    def test_cleanup_command_failure_is_incomplete_never_pass(self):
        self.assertEqual(self.run_with(FakeEngine(self.fx, fail='rm'), removed=False), 'inspector_run_incomplete')

    def test_request_replaced_during_the_run_is_incomplete(self):
        fx = self.fx

        class Replacing(FakeEngine):
            def __call__(self, argv, environment, session, timeout, output_limit):
                result = super().__call__(argv, environment, session, timeout, output_limit)
                if argv[8] == 'run':
                    path = session / 'request' / 'request.json'
                    os.chmod(path, 0o644)
                    path.write_bytes(b'{}\n')
                    os.chmod(path, 0o444)
                return result
        self.assertIn(self.run_with(Replacing(fx)), ('inspector_closure_metadata', 'inspector_closure_changed'))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TimedEngine(FakeEngine):
    """Each verb costs fake seconds; a verb that outlives its timeout also spends the kill wait."""

    def __init__(self, fx, clock, costs, **options):
        super().__init__(fx, **options)
        self.clock, self.costs, self.timeouts = clock, costs, []

    def __call__(self, argv, environment, session, timeout, output_limit):
        verb = argv[8]
        self.timeouts.append((verb, timeout))
        cost = self.costs.get(verb, 0)
        if cost > timeout:
            self.clock.now += timeout + inspector.CLEANUP_SECONDS / 3
            self.calls.append(verb)
            raise subprocess.TimeoutExpired(list(argv), timeout)
        self.clock.now += cost
        return super().__call__(argv, environment, session, timeout, output_limit)


class Budget(Base):
    """Hale R3-H2-DEADLINE-001 / CLEANUP-001: one work deadline, one cleanup reserve, identity."""

    def test_work_budget_is_shared_across_batches_of_one_session(self):
        clock = Clock()
        fake = TimedEngine(self.fx, clock, {'load': 5, 'image': 5, 'run': 10})
        with patch.object(time, 'monotonic', clock), patch.object(inspector, '_engine', fake):
            session = inspector._session(self.fx.admit(), self.fx.profile)
            session.check(rows(('a', b'x = 1\n')))
            self.refuses('inspector_run_incomplete', session.check, rows(('b', b'y = 2\n')))
        self.assertLessEqual(clock.now, LIMITS['seconds'] + inspector.CLEANUP_SECONDS)

    def test_late_timeouts_never_exceed_work_plus_cleanup_reserve(self):
        clock = Clock()
        fake = TimedEngine(self.fx, clock, {'load': 5, 'image': 5, 'run': 1000, 'rm': 9, 'ps': 9, 'unshare': 1000})
        with patch.object(time, 'monotonic', clock), patch.object(inspector, '_engine', fake):
            session = inspector._session(self.fx.admit(), self.fx.profile)
            self.refuses('inspector_run_incomplete', session.check, rows(('a', b'x = 1\n')))
        self.assertLessEqual(clock.now, LIMITS['seconds'] + inspector.CLEANUP_SECONDS)

    def test_direct_cleanup_spends_one_reserve_including_kill_waits(self):
        closure = self.fx.admit()
        plan = inspector.prepare_plan(closure, self.fx.profile, codec.canonical_bytes({'rows': []}))
        plan.session.mkdir(mode=0o700)
        for name in ('store', 'run', 'tmp'):
            (plan.session / name).mkdir(mode=0o700)
        clock = Clock()
        fake = TimedEngine(self.fx, clock, {'rm': 9, 'ps': 9, 'unshare': 1000})
        with patch.object(time, 'monotonic', clock), patch.object(inspector, '_engine', fake):
            self.refuses('inspector_run_incomplete', inspector._cleanup, plan, dict(plan.environment), plan.session)
        self.assertLessEqual(clock.now, inspector.CLEANUP_SECONDS)

    def test_container_still_present_preserves_every_owned_root(self):
        sessions = []
        fake = FakeEngine(self.fx, ps=b'0123456789abcdef\n')
        def recording(argv, environment, session, timeout, output_limit):
            sessions.append(session)
            return fake(argv, environment, session, timeout, output_limit)
        with patch.object(inspector, '_engine', recording):
            session = inspector._session(self.fx.admit(), self.fx.profile)
            self.refuses('inspector_run_incomplete', session.check, rows(('a', b'x = 1\n')))
        self.assertNotIn('unshare', fake.calls)
        self.assertTrue((sessions[0] / 'store').is_dir() and (sessions[0] / 'run').is_dir())

    def test_same_uid_replacement_root_is_never_deleted(self):
        fake = FakeEngine(self.fx)
        seen = {}
        def replacing(argv, environment, session, timeout, output_limit):
            result = fake(argv, environment, session, timeout, output_limit)
            if argv[8] == 'run':
                held = session.with_name(session.name + '-held')
                session.rename(held)
                session.mkdir(mode=0o700)
                (session / 'store').mkdir(mode=0o700)
                marker = session / 'foreign-owned-marker'
                marker.write_text('same uid, different directory inode\n')
                seen.update(marker=marker, held=held, store=session / 'store')
            return result
        with patch.object(inspector, '_engine', replacing):
            session = inspector._session(self.fx.admit(), self.fx.profile)
            self.refuses('inspector_run_incomplete', session.check, rows(('a', b'x = 1\n')))
        self.assertTrue(seen['marker'].exists() and seen['store'].is_dir())
        self.assertNotIn('unshare', fake.calls)
        self.assertTrue(seen['held'].is_dir())


class Qualification(Base):
    """Vera R3-T2: eligibility never becomes qualification."""

    def verified(self):
        return SimpleNamespace(metadata={'release_policy': self.fx.profile},
                               document={'builder_requirements': {'toolchain': {'version': VERSION}},
                                         'target': {'oci_platform': 'linux/amd64'}})

    def test_T2a_valid_host_and_admitted_closure_stay_unqualified(self):
        host = {'os': 'linux', 'arch': 'x86_64', 'uid': 1000, 'rootless': True, 'emulated': False}
        closure = self.fx.admit()
        with patch.object(inspector, '_engine', side_effect=AssertionError('engine ran')):
            self.refuses('native_inspector_execution_unqualified', inspector.admit_inspection, self.verified(),
                         {'inspector_capability': closure, 'inspector_host': host})

    def test_T2b_missing_or_ineligible_host_stays_unqualified(self):
        closure = self.fx.admit()
        for host in (None, {'os': 'linux', 'arch': 'x86_64', 'uid': 0, 'rootless': True},
                     {'os': 'linux', 'arch': 'aarch64', 'uid': 1000, 'rootless': True}):
            with self.subTest(host=host):
                self.refuses('native_inspector_execution_unqualified', inspector.admit_inspection, self.verified(),
                             {'inspector_capability': closure, 'inspector_host': host})

    def test_T2c_fabricated_capability_is_not_admitted(self):
        closure = self.fx.admit()
        forged = inspector.InspectorClosure(closure.policy_sha256, closure.closure_sha256, closure.root, closure.members,
                                            closure.image_digest, closure.engine, closure.engine_sha256, inspector._SEAL,
                                            closure.descriptor)
        self.refuses('trusted_inspector_capability_unavailable', inspector.admit_inspection, self.verified(),
                     {'inspector_capability': forged})
        self.refuses('trusted_inspector_capability_unavailable', inspector._session, forged, self.fx.profile)

    def test_T2d_native_emission_guard_is_unchanged(self):
        source = Path(product('build.receipts').__file__).read_text()
        self.assertIn('raise ValueError("r02_native_emission_unqualified")', source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
