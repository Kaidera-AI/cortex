"""U3-E A source oracle. Inert observations are labelled SOURCE SIMULATION.

Fresh subprocess cases separately exercise real isolated startup, observer,
original-file hashes and B/C/D/RC2 read-only DATA. No installed/B/native credit.
"""
import base64
from dataclasses import FrozenInstanceError, replace
import hashlib
import importlib.machinery as machinery
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from r01u3_support.startup import StartupFixture, isolated_original_control, MAIN
from test_original_member_bridge import MEMBER, NEXT
from cortex_core.runtime import signed_member as port


class StartupBoundary(StartupFixture, unittest.TestCase):
    def test_positive_source_simulation_measures_every_original_and_owner_binding(self):
        api=self.init_boundary()
        owner,stamp=self.check()
        self.assertEqual(owner.policy_sha256,self.frame.owner_binding[0])
        self.assertEqual((owner.generation,owner.fence),self.frame.owner_binding[1:])
        self.assertEqual(len(stamp),len(self.files))
        self.assertEqual({row[1] for row in stamp},{row[1] for row in self.files})
        self.assertEqual(self.transports,[])
        self.assertFalse(getattr(owner,'effect_authority',False))

    def test_source_simulation_actual_port_preserves_original_bytes_and_no_effect(self):
        self.init_boundary()
        def effect(*args,**kwargs):self.fail('A attempted a process/network effect')
        with self.composition(),patch('socket.socket',effect),patch('subprocess.Popen',effect),patch('os.system',effect):
            member=port.request_member(self.aggregate,MEMBER)
        self.assertIs(type(member),port.OriginalMember)
        self.assertEqual(port.read_member(member,aggregate_id=self.aggregate,member_name=MEMBER),self.bodies[MEMBER])
        self.assertEqual(self.transports,[self.aggregate]*2)
        self.assertGreaterEqual(len(self.startup_calls),7)
        self.assertFalse(hasattr(member,'effect_authority'));self.assertFalse(hasattr(member,'native_qualified'))
        self.assert_closed()

    def test_T14_default_has_no_public_bootstrap_or_root_binder(self):
        api=self.init_boundary()
        class Trap:
            def __eq__(self,other):raise AssertionError('candidate equality')
            def __str__(self):raise AssertionError('candidate text')
            def __fspath__(self):raise AssertionError('candidate path')
        with patch.dict(os.environ,{'CORTEX_TEST_ROOT':str(self.install),'CORTEX_TRUSTED':'true','R01_TEST_MODE':'1'}):
            for fn in (api.request_original_member,port.request_member):
                error=self.refusal('required_binding_gap',lambda:fn(Trap(),Trap()))
                self.assertEqual(error.unit,port.MISSING_UNIT)
        self.assertFalse(any(not name.startswith('_') and any(word in name for word in ('bind','test_root','startup_context')) for name in vars(api)))
        self.assertEqual(self.transports,[])

    def test_startup_DATA_constructor_cannot_bind_production(self):
        api=self.init_boundary()
        with self.assertRaises(FrozenInstanceError):self.frame.bootstrap_id='forged'
        self.refusal('required_binding_gap',lambda:api.request_original_member(self.aggregate,MEMBER))
        self.assertEqual(self.transports,[])

    def test_missing_bootstrap_refuses_before_candidates(self):
        api=self.init_boundary()
        with patch.object(api,'_installed_startup',lambda:None):
            self.refusal('required_binding_gap',lambda:api.request_original_member(object(),object()))
        self.assertEqual(self.transports,[]);self.assertEqual(self.owner_calls,[])

    def test_dictionary_claims_are_not_independent_startup_DATA(self):
        api=self.init_boundary()
        value=vars(self.frame)|{'trusted':True,'verified':True,'effect_authority':True}
        self.refusal('required_binding_gap',lambda:api._admit_startup(value))
        self.assertEqual(self.transports,[]);self.assertEqual(self.owner_calls,[])

    def test_bad_frame_field_types_do_not_reach_owner_or_physical_reads(self):
        api=self.init_boundary()
        for fields in ({'bootstrap_id':True},{'python_version':(3,14,True)},
                       {'import_roots':list(self.roots)},{'files':list(self.files)},
                       {'owner_binding':('a'*64,True,1)},{'originals':None},{'current':None}):
            with self.subTest(fields=tuple(fields)):
                self.frame=replace(self.frame,**fields)
                self.refusal('required_binding_gap',self.check)
                self.init_boundary()
        self.assertEqual(self.transports,[])

    def test_executable_and_entrypoint_are_required_originals(self):
        self.init_boundary()
        self.frame=replace(self.frame,files=tuple(r for r in self.files if r[0]!='interpreter'))
        self.refusal('required_binding_gap',self.check)
        self.assertEqual(self.transports,[])

    def test_duplicate_original_paths_refuse(self):
        self.init_boundary();self.frame=replace(self.frame,files=(self.files[0],)+self.files)
        self.refusal('required_binding_gap',self.check)

    def test_unsafe_original_path_refuses_before_outside_read(self):
        self.init_boundary();row=list(self.files[0]);row[1]='/outside/../not-admitted'
        self.frame=replace(self.frame,files=(tuple(row),)+self.files[1:])
        self.refusal('required_binding_gap',self.check)

    def test_T24_test_support_is_forbidden_even_if_owner_table_names_it(self):
        self.init_boundary();row=self.add_original('src/r01u3_support/factory.py')
        self.frame=replace(self.frame,files=tuple(sorted((*self.files,row),key=lambda r:r[1])))
        self.replace_observation(modules=(*self.observation.modules,('r01u3_support.factory',row[1],machinery.SourceFileLoader,())))
        self.refusal('member_authority_unavailable',self.check)
        self.assertEqual(self.transports,[])

    def test_T24_loaded_module_origin_must_be_in_independent_file_table(self):
        self.init_boundary();row=self.add_original('src/ambient.py')
        self.replace_observation(modules=(*self.observation.modules,('ambient',row[1],machinery.SourceFileLoader,())))
        self.refusal('member_authority_unavailable',self.check)

    def test_T24_package_search_locations_cannot_escape_admitted_roots(self):
        self.init_boundary();items=list(self.observation.modules)
        items[3]=('cortex_core',None,machinery.NamespaceLoader,(str(self.install/'outside'),))
        self.replace_observation(modules=tuple(items));self.refusal('member_authority_unavailable',self.check)

    def test_T24_sourceless_loader_is_refused(self):
        self.init_boundary();items=list(self.observation.modules);name,path,_,locations=items[-1]
        items[-1]=(name,path,machinery.SourcelessFileLoader,locations)
        self.replace_observation(modules=tuple(items));self.refusal('member_authority_unavailable',self.check)

    def test_T24_intrinsic_module_must_be_in_independent_interpreter_inventory(self):
        self.init_boundary();self.replace_observation(modules=(*self.observation.modules,('unadmitted_builtin','built-in',machinery.BuiltinImporter,())))
        self.refusal('member_authority_unavailable',self.check)

    def test_T29_same_named_fake_meta_importer_is_refused(self):
        self.init_boundary()
        fake=type('PathFinder',(),{'__module__':'_frozen_importlib_external'})
        self.replace_observation(meta_path=(machinery.BuiltinImporter,machinery.FrozenImporter,fake))
        self.refusal('member_authority_unavailable',self.check)

    def test_T24_changed_path_hook_is_refused(self):
        self.init_boundary();self.replace_observation(path_hooks=(self.frame.path_hooks[0],lambda *a:None))
        self.refusal('member_authority_unavailable',self.check)

    def test_T24_custom_cached_finder_is_refused(self):
        self.init_boundary();self.replace_observation(importers=((self.roots[0],object(),()),))
        self.refusal('member_authority_unavailable',self.check)

    def test_T24_cached_loader_substitution_is_refused(self):
        self.init_boundary();self.replace_observation(importers=((self.roots[0],machinery.FileFinder,(('.py',object),)),))
        self.refusal('member_authority_unavailable',self.check)

    def test_T24_nonempty_private_cache_refuses_even_with_B(self):
        self.init_boundary();(self.cache/'unadmitted.pyc').write_bytes(b'INERT cache, NOT EXECUTED')
        self.refusal('member_authority_unavailable',self.check)

    def test_T29_changed_source_original_refuses(self):
        self.init_boundary();path=Path(next(r[1] for r in self.files if r[0]=='source'))
        path.write_bytes(b'changed source, NOT EXECUTED')
        self.refusal('member_custody_changed',self.check)

    def test_T29_changed_crypto_original_refuses(self):
        self.init_boundary();path=Path(next(r[1] for r in self.files if r[0]=='crypto'))
        raw=path.read_bytes();path.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        self.refusal('member_custody_changed',self.check)

    def test_T29_symlink_original_refuses(self):
        self.init_boundary();path=Path(next(r[1] for r in self.files if r[0]=='source'))
        target=path.with_name('elsewhere');path.rename(target);path.symlink_to(target)
        self.refusal('member_custody_changed',self.check)

    def test_T29_writable_by_group_original_refuses(self):
        self.init_boundary();Path(self.files[0][1]).chmod(0o666)
        self.refusal('member_custody_changed',self.check)

    def test_owner_policy_generation_is_not_self_reported(self):
        self.init_boundary();sha,generation,fence=self.frame.owner_binding
        self.frame=replace(self.frame,owner_binding=(sha,generation+1,fence))
        self.refusal('aggregate_replay',self.check)

    def test_owner_root_policy_pin_is_not_selected_by_startup(self):
        self.init_boundary();_,generation,fence=self.frame.owner_binding
        self.frame=replace(self.frame,owner_binding=('f'*64,generation,fence))
        self.refusal('aggregate_replay',self.check)

    def test_missing_current_owner_DATA_keeps_named_gap(self):
        self.init_boundary();self.frame=replace(self.frame,current=lambda:None)
        self.refusal('required_binding_gap',self.check)

    def test_owner_exception_is_sanitized_without_private_details(self):
        api=self.init_boundary()
        def fail():raise ValueError('PRIVATE_OWNER_DETAIL_NEVER_EXPORT')
        self.frame=replace(self.frame,current=fail)
        with self.composition():
            error=self.refusal('member_authority_unavailable',lambda:api.request_original_member(self.aggregate,MEMBER))
        self.assertNotIn('PRIVATE_OWNER_DETAIL',str(error));self.assertEqual(self.transports,[])

    def test_T28_changed_bootstrap_epoch_invalidates_retained_delivery(self):
        api=self.init_boundary()
        with self.composition():
            delivery=api.request_original_member(self.aggregate,MEMBER)
            self.frame=replace(self.frame,bootstrap_id='source-fixture-epoch-two')
            self.refusal('aggregate_replay',lambda:delivery.read_original(262144))
        self.assertEqual(self.transports,[self.aggregate]);self.assert_closed()

    def test_T28_same_byte_code_replacement_changes_custody_before_delivery(self):
        api=self.init_boundary()
        with self.composition():
            delivery=api.request_original_member(self.aggregate,MEMBER)
            path=Path(next(r[1] for r in self.files if r[0]=='source'))
            other=path.with_name('replacement');other.write_bytes(path.read_bytes());other.chmod(path.stat().st_mode & 0o777);other.replace(path)
            self.refusal('member_custody_changed',lambda:delivery.read_original(262144))
        self.assertEqual(self.transports,[self.aggregate]);self.assert_closed()

    def test_T28_new_import_origin_after_initial_admission_is_rechecked(self):
        api=self.init_boundary()
        with self.composition():
            delivery=api.request_original_member(self.aggregate,MEMBER)
            self.replace_observation(modules=(*self.observation.modules,('later','/outside/later.py',machinery.SourceFileLoader,())))
            self.refusal('member_authority_unavailable',lambda:delivery.read_original(262144))
        self.assertEqual(self.transports,[self.aggregate]);self.assert_closed()

    def test_T28_sealed_bytes_remain_DATA_when_new_startup_refuses(self):
        self.init_boundary()
        with self.composition():
            member=port.request_member(self.aggregate,MEMBER)
            self.replace_observation(import_roots=(*self.roots,'/ambient'))
            self.refusal('member_authority_unavailable',lambda:port.request_member(self.aggregate,MEMBER))
        self.assertEqual(port.read_member(member,aggregate_id=self.aggregate,member_name=MEMBER),self.bodies[MEMBER])
        self.assertFalse(hasattr(member,'effect_authority'));self.assertFalse(hasattr(member,'native_qualified'))
        self.assert_closed()

    def test_original_measurement_obeys_terminal_existing_budget(self):
        api=self.init_boundary();clock=[100.0]
        from cortex_core.release import inspector
        original=inspector._read_member;reads=[]
        def measured(*args):
            result=original(*args);reads.append(args[1]['path'])
            if len(reads)==len(self.files):clock[0]=221.0
            return result
        with patch.object(api.time,'monotonic',lambda:clock[0]),patch.object(inspector,'_read_member',measured):
            self.refusal('raw_limit',self.check)
        self.assertEqual(len(reads),len(self.files))
        self.assertEqual(self.transports,[])

    def test_startup_original_total_uses_existing_owner_tightening(self):
        self.init_boundary();self.policy['limits']['aggregate_bytes']=1
        from aggregate_fixture import encode
        self.frame=replace(self.frame,owner_binding=(hashlib.sha256(encode(self.policy)).hexdigest(),self.authority.generation,self.authority.fence))
        self.refusal('raw_limit',self.check);self.assertEqual(self.transports,[])

    def test_actual_isolated_source_originals_through_public_port(self):
        self.init_boundary();result=isolated_original_control(self)
        self.assertEqual(result['status'],'original-data',result)
        self.assertEqual(result['raw'],result['expected_original'])
        self.assertEqual(result['type'],'OriginalMember')
        self.assertEqual(result['counts']['transport'],2)
        self.assertGreaterEqual(result['counts']['bootstrap'],7)
        self.assertEqual(result['forbidden'],[])
        self.assertEqual(result['flags'],[1,1,1,1,True,0,True])
        self.assertFalse(result['effect_attribute']);self.assertFalse(result['native_attribute'])
        self.assertFalse(result['test_support_loaded']);self.assertFalse(result['physical_install_qualified'])
        self.assertTrue(result['actual_observation']);self.assertEqual(result['bootstrap_provenance'],'SOURCE_FIXTURE_ONLY')

    def test_actual_isolated_omitted_idna_closure_refuses_before_transport(self):
        self.init_boundary();result=isolated_original_control(self,fault='omit-idna')
        self.assertEqual(result['status'],'refused',result)
        self.assertEqual(result['reason'],'member_authority_unavailable')
        self.assertEqual(result['counts']['transport'],0);self.assertEqual(result['forbidden'],[])
        self.assertTrue(any(name=='idna' or name.startswith('idna.') for name in result['unadmitted_files']))
        self.assertEqual(result['measurement_errors'],[])

    def test_inert_source_writable_private_parent_refuses(self):
        self.init_boundary()
        (self.install/'stdlib').chmod(0o770)
        self.refusal('member_custody_changed',self.check)
        self.assertEqual(self.transports,[])

    def test_actual_isolated_writable_copied_parent_refuses_before_transport(self):
        self.init_boundary();result=isolated_original_control(self,fault='copied-parent')
        self.assertEqual(result['status'],'refused',result)
        self.assertEqual(result['reason'],'member_custody_changed')
        self.assertEqual(result['counts']['transport'],0);self.assertEqual(result['forbidden'],[])
        self.assertTrue(any(row['reason']=='inspector_closure_parent' for row in result['measurement_errors']))

    def test_actual_isolated_unadmitted_origin_refuses_before_original_transport(self):
        self.init_boundary();result=isolated_original_control(self,fault='origin')
        self.assertEqual(result['status'],'refused');self.assertEqual(result['reason'],'member_authority_unavailable')
        self.assertEqual(result['counts']['transport'],0);self.assertEqual(result['forbidden'],[])
        self.assertFalse(result['physical_install_qualified'])


