"""Family 1: installer-side protected bootstrap producer (Focus gB2).

The installer measures the selected interpreter/entry/stdlib/loader/crypto/data
originals BEFORE the owner (V) runs, using the consumer's own reader
(inspector._read_member) as the one rule set, and records them canonically. The
fixed B1 producer then delivers that record as _Inputs.release_startup /
_StartupContext. Fixture files are inert and never executed. Real values
(CPython 3.12.15, cryptography 50.0.0, cffi 2.1.1, pycparser 3.0) come from the
real host in group C/D.
"""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

NEXT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(NEXT / 'src'), str(NEXT / 'tests/release')]
from r01u3_support.workspace import temporary_directory
from cortex_core.runtime.signed_member import Refusal

PY = '/opt/cortex/python/bin/python3.12'
ENTRY = '/opt/cortex/libexec/cortex-owner.py'
STD = '/opt/cortex/python/lib/python3.12'
SITE = STD + '/site-packages'
SRC = '/opt/cortex/next/src'
BUILD = b'INERT CPython 3.12.15 interpreter fixture, NOT EXECUTED\n'
FILES = {
    PY: ('interpreter', BUILD, 0o755),
    ENTRY: ('source', b'# INERT owner entry\n', 0o644),
    SRC + '/cortex_core/release/member_admission.py': ('source', b'# INERT\n', 0o644),
    STD + '/os.py': ('stdlib', b'# INERT stdlib\n', 0o644),
    STD + '/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so': ('loader', b'INERT loader\n', 0o644),
    SITE + '/_cffi_backend.cpython-312-x86_64-linux-gnu.so': ('loader', b'INERT cffi backend\n', 0o644),
    SITE + '/cryptography/hazmat/bindings/_rust.abi3.so': ('crypto', b'INERT rust bindings\n', 0o644),
    SITE + '/cryptography/__init__.py': ('crypto', b'# INERT\n', 0o644),
    SITE + '/cryptography-50.0.0.dist-info/METADATA': ('data', b'Metadata-Version: 2.4\nName: cryptography\nVersion: 50.0.0\n\nbody\n', 0o644),
    SITE + '/cffi-2.1.1.dist-info/METADATA': ('data', b'Metadata-Version: 2.1\nName: cffi\nVersion: 2.1.1\n\n', 0o644),
    SITE + '/pycparser-3.0.dist-info/METADATA': ('data', b'Metadata-Version: 2.1\nName: pycparser\nVersion: 3.0\n\n', 0o644),
}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def selection(**change):
    doc = {'schema': 'cortex.release-bootstrap-selection.v1', 'bootstrap_id': 'host-epoch-1',
           'pins': {'cpython': '3.12.15', 'cryptography': '50.0.0', 'cffi': '2.1.1', 'pycparser': '3.0',
                    'cpython_build_sha256': hashlib.sha256(BUILD).hexdigest()},
           'executable': PY, 'entrypoint': ENTRY, 'python_version': [3, 12, 15],
           'import_roots': [STD, STD + '/lib-dynload', SITE, SRC], 'cache_prefix': '/opt/cortex/pycache',
           'intrinsics': ['_imp', 'builtins', 'sys'],
           'module_aliases': [['_cffi_backend', SITE + '/_cffi_backend.cpython-312-x86_64-linux-gnu.so']],
           'dist_metadata': {'cryptography': SITE + '/cryptography-50.0.0.dist-info/METADATA',
                             'cffi': SITE + '/cffi-2.1.1.dist-info/METADATA',
                             'pycparser': SITE + '/pycparser-3.0.dist-info/METADATA'},
           'closure': sorted([[role, path] for path, (role, _, _) in FILES.items()], key=lambda r: r[1])}
    doc.update(change)
    return doc


def module():
    try:
        return importlib.import_module('cortex_core.install.bootstrap')
    except ModuleNotFoundError as error:
        if error.name != 'cortex_core.install.bootstrap':
            raise
        raise AssertionError('SURFACE_ABSENCE: cortex_core.install.bootstrap') from None


