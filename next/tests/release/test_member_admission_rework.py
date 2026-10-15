"""U3-D reviewer-key regressions; actual source delivery, inert RAM fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_original_member_bridge import Fixture, MEMBER, NEXT
from cortex_core.release.custody import RootReader


class DeliveryRework(Fixture, unittest.TestCase):
    def test_U3C_CUST_01_case_only_root_rename_after_delivery_owner_reload(self):
        # Late reload is AFTER C finish: use D's actual final held/name check.
        delivery = self.backend()
        old = self.root
        renamed = old.with_name('B'+old.name[1:])
        before = old.stat()
        events = []
        def current(context):
            if len(self.contexts) == 6:
                old.rename(renamed)
                names = {entry.name for entry in os.scandir(old.parent)}
                self.assertNotIn(old.name, names)
                self.assertIn(renamed.name, names)
                self.assertEqual((old.stat().st_dev, old.stat().st_ino),
                                 (before.st_dev, before.st_ino))
                events.append('actual case-only rename after final delivery reload')
            return context
        self.current_hook = current
        self.refusal('member_path_alias', lambda: delivery.read_original(262144))
        self.assertEqual(len(events), 1)
        self.assertEqual(len(self.contexts), 6)
        self.assertEqual(self.transports, [self.aggregate]*2)
        self.assert_closed()

    def test_U3C_CUST_02_deadline_crosses_last_delivery_fstat(self):
        import cortex_core.release.member_admission as api
        clock = [100.0]
        crossed = []
        real_fstat = os.fstat
        def observed(fd):
            info = real_fstat(fd)
            if len(self.contexts) == 6 and not crossed:
                last = list(self.readers[-1]._aggregate_files.values())[-1][0]
                if fd == last:
                    clock[0] = 221.0
                    crossed.append(fd)
            return info
        with patch.object(api.time, 'monotonic', lambda: clock[0]):
            delivery = self.backend()
            with patch.object(os, 'fstat', observed):
                self.refusal('raw_limit', lambda: delivery.read_original(262144))
        self.assertEqual(len(crossed), 1)
        self.assertEqual(clock[0], 221.0)
        self.assertEqual(len(self.contexts), 6)
        self.assertEqual(self.transports, [self.aggregate]*2)
        self.assert_closed()

    def test_U3C_CUST_03_extra_physical_member_after_delivery_owner_reload(self):
        delivery = self.backend()
        extra = self.root/'members/undeclared'
        def current(context):
            if len(self.contexts) == 6:
                extra.write_bytes(b'undeclared member')
            return context
        self.current_hook = current
        self.refusal('aggregate_pair_mismatch', lambda: delivery.read_original(262144))
        self.assertEqual(extra.read_bytes(), b'undeclared member')
        self.assertEqual(len(self.contexts), 6)
        self.assertEqual(self.transports, [self.aggregate]*2)
        self.assert_closed()


    def test_U3C_CUST_03_namespace_entry_during_last_delivery_fstat(self):
        # Matched same-key terminal window: a real namespace change during the
        # actual final held-file stat, after the last owner DATA reload.
        delivery = self.backend()
        extra = self.root/'members/late-undeclared'
        events = []
        real_fstat = os.fstat
        def observed(fd):
            info = real_fstat(fd)
            if len(self.contexts) == 6 and not events:
                last = list(self.readers[-1]._aggregate_files.values())[-1][0]
                if fd == last:
                    extra.write_bytes(b'undeclared member')
                    events.append(fd)
            return info
        with patch.object(os, 'fstat', observed):
            self.refusal('member_custody_changed', lambda: delivery.read_original(262144))
        self.assertEqual(len(events), 1)
        self.assertEqual(extra.read_bytes(), b'undeclared member')
        self.assertEqual(len(self.contexts), 6)
        self.assert_closed()


    def test_U3C_CUST_02_deadline_crosses_actual_delivery_cleanup(self):
        import cortex_core.release.member_admission as api
        clock = [100.0]
        crossed = []
        close = RootReader.close
        def observed(reader):
            close(reader)
            if len(self.contexts) == 6 and reader is self.readers[-1] and not crossed:
                self.assertIsNone(reader.fd)
                self.assertFalse(reader._aggregate_files)
                clock[0] = 221.0
                crossed.append('after actual terminal close')
        with patch.object(api.time, 'monotonic', lambda: clock[0]):
            delivery = self.backend()
            with patch.object(RootReader, 'close', observed):
                self.refusal('raw_limit', lambda: delivery.read_original(262144))
        self.assertEqual(len(crossed), 1)
        self.assertEqual(len(self.contexts), 6)
        self.assert_closed()


class ColdRework(unittest.TestCase):
    def test_U3D_COLD_01_fresh_import_has_zero_schema_reads(self):
        # Each order starts in a separate unprewarmed interpreter. Keep Python's
        # ordinary module-loader reads distinct from explicit schema I/O.
        code = r'''import importlib,json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
assert not any(n.startswith(('cortex_core','jsonschema')) for n in sys.modules)
reads=[]
for method in ('read_text','read_bytes'):
 original=getattr(Path,method)
 def observe(self,*args,_original=original,**kwargs):
  if '/schemas/' in str(self) or '/contracts/' in str(self): reads.append(str(self))
  return _original(self,*args,**kwargs)
 setattr(Path,method,observe)
first=importlib.import_module(sys.argv[2])
from cortex_core.release import member_admission as backend
from cortex_core.runtime import signed_member as port
class Trap:
 def __eq__(self,other): raise AssertionError('candidate equality')
 def __str__(self): raise AssertionError('candidate text')
 def __fspath__(self): raise AssertionError('candidate path')
for fn in (backend.request_original_member,port.request_member):
 try: fn(Trap(),Trap())
 except port.Refusal as e:
  assert e.reason=='required_binding_gap' and e.unit==port.MISSING_UNIT
 else: raise AssertionError('default gap absent')
assert 'cortex_core.release.aggregate' not in sys.modules
assert 'cortex_core.release.trust' not in sys.modules
print(json.dumps({'reads':reads,'codec_loaded':'cortex_core.release.codec' in sys.modules,'jsonschema_loaded':'jsonschema' in sys.modules}))
assert reads==[], 'BODY: cold default import read packaged schemas: '+repr(reads)
assert 'cortex_core.release.codec' not in sys.modules
assert 'jsonschema' not in sys.modules
'''
        for first in ('cortex_core.release.member_admission',
                      'cortex_core.runtime.signed_member'):
            with self.subTest(first=first):
                run = subprocess.run([sys.executable, '-B', '-X',
                    'pycache_prefix='+str(NEXT/'cold-unused-cache'), '-c', code,
                    str(NEXT/'src'), first], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
                observed = json.loads(run.stdout)
                self.assertEqual(observed['reads'], [])
                self.assertFalse(observed['codec_loaded'])
                self.assertFalse(observed['jsonschema_loaded'])