def observation_fault(field,value):
    def case(self):
        self.init_boundary();self.replace_observation(**{field:value(self) if callable(value) else value})
        self.refusal('member_authority_unavailable',self.check)
        self.assertEqual(self.transports,[])
    return case

for name,field,value in [
    ('wrong_executable','executable','/outside/python'),
    ('wrong_entrypoint','entrypoint','/outside/entry.py'),
    ('wrong_python_version','python_version',(9,9,9)),
    ('ambient_import_root','import_roots',lambda s:(*s.roots,'/ambient')),
    ('reordered_import_roots','import_roots',lambda s:tuple(reversed(s.roots))),
    ('wrong_cache_prefix','cache_prefix','/outside/cache'),
    ('ambient_python_env','environment',(('PYTHONPATH','/ambient'),)),
    ('ambient_loader_env','environment',(('LD_PRELOAD','/ambient/library.so'),)),
    ('ambient_test_root_env','environment',(('CORTEX_TEST_ROOT','/ambient/root'),)),
    ('missing_isolated','flags',(0,1,1,1,True,0,True)),
    ('environment_not_ignored','flags',(1,0,1,1,True,0,True)),
    ('site_enabled','flags',(1,1,0,1,True,0,True)),
    ('user_site_enabled','flags',(1,1,1,0,True,0,True)),
    ('unsafe_initial_path','flags',(1,1,1,1,False,0,True)),
    ('optimized_python','flags',(1,1,1,1,True,2,True)),
    ('bytecode_writes_enabled','flags',(1,1,1,1,True,0,False)),
]:setattr(StartupBoundary,'test_T24_'+name,observation_fault(field,value))