class Producer(unittest.TestCase):
    def setUp(self):
        temp = temporary_directory('bootstrap-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for path, (_, raw, mode) in FILES.items():
            real = self.root / path[1:]
            real.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            real.write_bytes(raw)
            real.chmod(mode)
        (self.root / 'opt/cortex/pycache').mkdir(mode=0o700)

    def produce(self, doc=None):
        m = module()
        with patch.object(m, '_ROOT', self.root):
            return m.measure_bootstrap(canonical(selection() if doc is None else doc))

    def refused(self, reason, fn):
        result = None
        try:
            fn()
        except Exception as error:
            result = error
        self.assertIsInstance(result, Refusal, 'BODY: accepted or leaked a non-refusal')
        self.assertEqual(result.reason, reason, 'BODY: exact refusal')

    # ---- positives --------------------------------------------------------------
    def test_P1_record_is_canonical_and_measures_every_original(self):
        m = module()
        raw = self.produce()
        doc = json.loads(raw)
        self.assertEqual(raw, canonical(doc))
        self.assertEqual(doc['schema'], 'cortex.release-bootstrap.v1')
        rows = {r[1]: r for r in doc['files']}
        self.assertEqual(set(rows), set(FILES))
        for path, (role, body, mode) in FILES.items():
            self.assertEqual(rows[path], [role, path, len(body), hashlib.sha256(body).hexdigest(), mode])
        self.assertEqual(self.produce(), raw, 'BODY: not deterministic')

    def test_P2_default_root_is_fixed_and_not_a_parameter(self):
        m = module()
        self.assertEqual(m._ROOT, Path('/'))
        import inspect
        self.assertEqual(list(inspect.signature(m.measure_bootstrap).parameters), ['selection_raw'])

    def test_P3_record_delivers_a_startup_context_the_real_consumer_admits(self):
        m = module()
        from cortex_core.release import member_admission
        raw = self.produce()
        sys.modules.setdefault('_cffi_backend', type(sys)('_cffi_backend'))
        kw = dict(owner_binding=('a' * 64, 1, 1), current=lambda: None, originals=lambda _: None)
        # V runs with -I -S: exactly the two standard path hooks (zipimporter, FileFinder).
        with patch.object(sys, 'path_hooks', sys.path_hooks[:2]):
            frame = m.startup_context(raw, **kw)
            self.assertIs(type(frame), member_admission._StartupContext)
            identity = member_admission._startup_identity(frame)  # the existing consumer accepts it
            self.assertEqual(frame.path_hooks, tuple(sys.path_hooks))
        self.assertEqual(identity[6], tuple(tuple(r) for r in json.loads(raw)['files']))
        self.assertIs(frame.module_aliases[0][1], sys.modules['_cffi_backend'])
        # A polluted interpreter (an extra .pth path hook) is carried truthfully and refused.
        with patch.object(sys, 'path_hooks', sys.path_hooks[:2] + [lambda path: None]):
            polluted = m.startup_context(raw, **kw)
        self.refused('required_binding_gap', lambda: member_admission._startup_identity(polluted))

    def test_P4_every_row_reverifies_with_the_consumer_reader_then_tamper_refuses(self):
        from cortex_core.release import inspector
        raw = self.produce()
        rows = json.loads(raw)['files']
        for role, path, size, digest, mode in rows:
            inspector._read_member(self.root, dict(role=role, path=path[1:], bytes=size, sha256=digest, mode=mode))
        (self.root / STD[1:] / 'os.py').write_bytes(b'# TAMPERED after measurement\n')
        role, path, size, digest, mode = next(r for r in rows if r[1].endswith('/os.py'))
        with self.assertRaises(Exception):
            inspector._read_member(self.root, dict(role=role, path=path[1:], bytes=size, sha256=digest, mode=mode))

    # ---- selection ----------------------------------------------------------------
    def test_S1_selection_is_closed_canonical_and_pins_are_the_selected_closure(self):
        m = module()
        self.assertEqual(m.SELECTED, {'cpython': '3.12.15', 'cryptography': '50.0.0', 'cffi': '2.1.1', 'pycparser': '3.0'})
        bad = [selection(extra=1), selection(schema='v2'), selection(python_version=[3, 12, 14]),
               selection(pins=dict(selection()['pins'], cryptography='50.0.1')),
               selection(pins=dict(selection()['pins'], cffi='2.1.0')),
               selection(pins={k: v for k, v in selection()['pins'].items() if k != 'pycparser'}),
               selection(executable='python3.12'), selection(import_roots=[]),
               selection(intrinsics=['sys', 'builtins']),
               selection(closure=list(reversed(selection()['closure']))),
               selection(closure=selection()['closure'] + [selection()['closure'][0]]),
               selection(closure=[['shell', PY]] + selection()['closure'][1:])]
        for doc in bad:
            self.refused('bootstrap_selection_invalid', lambda doc=doc: self.produce(doc))
        self.refused('bootstrap_selection_invalid', lambda: module().measure_bootstrap(json.dumps(selection()).encode()))

    def test_S2_executable_and_entrypoint_must_be_measured_in_their_roles(self):
        doc = selection()
        doc['closure'] = [r if r[1] != PY else ['stdlib', PY] for r in doc['closure']]
        self.refused('bootstrap_selection_invalid', lambda: self.produce(doc))
        doc = selection(entrypoint=STD + '/os.py')
        self.refused('bootstrap_selection_invalid', lambda: self.produce(doc))
        doc = selection(module_aliases=[['_cffi_backend', STD + '/os.py']])
        self.refused('bootstrap_selection_invalid', lambda: self.produce(doc))
        # The interpreter row stays, but the executable names another file.
        self.refused('bootstrap_selection_invalid', lambda: self.produce(selection(executable=STD + '/os.py')))
        # Every required role must be present (here: no crypto originals at all).
        doc = selection(); doc['closure'] = [r for r in doc['closure'] if r[0] != 'crypto']
        self.refused('bootstrap_selection_invalid', lambda: self.produce(doc))
        # Version metadata must be a measured closure row.
        doc = selection(dist_metadata=dict(selection()['dist_metadata'], cffi=SITE + '/cffi-elsewhere/METADATA'))
        self.refused('bootstrap_selection_invalid', lambda: self.produce(doc))
        self.assertTrue(self.produce())  # matched positive

    # ---- measurement ----------------------------------------------------------------
    def test_M1_symlink_original_refuses_then_restore(self):
        real = self.root / STD[1:] / 'os.py'
        real.rename(real.with_name('os-real.py'))
        real.symlink_to('os-real.py')
        self.refused('bootstrap_original_refused', self.produce)
        real.unlink(); real.with_name('os-real.py').rename(real)
        self.assertTrue(self.produce())

    def test_M2_symlinked_or_writable_ancestor_refuses_then_restore(self):
        lib = self.root / STD[1:] / 'lib-dynload'
        lib.chmod(0o777)
        self.refused('bootstrap_original_refused', self.produce)
        lib.chmod(0o755)
        self.assertTrue(self.produce())

    def test_M3_writable_hardlinked_or_missing_original_refuses_then_restore(self):
        target = self.root / ENTRY[1:]
        target.chmod(0o666)
        self.refused('bootstrap_original_refused', self.produce)
        target.chmod(0o644)
        link = self.root / 'opt/cortex/extra-link'
        os.link(target, link)
        self.refused('bootstrap_original_refused', self.produce)
        link.unlink()
        target.rename(target.with_suffix('.gone'))
        self.refused('bootstrap_original_refused', self.produce)
        target.with_suffix('.gone').rename(target)
        self.assertTrue(self.produce())

    def test_M4_interpreter_build_must_be_the_pinned_build(self):
        (self.root / PY[1:]).write_bytes(b'INERT CPython 3.12.14 other build\n')
        (self.root / PY[1:]).chmod(0o755)
        self.refused('bootstrap_pin_mismatch', self.produce)

    def test_M5_dist_metadata_versions_must_equal_the_pins(self):
        meta = self.root / (SITE + '/cryptography-50.0.0.dist-info/METADATA')[1:]
        meta.write_bytes(b'Metadata-Version: 2.4\nName: cryptography\nVersion: 50.0.1\n\n')
        self.refused('bootstrap_pin_mismatch', self.produce)
        meta.write_bytes(b'Metadata-Version: 2.4\nName: not-cryptography\nVersion: 50.0.0\n\n')
        self.refused('bootstrap_pin_mismatch', self.produce)
        meta.write_bytes(b'Metadata-Version: 2.4\nName: cryptography\nVersion: 50.0.0\nVersion: 50.0.1\n\n')
        self.refused('bootstrap_pin_mismatch', self.produce)
        meta.write_bytes(FILES[SITE + '/cryptography-50.0.0.dist-info/METADATA'][1])
        self.assertTrue(self.produce())

    def test_M6_nothing_is_executed_or_imported_from_the_closure(self):
        import ast
        m = module()
        tree = ast.parse(Path(m.__file__).read_text())
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(names & {'subprocess', 'importlib', 'runpy', 'ctypes', 'socket'})
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(calls & {'exec', 'eval', 'compile', '__import__'})

    # ---- delivery -------------------------------------------------------------------
    def test_D1_delivery_refuses_bad_records_and_unloaded_aliases(self):
        m = module()
        raw = self.produce()
        doc = json.loads(raw)
        kw = dict(owner_binding=('a' * 64, 1, 1), current=lambda: None, originals=lambda _: None)
        sys.modules['_cffi_backend'] = type(sys)('_cffi_backend')  # aliases loaded: only the record can refuse
        for bad in (raw.replace(b'"files"', b'"filez"'), canonical(dict(doc, extra=1)), b'{}\n'):
            self.refused('bootstrap_record_invalid', lambda bad=bad: m.startup_context(bad, **kw))
        with patch.dict(sys.modules, {}, clear=False):
            sys.modules.pop('_cffi_backend', None)
            self.refused('bootstrap_record_invalid', lambda: m.startup_context(raw, **kw))
        sys.modules['_cffi_backend'] = type(sys)('_cffi_backend')
        self.assertTrue(m.startup_context(raw, **kw))  # matched positive

    def test_D2_record_rides_the_existing_B1_carrier_unchanged(self):
        m = module()
        from cortex_core.install import owner
        sys.modules.setdefault('_cffi_backend', type(sys)('_cffi_backend'))
        frame = m.startup_context(self.produce(), owner_binding=('a' * 64, 1, 1), current=lambda: None, originals=lambda _: None)
        inputs = owner._Inputs(b'{}', {}, b'{}', 'sha256:' + 'b' * 64, release_startup=frame)
        from dataclasses import replace
        self.assertIs(replace(inputs, context_raw=b'{"x":1}').release_startup, frame)


if __name__ == '__main__':
    unittest.main(verbosity=2)
