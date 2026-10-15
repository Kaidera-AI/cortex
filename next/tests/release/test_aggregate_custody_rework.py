"""Vera U3C-CUST-01..03: original delegated probes, source/fixture only.

Actual directory-entry and extra-file changes use private local inert originals.
Deadline probes advance only a controlled clock; native latency/custody NOT_RUN.
"""
import os
from pathlib import Path
from r01u3_support.workspace import workspace_output, renamed_directory_observation
import tempfile
import unittest
from unittest.mock import patch

from aggregate_fixture import materials, write, context, encode
from cortex_core.release import aggregate, assembly, custody
from cortex_core.runtime.signed_member import Refusal



MAIN=workspace_output()

class CustodyRework(unittest.TestCase):
    def setUp(self):
        MAIN.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='keyed-',dir=MAIN.resolve())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'rootcase'; self.root.mkdir(mode=0o700)
        self.value,self.bodies,self.builds,self.policy = materials(); write(self.root,self.bodies)
        self.reader = custody.RootReader.for_aggregate(str(self.root),8*1024**2)
        self.addCleanup(self.reader.close)

    def run_assembly(self):
        return assembly._assemble_original(encode(self.value),self.reader,self.builds,context(self.policy))

    def refusal(self,reason,fn=None):
        with self.assertRaises(Refusal) as caught: (fn or self.run_assembly)()
        self.assertEqual(caught.exception.reason,reason)
        self.assertIsNone(self.reader.fd)
        self.assertFalse(self.reader._aggregate_files)
        self.assertFalse(self.reader._aggregate_dirs)

    def test_U3C_CUST_01_case_only_root_rename_after_pair_validation(self):
        real=assembly._check_pairs;observed={};events=[]
        finish=self.reader.aggregate_finish
        def after(*args):
            real(*args);before=self.root.stat();renamed=self.root.with_name('Rootcase')
            self.root.rename(renamed)
            observed.update(renamed_directory_observation(self.root,renamed,before))
            events.append('hook completed')
        def terminal():events.append('terminal check');return finish()
        with patch.object(assembly,'_check_pairs',after),patch.object(self.reader,'aggregate_finish',terminal):
            with self.assertRaises(Refusal) as caught:self.run_assembly()
        self.assertEqual(caught.exception.reason,observed['reason'])
        self.assertEqual(events,['hook completed','terminal check'])
        self.refusal_cleanup()

    def refusal_cleanup(self):
        self.assertIsNone(self.reader.fd);self.assertFalse(self.reader._aggregate_files);self.assertFalse(self.reader._aggregate_dirs)

    def test_U3C_CUST_02_deadline_crossed_in_final_actual_fstat(self):
        real_bytes = self.reader.aggregate_bytes; real_fstat = custody.os.fstat
        real_clock = custody.time.monotonic
        state = dict(reads=0,final_check=False,expired=False,triggered=0)
        def read(path):
            raw = real_bytes(path); state['reads'] += 1
            if state['reads']==2*len(self.bodies): state['final_check']=True
            return raw
        def now():
            return self.reader._aggregate_deadline+1 if state['expired'] else real_clock()
        def fstat(fd):
            result = real_fstat(fd)
            if state['final_check'] and fd==list(self.reader._aggregate_files.values())[-1][0]:
                state['expired']=True; state['triggered']+=1
            return result
        with patch.object(self.reader,'aggregate_bytes',read),patch.object(custody.time,'monotonic',now),patch.object(custody.os,'fstat',fstat):
            self.refusal('raw_limit')
        self.assertEqual(state['reads'],2*len(self.bodies)); self.assertEqual(state['triggered'],1)

    def test_U3C_CUST_03_extra_physical_file_during_final_original_rehash(self):
        real = self.reader.aggregate_bytes; state = dict(reads=0,created=False)
        extra = self.root/'members/undeclared'
        def read(path):
            raw = real(path); state['reads']+=1
            if state['reads']==len(self.bodies)+1:
                extra.write_bytes(b'inert undeclared\n'); state['created']=True
            return raw
        with patch.object(self.reader,'aggregate_bytes',read): self.refusal('aggregate_pair_mismatch')
        self.assertTrue(state['created']); self.assertEqual(state['reads'],2*len(self.bodies))
        self.assertEqual(extra.stat().st_size,17)

    def test_matched_unsigned_original_positive_closes_all_descriptors(self):
        self.assertEqual(self.run_assembly(),encode(self.value))
        self.assertIsNone(self.reader.fd); self.assertFalse(self.reader._aggregate_files)
        self.assertFalse(self.reader._aggregate_dirs)

    def test_case_only_absolute_ancestor_rename_after_pair_validation(self):
        path=self.root.parent;renamed=path.with_name(path.name[0].upper()+path.name[1:])
        real=assembly._check_pairs;observed={};events=[];finish=self.reader.aggregate_finish
        def after(*args):
            real(*args);before=path.stat();path.rename(renamed)
            observed.update(renamed_directory_observation(path,renamed,before));events.append('hook completed')
        def terminal():events.append('terminal check');return finish()
        try:
            with patch.object(assembly,'_check_pairs',after),patch.object(self.reader,'aggregate_finish',terminal):
                with self.assertRaises(Refusal) as caught:self.run_assembly()
            self.assertEqual(caught.exception.reason,observed['reason'])
            self.assertEqual(events,['hook completed','terminal check']);self.refusal_cleanup()
        finally:
            if renamed.name in {e.name for e in renamed.parent.iterdir()}:renamed.rename(path)

    def test_missing_admitted_root_keeps_custody_changed_reason(self):
        real=assembly._check_pairs
        def after(*args): real(*args); self.root.rename(self.root.with_name('moved-originals'))
        with patch.object(assembly,'_check_pairs',after): self.refusal('member_custody_changed')

    def test_namespace_entry_drift_during_final_stat_is_custody_changed(self):
        real_bytes=self.reader.aggregate_bytes; real_fstat=custody.os.fstat
        state=dict(reads=0,created=False); extra=self.root/'members/late-undeclared'
        def read(path):
            raw=real_bytes(path); state['reads']+=1; return raw
        def fstat(fd):
            result=real_fstat(fd)
            if (state['reads']==2*len(self.bodies) and not state['created']
                    and fd==list(self.reader._aggregate_files.values())[-1][0]):
                extra.write_bytes(b'inert undeclared\n'); state['created']=True
            return result
        with patch.object(self.reader,'aggregate_bytes',read),patch.object(custody.os,'fstat',fstat):
            self.refusal('member_custody_changed')
        self.assertTrue(state['created']); self.assertEqual(state['reads'],2*len(self.bodies))


if __name__=='__main__': unittest.main()