class IsolatedDefault(unittest.TestCase):
    def test_real_isolated_startup_ignores_ambient_code_and_keeps_gap_before_candidates(self):
        MAIN.mkdir(mode=0o700,parents=True,exist_ok=True)
        with __import__('tempfile').TemporaryDirectory(prefix='ambient-',dir=MAIN) as name:
            home=Path(name);marker=home/'EXECUTED';cache=home/'empty-cache';cache.mkdir()
            (home/'sitecustomize.py').write_text('from pathlib import Path;Path('+repr(str(marker))+').write_text("BAD")')
            (home/'startup.pth').write_text('import sitecustomize\n')
            code=r'''import sys,os,json
from pathlib import Path
assert not any(n.startswith(('cortex_core','jsonschema','r01u3_support')) for n in sys.modules)
sys.path.insert(0,sys.argv[1])
reads=[]
for method in ('read_text','read_bytes'):
 original=getattr(Path,method)
 def observed(self,*a,_original=original,**kw):
  if '/schemas/' in str(self) or '/contracts/' in str(self):reads.append(str(self))
  return _original(self,*a,**kw)
 setattr(Path,method,observed)
from cortex_core.release import member_admission as a
from cortex_core.runtime import signed_member as port
class Trap:
 def __eq__(self,other):raise AssertionError('candidate equality')
 def __str__(self):raise AssertionError('candidate text')
 def __fspath__(self):raise AssertionError('candidate path')
for fn in (a.request_original_member,port.request_member):
 try:fn(Trap(),Trap())
 except port.Refusal as e:assert e.reason=='required_binding_gap' and e.unit==port.MISSING_UNIT
 else:raise AssertionError('default gap absent')
assert 'cortex_core.release.trust' not in sys.modules
assert 'cortex_core.release.aggregate' not in sys.modules
assert 'jsonschema' not in sys.modules
print(json.dumps({'reads':reads,'flags':[sys.flags.isolated,sys.flags.ignore_environment,sys.flags.no_site,sys.flags.no_user_site,sys.flags.safe_path,sys.flags.optimize,sys.dont_write_bytecode],'test_support':any(n.startswith('r01u3_support') for n in sys.modules),'sitecustomize':'sitecustomize' in sys.modules}))
'''
            env=dict(os.environ,PYTHONPATH=str(home),PYTHONHOME='/unadmitted',PYTHONUSERBASE=str(home),PYTHONOPTIMIZE='2',PYTHONDONTWRITEBYTECODE='0',CORTEX_TEST_ROOT=str(home),CORTEX_TRUSTED='true')
            run=subprocess.run([sys.executable,'-I','-S','-B','-X','pycache_prefix='+str(cache),'-c',code,str(NEXT/'src')],env=env,cwd=home,capture_output=True,text=True,timeout=20)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr);result=json.loads(run.stdout)
            self.assertEqual(result['reads'],[]);self.assertEqual(result['flags'],[1,1,1,1,True,0,True])
            self.assertFalse(result['test_support']);self.assertFalse(result['sitecustomize']);self.assertFalse(marker.exists())


