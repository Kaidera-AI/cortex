"""Local restoration, doctor and post-effect regression controls; no target."""
import copy
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import doctor
import restoration
from custody import Refusal, bound_json, json_bytes
from observations import ObservationStore
from support import Fixture, INSTANCE, VOLUME
from workflow import Workflow
import test_dispatch_receipts as dispatch_controls


class CompletionCase(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(); self.addCleanup(self.f.close)
        self.host = {'instance_id': INSTANCE, 'owner_uid': os.getuid(), 'boot_id': 'boot-one',
                     'engine': {'executable': self.f.B['verifier']}, 'host_key_binding': {},
                     'local_watchdog': {'state': 'ARMED'}, 'guest_watchdog': {'state': 'ARMED'}}
        identities = {name: {'container_id': format(n, '064x'), 'owner_uid': os.getuid(),
            'running': True, 'healthy': True, 'source_revision': self.f.B['kos_signed_binding']['source_revision']
            if name == 'kos-appdb' else self.f.B['source_revision']}
            for n, name in enumerate(restoration.SLOTS.values(), 1)}
        self.installed = {'instance_id': INSTANCE, 'container_identity_map': identities,
            'cortex_check': {'healthy': True}, 'initial_appdb_doctor': {'status': 'ready'},
            'cm1_sha256': 'a' * 64, 'credential_metadata': {'mode': 384}, 'kos_install_receipt': {}}
        self.armed = {'schema': 'cortex.gate-a.run8.restoration-armed.v2', **self.f.A.context,
            'instance_id': INSTANCE, 'owner_uid': os.getuid(), 'boot_id_before': 'boot-one',
            'unit_properties': {'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'active',
                'SubState': 'exited', 'Result': 'success', 'ExecMainStatus': 0, 'Restart': 'no', 'TimeoutStartUSec': 900000000},
            'linger': 'yes', 'same_owner_rootless_private_store': True, 'console_dependency_effective': True,
            'container_identity_map': identities, 'reboot_recovery': 'NOT_RUN',
            'cortex_check': self.installed['cortex_check'], 'appdb_doctor': self.installed['initial_appdb_doctor']}
        self.armed['unit_file'] = self.f.file('owner/restoration.service', restoration.render(self.f.A, self.installed, self.host, template_row=self.f.B['restoration']['template']))
        self.armed['console_dropin'] = self.f.file('owner/restoration.conf', (ROOT/'templates/console-dropin.conf.in').read_bytes())
        self.armed['input_checksum_map'] = self.f.file('owner/inputs.sha256', (self.f.B['verifier']['sha256']+'  '+self.f.B['verifier']['path']+'\n').encode())
        facts = {k:v for k,v in self.armed.items() if k not in {'schema',*self.f.A.context,'unit_file','console_dropin','input_checksum_map'}}
        self.armed['native_properties_receipt'] = self.f.file('raw/armed.json', json_bytes({'schema':'cortex.gate-a.run8.restoration-native.v2',**self.f.A.context,'phase':'ARMED','facts':facts}))

    def attempt(self, fn):
        try: return fn()
        except Refusal as error: return error.code

    def test_armed_properties_derived_from_raw(self):
        row = self.f.file('armed.json', json_bytes(self.armed))
        self.assertEqual(restoration.armed(self.f.A, self.installed, self.host, row)['reboot_recovery'], 'NOT_RUN')
        raw = bound_json(self.armed['native_properties_receipt']); raw['facts']['unit_properties']['ActiveState']='failed'
        self.armed['native_properties_receipt'] = self.f.file('raw/armed.json', json_bytes(raw))
        self.assertEqual(self.attempt(lambda: restoration.armed(self.f.A, self.installed, self.host, self.f.file('armed.json', json_bytes(self.armed)))), 'restoration_raw_facts_disagree')

    def test_restoration_engine_must_be_sealed_podman_role(self):
        host = copy.deepcopy(self.host); host['engine']['executable'] = self.f.file('tools/foreign-tool', b'foreign intact executable\n', mode=0o500)
        self.assertEqual(self.attempt(lambda: restoration.render(self.f.A, self.installed, host, template_row=self.f.B['restoration']['template'])), 'restoration_engine_not_sealed')

    def test_recovery_raw_and_boot_effects(self):
        R = {'schema':'cortex.gate-a.run8.recovery.v2', **self.f.A.context, 'instance_id':INSTANCE,
             'owner_uid':os.getuid(), 'boot_id_after':'boot-two', 'manual_start_or_repair':False,
             'unit_new_boot_success':True, 'container_identity_map':self.installed['container_identity_map'],
             'cortex_check':self.installed['cortex_check'], 'appdb_doctor':self.installed['initial_appdb_doctor'],
             'cm1_sha256':self.installed['cm1_sha256'], 'credential_metadata':self.installed['credential_metadata'],
             'denials_pass':True, 'console_health_pass':True, 'console_service_active':True}
        facts = {k:v for k,v in R.items() if k not in {'schema',*self.f.A.context}}
        raw = {'schema':'cortex.gate-a.run8.restoration-native.v2',**self.f.A.context,'phase':'REBOOT_VERIFIED','facts':facts}
        R['raw_evidence']={'facts':self.f.file('raw/recovery.json',json_bytes(raw))}
        self.assertEqual(restoration.recovered(self.f.A,self.installed,self.armed,self.f.file('recovery.json',json_bytes(R)))['boot_id_after'],'boot-two')
        raw['facts']['manual_start_or_repair']=True
        R['raw_evidence']['facts']=self.f.file('raw/recovery.json',json_bytes(raw))
        self.assertEqual(self.attempt(lambda: restoration.recovered(self.f.A,self.installed,self.armed,self.f.file('recovery.json',json_bytes(R)))), 'restoration_raw_facts_disagree')

    def test_extract_and_load_refuse_after_failure_before_callbacks(self):
        S=ObservationStore(self.f.root/'observations',self.f.A,create=True)
        S.append('PRE_ALLOC_CHECKED',{'checks':self.f.checks});S.append('ALLOCATED',{'instance_id':INSTANCE,'volume_id':VOLUME});S.append('HOST_BOUND',self.host)
        S.append('FAILURE',{'failed_phase':'HOST_BOUND','refusal':'fixture_failed'})
        flow=Workflow(self.f.A,S,signature_runner=self.f.signature_spy); effects=[]
        self.assertEqual(self.attempt(lambda:flow.extract(self.f.literals(),self.f.B['delivery_binding'],effects.append)), 'workflow_current_phase')
        self.assertEqual(self.attempt(lambda:flow.load_images(self.f.literals(),self.f.B['delivery_binding'],{'CORTEX_PROJECTS_ROOT':self.f.B['projects_root']},effects.append)), 'workflow_current_phase')
        self.assertEqual(effects,[])

    def doctor_fixture(self):
        app=self.f.root/'kos';runtime='fixture-runtime';prefix='kos/runtime/'+runtime
        python=self.f.file(prefix+'/python/bin/python3.12',b'fixture interpreter\n',mode=0o500)
        alias=Path(python['path']).with_name('python3');alias.symlink_to('python3.12')
        script=self.f.file(prefix+'/scripts/install/appdb-managed.py',b'fixture signed doctor\n')
        module=self.f.file(prefix+'/local-cortex/console/app/managed_appdb.py',b'fixture signed module\n')
        data=self.f.root/'kos-data';(data/'secrets').mkdir(parents=True,mode=0o700)
        K={'source_revision':'4'*40,'version':'0.2.009','platform':'linux-x86_64','manifest_sha256':'e'*64,
           'app_root':str(app),'data_root':str(data),'runtime':runtime,'service':'kaidera-dev-kos-console.service',
           'python_alias_target':'python3.12','python':python,'doctor':script,'managed_appdb':module}
        self.f.B['kos_signed_binding']=K
        R={'mode':'fresh','outcome':'installed','version':K['version'],'platform':K['platform'],
           'release_sha256':K['manifest_sha256'],'root':K['app_root'],'data_root':K['data_root'],
           'runtime':runtime,'service':K['service'],'appdb':{'managed':True,'port_chosen_by_the_manager':True,'port':55432}}
        D={key:'fixture-'+key for key in doctor.IDENTITY_FIELDS};D.update(status='ready',port=55432,database='harness_app',user='harness',platform='linux/amd64')
        return self.f.file('install-receipt.json',json_bytes(R)), D, data/'secrets'

    def test_signed_doctor_alias_config_identity_and_private_mode(self):
        row,D,config=self.doctor_fixture();calls=[]
        def runner(argv,**kw):calls.append((argv,kw));return SimpleNamespace(returncode=0,stdout=json_bytes(D),stderr=b'')
        initial=doctor.observe(self.f.A,row,runner=runner)
        self.assertEqual(calls[0][1]['env']['KOS_CONFIG_DIR'],str(config))
        self.assertTrue(calls[0][0][0].endswith('/python3.12'))
        D['port']=5432
        self.assertEqual(self.attempt(lambda:doctor.observe(self.f.A,row,runner=runner,initial=initial)),'appdb_doctor_effect')
        config.chmod(0o777);calls.clear()
        self.assertEqual(self.attempt(lambda:doctor.observe(self.f.A,row,runner=runner)),'kos_config_private_mode')
        self.assertEqual(calls,[])


class PostEffectCase(unittest.TestCase):
    setUp = dispatch_controls.DispatchReceiptCase.setUp
    spec = dispatch_controls.DispatchReceiptCase.spec
    store = dispatch_controls.DispatchReceiptCase.store
    call = dispatch_controls.DispatchReceiptCase.call
    attempt = dispatch_controls.DispatchReceiptCase.attempt
    def test_unusable_success_output_records_failure_and_invocation(self):
        for body in [b'invalid-json', b'']:
            with self.subTest(body=body):
                # Each effect needs a fresh admitted run, avoiding receipt reuse.
                if hasattr(self,'S'): self.f.close(); self.setUp()
                self.spec('manager','ALLOCATED');self.f.refresh();self.store('ALLOCATED');effects=[]
                def runner(*a,**k):effects.append(a);return SimpleNamespace(returncode=0,stdout=body,stderr=b'')
                self.attempt(lambda:self.call('manager',runner))
                rows,_=self.S.read()
                self.assertEqual(rows[-1]['phase'],'FAILURE')
                self.assertEqual(len(effects),1)
                self.assertEqual(len(list(self.invocations.glob('*.json'))),1)


if __name__=='__main__':unittest.main(verbosity=2)
