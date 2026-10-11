"""Verify source/load/project guards are on the executable dispatcher path."""
import copy
import hashlib
import io
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import dispatch
from custody import Refusal, actual_source_file, json_bytes, source_record
from observations import ObservationStore
from support import Fixture,INSTANCE,VOLUME


class ClosedDispatchCase(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        role='runtime';spec={'phase':'HOST_BOUND','argv':[self.f.B['verifier']['path']],
            'shell':False,'timeout_sec':30,'input_digests':{'source':'9'*64}}
        self.f.B['commands']={'HOST_BOUND':{role:spec},role:spec}
        self.f.B['phase_command_inputs']['HOST_BOUND'][role]=spec['input_digests'];self.f.refresh()
        self.S=ObservationStore(self.f.root/'observations',self.f.A,create=True)
        self.S.append('PRE_ALLOC_CHECKED',{'checks':self.f.checks});self.S.append('ALLOCATED',{'instance_id':INSTANCE,'volume_id':VOLUME})
        self.S.append('HOST_BOUND',{'instance_id':INSTANCE,'owner_uid':1001,'boot_id':'fixture-boot','engine':{},
            'host_key_binding':{},'local_watchdog':{'state':'ARMED'},'guest_watchdog':{'state':'ARMED'},
            'admitted_command_inputs':self.f.B['phase_command_inputs']['HOST_BOUND']})
        self.directory=self.f.root/'invocations';self.directory.mkdir(mode=0o700);self.calls=[]

    def execute(self,E):
        def runner(*a,**kw):self.calls.append((a,kw));return SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
        return dispatch.execute(self.f.A,self.S,'runtime',E,runner=runner,invocation_directory=self.directory)

    def test_closed_source_refuses_before_runtime_effect(self):
        E={**self.f.literals(),'CORTEX_PROJECTS_ROOT':self.f.B['projects_root']}
        bad=copy.deepcopy(self.f.install);bad['files']={}
        self.f.file('payload/packages/deploy/install.json',json_bytes(bad))
        refused=False
        try:self.execute(E)
        except (Refusal,ValueError):refused=True
        self.assertTrue(refused,'invalid closed payload reached runtime')
        self.assertEqual(self.calls,[])

    def test_projects_guard_and_export_before_runtime_effect(self):
        E={**self.f.literals(),'CORTEX_PROJECTS_ROOT':'/usr'}
        refused=False
        try:self.execute(E)
        except Refusal:refused=True
        self.assertTrue(refused,'unadmitted projects root reached runtime')
        self.assertEqual(self.calls,[])
        E['CORTEX_PROJECTS_ROOT']=self.f.B['projects_root'];self.execute(E)
        self.assertEqual(self.calls[0][1]['env']['CORTEX_PROJECTS_ROOT'],self.f.B['projects_root'])

    def test_archive_source_admits_before_extract_and_actual_target_after(self):
        body=b'public fixture executable\n';name='packages/deploy/tool';p=self.f.root/'archive.tar'
        with tarfile.open(p,'w') as t:
            entry=tarfile.TarInfo(name);entry.size=len(body);t.addfile(entry,io.BytesIO(body))
        from custody import measured_file
        p.chmod(0o600);target=self.f.root/'not-yet-extracted/tool'
        row={'kind':'archive_member','archive':measured_file(p),'member':name,'target_path':str(target),
             'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}
        source_record(row);self.assertFalse(target.exists())
        with self.assertRaises(Refusal):actual_source_file(row)
        target.parent.mkdir(mode=0o700);target.write_bytes(body);target.chmod(0o500)
        self.assertEqual(actual_source_file(row),target)
        bad=dict(row,sha256='f'*64)
        with self.assertRaises(Refusal):source_record(bad)


if __name__=='__main__':unittest.main(verbosity=2)