class CurrentJoinAndBudget(StartupFixture, unittest.TestCase):
    def test_current_main_runtime_join_uses_exact_original_operations_pin(self):
        self.init_boundary()
        from aggregate_fixture import encode,sha256
        from cortex_core.runtime.config import admit_config
        cfg=json.loads(json.loads((NEXT/'tests/install/fixtures/p1-subjects.json').read_bytes())['configs']['linux/arm64'])
        cfg['runtime_lock_sha256']=sha256(self.bodies[MEMBER])
        cfg['operations_sha256']=sha256(self.bodies[self.value['compatibility']['operations_member']])
        cfg['metrics_labels']['release']=self.value['release']['release_id']
        with self.composition():member=port.request_member(self.aggregate,MEMBER)
        plan=admit_config(member,encode(cfg),aggregate_id=self.aggregate,observed_target=cfg['target'],mode='inspect')
        self.assertEqual(plan.lock_sha256,sha256(self.bodies[MEMBER]))
        self.assertFalse(plan.effect_authority);self.assertFalse(plan.native_qualified)
        self.assertEqual(plan.authority_status,'UNBOUND');self.assert_closed()

    def test_startup_budget_includes_actual_reader_cleanup(self):
        api=self.init_boundary();clock=[100.0];closed=[]
        from cortex_core.release.custody import RootReader
        real=RootReader.close
        def cleanup(reader):
            real(reader)
            if getattr(reader,'_aggregate_deadline',None)==220.0:
                self.assertIsNone(reader.fd);clock[0]=221.0;closed.append('actual close')
        with patch.object(api.time,'monotonic',lambda:clock[0]),patch.object(RootReader,'close',cleanup):
            self.refusal('raw_limit',self.check)
        self.assertEqual(closed,['actual close'])


class AdditionalAdmissionControls(StartupFixture, unittest.TestCase):
    def test_file_package_search_locations_cannot_escape_roots(self):
        self.init_boundary();modules=list(self.observation.modules)
        name,origin,kind,_=modules[-1];modules[-1]=(name,origin,kind,('/outside/package',))
        self.replace_observation(modules=tuple(modules))
        self.refusal('member_authority_unavailable',self.check)

    def test_code_file_count_obeys_existing_owner_tightening(self):
        self.init_boundary();self.policy['limits']['member_count']=1
        from aggregate_fixture import encode
        self.frame=replace(self.frame,owner_binding=(hashlib.sha256(encode(self.policy)).hexdigest(),self.authority.generation,self.authority.fence))
        self.refusal('raw_limit',self.check);self.assertEqual(self.transports,[])

    def test_startup_budget_includes_current_owner_reload_before_file_reads(self):
        api=self.init_boundary();clock=[100.0]
        current=self.frame.current
        def late():
            result=current();clock[0]=221.0;return result
        self.frame=replace(self.frame,current=late)
        from cortex_core.release import inspector
        with patch.object(api.time,'monotonic',lambda:clock[0]),patch.object(inspector,'_read_member',side_effect=AssertionError('expired metadata admission read file')):
            self.refusal('raw_limit',self.check)
        self.assertEqual(self.transports,[])


class RetainedPathHookIdentity(StartupFixture, unittest.TestCase):
    """Hale's U3EA-HOOK-001 witness; existing SOURCE SIMULATION only."""
    def test_retained_delivery_refuses_changed_bootstrap_path_hook_identity(self):
        api = self.init_boundary()
        with self.composition():
            delivery = api.request_original_member(self.aggregate, MEMBER)
            old = self.frame
            new_hook = machinery.FileFinder.path_hook(
                (machinery.ExtensionFileLoader, machinery.EXTENSION_SUFFIXES),
                (machinery.SourceFileLoader, machinery.SOURCE_SUFFIXES),
                (machinery.SourcelessFileLoader, machinery.BYTECODE_SUFFIXES))
            new_hooks = (old.path_hooks[0], new_hook)
            self.frame = replace(old, path_hooks=new_hooks)
            self.replace_observation(path_hooks=new_hooks)
            self.refusal('aggregate_replay', lambda: delivery.read_original(262144))


class OwnerSourceJoin(StartupFixture, unittest.TestCase):
    """EB1 SOURCE SIMULATION: real R01/RC2 in the existing owner, no effects.

    Bootstrap/owner observations are test-owned DATA. Neither fixture originals
    nor this read-only proposal qualify protected installation, image or B.
    """
    def owner_data(self):
        import test_owner_proposal as oracle
        helper = oracle.OwnerProposalOracle()
        data = helper.data()
        request = json.loads(data['request_raw'])
        # Derive test-owned P1 originals from the actual signed R01 fixture.
        # Never alter the frozen owner corpus or inject a verifier result.
        release = request['release']
        release['joint_index_ref'] = dict(release['joint_index_ref'],
                                         bytes=len(self.raw), sha256=hashlib.sha256(self.raw).hexdigest())
        for index, record in enumerate(release['artifact_refs']):
            if record['path'] == MEMBER:
                original = self.bodies[MEMBER]
                release['artifact_refs'][index] = dict(record, bytes=len(original),
                                                       sha256=hashlib.sha256(original).hexdigest())
        data['aggregate_id'] = self.aggregate
        data['request_raw'] = oracle.encode(request).decode()
        helper.edit_record(data, 'request_ref', lambda value: value.update(request))
        context = json.loads(data['context_raw'])
        consent = json.loads(data['originals'][context['consent_ref']['path']])
        scope = json.loads(data['originals'][consent['scope_ref']['path']])
        reference = scope['release_binding_ref']
        raw = oracle.encode(release)
        data['originals'][reference['path']] = raw.decode()
        helper.scope(data, lambda value: value.update(release_binding_ref=dict(
            reference, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())))
        return helper, data

    def owner_source(self, *, data=None, fresh=None, request=None, entry=None):
        from cortex_core.install import owner
        helper, original = self.owner_data()
        data = original if data is None else data
        values = [helper.inputs(owner, data), helper.inputs(owner, data if fresh is None else fresh)]
        calls = []
        def installed():
            calls.append('owner-observation')
            return values[min(len(calls)-1, 1)]
        raw = data['request_raw'].encode() if request is None else request
        # Only test-owned startup/current DATA is substituted. The existing
        # RC2 port, R01 backend and real cryptography remain the deciding path.
        with self.composition(), patch.object(owner, '_installed_owner_inputs', installed):
            result = (owner.propose_effect if entry is None else entry)(raw)
        return result, calls

    def owner_refusal(self, detail, **kwargs):
        from cortex_core.install import owner
        with self.assertRaises(owner.OwnerRefusal) as caught:
            self.owner_source(**kwargs)
        self.assertEqual(caught.exception.detail, detail)
        return caught.exception

    def test_EB1_actual_signature_originals_and_owner_DATA_join(self):
        self.init_boundary()
        from cortex_core.install import owner
        from cortex_core.release import trust
        with patch.object(trust, 'verify_ed25519', wraps=trust.verify_ed25519) as crypto:
            result, calls = self.owner_source()
        self.assertGreaterEqual(crypto.call_count, 4)
        self.assertGreaterEqual(len(self.transports), 2)
        self.assertEqual(calls, ['owner-observation', 'owner-observation'])
        self.assertIs(type(result), owner.OwnerProposal)
        self.assertEqual(result.stage, 'PRECONSUME_DATA')
        self.assertFalse(result.effect_authority)
        self.assertFalse(result.native_qualified)
        self.assertFalse(result.reservation_created)
        self.assertFalse(result.consumed)
        self.assertEqual(result.deadline.expires_at, 1700000005)
        self.assertIs(port._original_authority, port._unbound)
        self.assert_closed()

    def test_EB1_invalid_signature_refuses_before_lifecycle_binding(self):
        self.init_boundary()
        from cortex_core.install import lifecycle
        lines = self.signature.split(b'\n')
        signature = bytearray(base64.b64decode(lines[3])); signature[0] ^= 1
        lines[3] = base64.b64encode(signature); self.signature = b'\n'.join(lines)
        with patch.object(lifecycle, '_installed_lifecycle',
                          side_effect=AssertionError('EB1 reached finite backend')) as backend:
            error = self.owner_refusal('r01_other_refusal', entry=lifecycle.execute_effect)
        self.assertEqual(error.r01_reason, 'aggregate_signature_invalid')
        self.assertEqual(backend.call_count, 0)
        self.assert_closed()

    def test_EB1_unsigned_original_refuses(self):
        self.init_boundary(); self.signature = None
        error = self.owner_refusal('r01_other_refusal')
        self.assertEqual(error.r01_reason, 'aggregate_unsigned')
        self.assert_closed()

    def test_EB1_substituted_root_refuses(self):
        self.init_boundary()
        from r01u3_support.factory import test_authority
        self.authority.root_material = test_authority().root_material
        error = self.owner_refusal('r01_other_refusal')
        self.assertEqual(error.r01_reason, 'aggregate_root_mismatch')
        self.assert_closed()

    def test_EB1_corrupt_original_lock_refuses(self):
        self.init_boundary()
        raw = self.bodies[MEMBER]
        (self.root/MEMBER).write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        error = self.owner_refusal('r01_other_refusal')
        self.assertEqual(error.r01_reason, 'member_digest_mismatch')
        self.assert_closed()

    def test_EB1_nonce_drift_after_real_audit_refuses(self):
        self.init_boundary()
        from cortex_core.release import trust
        helper, data = self.owner_data()
        import copy
        fresh = copy.deepcopy(data)
        helper.context(fresh, lambda context: context.update(nonce='different-source-nonce'))
        with patch.object(trust, 'verify_ed25519', wraps=trust.verify_ed25519) as crypto:
            self.owner_refusal('nonce_replay', data=data, fresh=fresh)
        self.assertGreaterEqual(crypto.call_count, 4)
        self.assert_closed()

    def test_EB1_deadline_after_real_audit_is_not_renewed(self):
        self.init_boundary()
        from cortex_core.release import trust
        helper, data = self.owner_data()
        import copy
        fresh = copy.deepcopy(data)
        helper.clock(fresh, lambda clock: clock.update(now_utc=1700000005))
        with patch.object(trust, 'verify_ed25519', wraps=trust.verify_ed25519) as crypto:
            self.owner_refusal('reservation_expired', data=data, fresh=fresh)
        self.assertGreaterEqual(crypto.call_count, 4)
        self.assert_closed()

    def test_EB1_client_authority_field_refuses_before_R01(self):
        self.init_boundary()
        from cortex_core.release import trust
        helper, data = self.owner_data()
        request = json.loads(data['request_raw']); request['trusted'] = True
        with patch.object(trust, 'verify_ed25519', wraps=trust.verify_ed25519) as crypto:
            self.owner_refusal('invalid_request', data=data, request=__import__('test_owner_proposal').encode(request))
        self.assertEqual(crypto.call_count, 0)
        self.assertEqual(self.transports, [])

    def test_EB1_peer_mismatch_refuses_before_R01(self):
        self.init_boundary()
        helper, data = self.owner_data()
        helper.edit_record(data, 'peer_ref', lambda peer: peer.update(owner_uid=502))
        self.owner_refusal('peer_denied', data=data)
        self.assertEqual(self.transports, [])

    def test_EB1_default_owner_gap_precedes_client_and_R01(self):
        self.init_boundary()
        from cortex_core.install import owner
        class Trap:
            def __getattribute__(self, name):
                raise AssertionError('EB1 touched client before owner binding')
        with self.composition(), self.assertRaises(owner.OwnerRefusal) as caught:
            owner.propose_effect(Trap())
        self.assertEqual(caught.exception.detail, 'required_binding_gap')
        self.assertEqual(self.startup_calls, [])
        self.assertEqual(self.transports, [])

    def test_EB1_cached_snapshot_is_not_an_owner_request(self):
        self.init_boundary()
        with self.composition():
            member = port.request_member(self.aggregate, MEMBER)
        count = len(self.transports)
        self.owner_refusal('invalid_request', request=member)
        self.assertEqual(len(self.transports), count)
        self.assertFalse(hasattr(member, 'effect_authority'))
        self.assert_closed()

    def test_EB1_wrong_first_signer_with_authentic_comment_refuses(self):
        self.init_boundary()
        from cortex_core.release import trust
        from r01u3_support.factory import test_authority
        # Isolate the first guard: the expected key still signs the comment.
        other = test_authority()
        message = hashlib.blake2b(self.raw, digest_size=64).digest()
        first = other._signer.sign(message).signature
        lines = self.signature.split(b'\n')
        trusted = lines[2][len(b'trusted comment: '):]
        second = self.authority._signer.sign(first + trusted).signature
        self.assertFalse(trust.verify_ed25519(self.authority.public, message, first))
        self.assertTrue(trust.verify_ed25519(self.authority.public, first + trusted, second))
        lines[1] = base64.b64encode(b'ED' + self.authority.key_id + first)
        lines[3] = base64.b64encode(second)
        self.signature = b'\n'.join(lines)
        error = self.owner_refusal('r01_other_refusal')
        self.assertEqual(error.r01_reason, 'aggregate_signature_invalid')
        self.assert_closed()

    def test_EB1_request_digest_must_match_verified_original_lock(self):
        self.init_boundary()
        import test_owner_proposal as oracle
        helper, data = self.owner_data()
        request = json.loads(data['request_raw'])
        row = next(r for r in request['release']['artifact_refs'] if r['path'] == MEMBER)
        row['sha256'] = 'f' * 64
        self.assertNotEqual(row['sha256'], hashlib.sha256(self.bodies[MEMBER]).hexdigest())
        # Keep physical/signed originals valid, and coherently rebind the
        # retained request and consent scope so only the owner measure decides.
        data['request_raw'] = oracle.encode(request).decode()
        helper.edit_record(data, 'request_ref', lambda value: value.update(request))
        context = json.loads(data['context_raw'])
        consent = json.loads(data['originals'][context['consent_ref']['path']])
        scope = json.loads(data['originals'][consent['scope_ref']['path']])
        reference = scope['release_binding_ref']
        raw = oracle.encode(request['release'])
        data['originals'][reference['path']] = raw.decode()
        helper.scope(data, lambda value: value.update(release_binding_ref=dict(
            reference, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())))
        error = self.owner_refusal('r01_other_refusal', data=data)
        self.assertEqual(error.r01_reason, 'member_digest_mismatch')
        self.assert_closed()

    def test_EB2_effect_joins_real_R01_and_terminal_SQL_barrier(self):
        from cortex_core.install import lifecycle as p1
        from cortex_core.release.contracts import thaw
        from psycopg.pq import TransactionStatus
        with self.sql_source() as s:
            published = []
            original = p1._record_result
            def record(*args, **kwargs):
                self.assertEqual(s.reader.info.transaction_status, TransactionStatus.INTRANS,
                                 'EB2 terminal publication lost the SQL barrier')
                published.append(original(*args, **kwargs))
                self.assertEqual(s.reader.info.transaction_status, TransactionStatus.INTRANS)
                return published[-1]
            with patch.object(p1, '_record_result', side_effect=record):
                result = self.sql_execute(s)
            self.assertEqual(thaw(result), s.history.head()[0])
            self.assertEqual(published, [thaw(result)])
            self.assertEqual(s.effect_count, 1)
            self.assertEqual(len(s.history.events()), 5)
            event, obs = p1._load_entry(s.history, result)
            value = json.loads(s.history.read(event['result_ref']))
            self.assertEqual(value['state'], 'applied_verified')
            self.assertEqual(value['identity'], event['identity'])
            self.assertEqual(value['request_ref'], event['request_ref'])
            self.assertEqual(json.loads(s.history.read(obs['attempt_ref']))['consumption_sequence'], 1)
            self.assertGreaterEqual(len(self.transports), 4)
            self.assertEqual(s.reader.info.transaction_status, TransactionStatus.IDLE)
        self.assert_closed()

    def test_EB2_expiry_after_consumption_never_dispatches(self):
        from cortex_core.install import owner
        with self.sql_source() as s:
            s.start()
            s.expired = True
            with self.assertRaises(owner.OwnerRefusal) as caught:
                s.primitive.dispatch(s.effect_context())
            self.assertEqual(caught.exception.detail, 'reservation_expired')
            self.assertEqual(s.effect_count, 0)
            self.assertEqual(len(s.history.events()), 2)

    def test_EB2_nonce_drift_after_consumption_never_dispatches(self):
        from cortex_core.install import owner
        with self.sql_source() as s:
            s.start()
            effect = s.effect_context()
            s.helper.context(s.data, lambda c: c.update(nonce='stale-EB2-nonce'))
            with self.assertRaises(owner.OwnerRefusal) as caught:
                s.primitive.dispatch(effect)
            self.assertEqual(caught.exception.detail, 'reservation_invalidated')
            self.assertEqual(s.effect_count, 0)
            self.assertEqual(len(s.history.events()), 2)

    def test_EB2_checkpoint_must_name_original_consumption(self):
        from cortex_core.install import lifecycle as p1, owner
        from cortex_core.release.codec import canonical_bytes
        with self.sql_source() as s:
            original = p1._publish_entry
            def wrong_anchor(*args, **kwargs):
                attempt = kwargs['attempt']
                if attempt.get('sql_xact_ref') is not None:
                    fact = json.loads(s.history.read(attempt['sql_xact_ref']))
                    fact['consumed_ref'] = s.history.events()[0][0]
                    attempt['sql_xact_ref'] = s.history.retain(canonical_bytes(fact))
                return original(*args, **kwargs)
            with patch.object(p1, '_publish_entry', side_effect=wrong_anchor):
                with self.assertRaises(owner.OwnerRefusal) as caught:
                    s.dispatch()
            self.assertEqual(caught.exception.detail, 'journal_invalid')
            self.assertEqual(s.effect_count, 0)
            self.assertNotIn(('writer', 'COMMIT'), s.trace)

    def test_EB2_absent_checkpoint_with_unfenced_writer_remains_UNKNOWN(self):
        from cortex_core.install import lifecycle as p1, owner
        with self.sql_source() as s:
            effect = self.sql_interrupted(s, 1)
            s.recover(effect)
            s.private['sql_native']['writers_ended'] = False
            s.set_private()
            before = s.tree()
            with self.assertRaises(owner.OwnerRefusal) as caught:
                p1.execute_effect(s.data['request_raw'].encode())
            self.assertEqual(caught.exception.detail, 'attempt_ambiguous')
            self.assertEqual(s.tree(), before)
            self.assertEqual(s.effect_count, 0)

    def test_EB2_COMMIT_reply_loss_reuses_exact_result_without_redispatch(self):
        from cortex_core.install import lifecycle as p1
        from psycopg.pq import TransactionStatus
        with self.sql_source() as s:
            effect = self.sql_interrupted(s, 5)
            s.recover(effect)
            original = p1._record_result
            measurements = []
            def record(*args, **kwargs):
                self.assertEqual(s.reader.info.transaction_status, TransactionStatus.INTRANS,
                                 'EB2 recovery publication lost the SQL barrier')
                measurements.append(args[2])
                return original(*args, **kwargs)
            with patch.object(p1, '_record_result', side_effect=record):
                first = p1.execute_effect(s.data['request_raw'].encode())
                before = s.tree()
                s.status = None  # retired status lookup cannot supply a fresh truth
                second = p1.execute_effect(s.data['request_raw'].encode())
            self.assertEqual(first, second)
            self.assertEqual(len(measurements), 2)
            self.assertEqual(measurements[0], measurements[1])
            self.assertEqual(s.tree(), before)
            self.assertEqual(s.effect_count, 1)
            self.assertEqual(json.loads(measurements[0])['state'], 'applied_verified')

    def test_EB2_authoritative_NOT_APPLIED_allows_only_fresh_child(self):
        from cortex_core.install import lifecycle as p1, owner
        from cortex_core.release.contracts import thaw
        with self.sql_source() as s:
            effect = self.sql_interrupted(s, 2)
            s.recover(effect)
            parent = p1.execute_effect(s.data['request_raw'].encode())
            event, obs = p1._load_entry(s.history, parent)
            result = json.loads(s.history.read(event['result_ref']))
            attempt = json.loads(s.history.read(obs['attempt_ref']))
            self.assertEqual(result['state'], 'not_applied_verified')
            self.assertEqual(attempt['reconciliation_ref'], event['result_ref'])
            self.assertEqual(s.effect_count, 0)
            self.sql_child(s, effect, parent)
            child = self.sql_execute(s)
            event2, obs2 = p1._load_entry(s.history, child)
            attempt2 = json.loads(s.history.read(obs2['attempt_ref']))
            self.assertEqual(attempt2['origin_request_ref'], thaw(effect['context']['request_ref']))
            self.assertEqual(attempt2['predecessor_result_refs'], [event['result_ref']])
            self.assertEqual(attempt2['dispatch_state'], 'applied_verified')
            self.assertEqual(s.effect_count, 1)
            self.assertNotEqual(event2['identity']['attempt_id'], event['identity']['attempt_id'])
            with self.assertRaises(owner.OwnerRefusal):
                self.sql_execute(s)
            self.assertEqual(s.effect_count, 1)

    def sql_source(self):
        """Existing R01 join + delivered SQL producer; scheduled SOURCE DATA only.

        SQLSubject's constructor uses its old nonauthoritative proposal fixture
        for setup DATA. Every deciding proposal/prime/execute/recovery below is
        rebound through this class's real R01 composition, never its double.
        """
        import copy
        import tempfile
        from contextlib import contextmanager
        from cortex_core.install import lifecycle as p1, owner
        from cortex_core.release.codec import canonical_bytes
        from cortex_core.release.contracts import thaw
        from test_hold1_producer import SQLSubject
        @contextmanager
        def joined():
            self.init_boundary()
            with tempfile.TemporaryDirectory(prefix='EB2-source-') as root:
                s = SQLSubject(root)
                _, release_data = self.owner_data()
                release = json.loads(release_data['request_raw'])['release']
                request = json.loads(s.data['request_raw'])
                request['release'] = release
                s.data['aggregate_id'] = self.aggregate
                s.data['request_raw'] = canonical_bytes(request).decode()
                s.helper.edit_record(s.data, 'request_ref', lambda value: value.update(request))
                c = json.loads(s.data['context_raw'])
                consent = json.loads(s.data['originals'][c['consent_ref']['path']])
                scope = json.loads(s.data['originals'][consent['scope_ref']['path']])
                ref = scope['release_binding_ref']
                raw = canonical_bytes(release)
                s.data['originals'][ref['path']] = raw.decode()
                s.helper.scope(s.data, lambda value: value.update(release_binding_ref=p1._ref(ref['path'], raw)))
                with self.composition(), patch.object(owner, '_installed_owner_inputs', s.inputs), \
                        patch.object(p1, '_installed_lifecycle', lambda: p1._Bindings(
                            s.history, {'database_role': s.primitive})):
                    s.proposal = s.current()
                    s.deadline = s.proposal.deadline
                    mandate = json.loads(s.mandate)
                    context = thaw(s.proposal.context)
                    mandate.update({k: context[k] for k in ('identity', 'effect', 'request_ref')})
                    s.mandate = canonical_bytes(mandate)
                    s.eb2_original = copy.deepcopy(s.data)
                    yield s
        return joined()

    def sql_execute(self, s):
        from cortex_core.install import lifecycle as p1
        s.primitive._prime_sql()
        return p1.execute_effect(s.data['request_raw'].encode())

    def sql_interrupted(self, s, schedule):
        # Reuse Cox's existing explicit interruption fixture and actual dispatch;
        # no replacement producer, cached admission or alternate classifier.
        return s.dispatch(schedule=schedule)

    def sql_child(self, s, effect, parent):
        import copy
        from cortex_core.release.codec import canonical_bytes
        from cortex_core.release.contracts import thaw
        s.data = copy.deepcopy(s.eb2_original)
        s.recover(effect)  # Existing fresh owner/peer/clock/private fence DATA.
        q = json.loads(s.data['request_raw'])
        q.update(operation='install', journal_ref=thaw(parent))
        q['identity'].update(request_id='EB2-child-request', attempt_id='EB2-child-attempt')
        s.data['request_raw'] = canonical_bytes(q).decode()
        s.helper.edit_record(s.data, 'request_ref', lambda value: value.update(q))
        s.helper.context(s.data, lambda c: c.update(identity=q['identity']))
        s.helper.scope(s.data, lambda scope: scope.update(identity=q['identity'], operation='install'))
        s.private['sql_native']['writer_attempt_id'] = q['identity']['attempt_id']
        s.set_private()
        s.adapter.identity = q['identity']
