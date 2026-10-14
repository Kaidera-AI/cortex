"""U3-C unsigned assembly oracle; independently expected fixture bytes only."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aggregate_fixture import materials,context,write,encode,sha256,update,repin
from cortex_core.runtime.signed_member import Refusal

SRC=Path(__file__).resolve().parents[2]/'src/cortex_core/release'
MAIN=Path('/Users/amadmalik/DevVault/helix/output/kos/ren-kos-u3c-fixtures-20261014')


class Assembly(unittest.TestCase):
    def source(self):
        self.assertTrue((SRC/'assembly.py').is_file(),'SURFACE_ABSENCE_BODY: U3-C assembly source absent')
        return importlib.import_module('cortex_core.release.assembly'),importlib.import_module('cortex_core.release.aggregate')

    def setUp(self):
        MAIN.mkdir(mode=0o700,parents=True,exist_ok=True);self.tmp=tempfile.TemporaryDirectory(prefix='assembly-inert-',dir=MAIN.resolve())
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.value,self.bodies,self.builds,self.policy=materials();write(self.root,self.bodies)

    def run_assembly(self):
        a,_=self.source();from cortex_core.release.custody import RootReader
        r=RootReader.for_aggregate(str(self.root),8*1024**2);self.addCleanup(r.close)
        return a._assemble_original(encode(self.value),r,self.builds,context(self.policy))

    def refuse(self,reason,fn=None):
        with self.assertRaises(Refusal) as caught:(fn or self.run_assembly)()
        self.assertEqual(caught.exception.reason,reason)

    def pin(self):repin(self.value,self.policy)

    def put(self,path,raw):update(self.value,self.bodies,path,raw);(self.root/path).write_bytes(raw);self.pin()

    def image(self):return next(r for r in self.value['members'] if r['kind']=='image')

    def lock(self,change):
        d=json.loads(self.bodies['runtime.lock.json']);change(d);self.put('runtime.lock.json',encode(d))

    def test_T30_expected_unsigned_bytes_and_original_receipts_unchanged(self):
        expected=encode(self.value);receipts={g['receipt']:self.bodies[g['receipt']] for g in self.builds}
        self.assertEqual(self.run_assembly(),expected)
        self.assertEqual(receipts,{p:(self.root/p).read_bytes() for p in receipts})
        for g in self.builds:
            self.assertNotEqual(g['paths']['A/archive.bin'],g['paths']['B/archive.bin'])

    def test_production_default_authority_first(self):
        a,_=self.source()
        class NoRead:
            def __iter__(self):raise AssertionError('candidate read')
            def __fspath__(self):raise AssertionError('root read')
        with patch('builtins.open',side_effect=AssertionError('opened before authority')):
            self.refuse('required_binding_gap',lambda:a.assemble_aggregate(NoRead(),NoRead(),NoRead()))

    def test_T04_valid_hash_role_swap(self):
        image=self.image();image['module_id']='core' if image['module_id']=='api' else 'api';self.pin()
        self.refuse('aggregate_pair_mismatch')

    def test_T15_missing_target(self):
        image=self.image();self.value['members'].remove(image);(self.root/image['path']).unlink();self.pin()
        self.refuse('aggregate_pair_mismatch')

    def test_T15_module_version(self):
        self.image()['module_version']='0.1.019';self.pin();self.refuse('aggregate_pair_mismatch')

    def test_T15_original_source(self):
        self.value['source']['tree']='c'*40;self.policy['source']=copy.deepcopy(self.value['source']);self.pin()
        self.refuse('aggregate_pair_mismatch')

    def test_T16_raw_archive_is_not_platform_manifest(self):
        image=self.image();image['oci_manifest_sha256']=image['sha256'];self.pin();self.refuse('aggregate_pair_mismatch')

    def test_T16_config_is_not_platform_manifest(self):
        image=self.image();g=next(g for g in self.builds if g['image']==image['path'])
        image['oci_manifest_sha256']=json.loads(self.bodies[g['receipt']])['builds']['A']['oci']['config'];self.pin()
        self.refuse('aggregate_pair_mismatch')

    def test_T18_wrong_original_input_subject(self):
        self.image()['build_input_sha256']='d'*64;self.pin();self.refuse('aggregate_pair_mismatch')

    def test_T18_wrong_original_receipt_subject(self):
        self.image()['build_receipt_sha256']='d'*64;self.pin();self.refuse('aggregate_pair_mismatch')

    def test_T18_missing_original_mapping(self):
        self.builds[0]['paths'].pop('B/archive.bin');self.refuse('required_binding_gap')

    def test_T18_collapsed_AB_equal_hash_paths(self):
        g=self.builds[0];g['paths']['B/archive.bin']=g['paths']['A/archive.bin'];self.refuse('aggregate_pair_mismatch')

    def test_T18_wrong_mapping_original_digest(self):
        g=self.builds[0];g['paths']['B/config.bin']=g['paths']['B/index.bin'];self.refuse('aggregate_pair_mismatch')

    def test_T18_missing_independent_build_policy(self):
        self.builds[0]['policy']=None;self.refuse('required_binding_gap')

    def test_T18_actual_R01_verifier_not_declared_PASS(self):
        g=self.builds[0];d=json.loads(self.bodies[g['receipt']]);d['comparison']['raw_archives_equal']=False
        self.put(g['receipt'],encode(d));next(r for r in self.value['members'] if r['path']==g['image'])['build_receipt_sha256']=sha256(encode(d));self.pin()
        self.refuse('aggregate_pair_mismatch')

    def test_T18_altered_original_audit(self):
        g=self.builds[0];path=next(v for k,v in g['paths'].items() if 'audit-' in k);d=json.loads(self.bodies[path]);d['findings']=['fixture-fault'];self.put(path,encode(d))
        self.refuse('aggregate_pair_mismatch')

    def test_T19_missing_actual_bytes_despite_declared_hash(self):
        (self.root/self.builds[0]['receipt']).unlink();self.refuse('required_binding_gap')

    def test_T27_fixture_cannot_be_relabelled_production(self):
        self.value['purpose']='cortex/core-api/production';self.policy['purpose']=self.value['purpose'];self.policy['root']['allowed_purposes']=[self.value['purpose']];self.pin()
        self.refuse('required_binding_gap')

    def test_T27_native_qualified_flag_is_not_a_binding(self):
        self.builds[0]['native_qualified']=True;self.refuse('required_binding_gap')

    def test_T15_compatibility_tuple(self):
        p=self.value['compatibility']['compatibility_member'];d=json.loads(self.bodies[p]);d['modules'][0]['module_version']='0.1.019';self.put(p,encode(d))
        self.refuse('aggregate_pair_mismatch')

    def test_T15_schema_version(self):
        p=self.value['compatibility']['schema_member'];self.put(p,encode({'version':2,'migrations':[]}));self.refuse('aggregate_pair_mismatch')

    def test_T15_event_version(self):
        p=self.value['compatibility']['event_member'];self.put(p,encode({'version':2,'producers':['core','api']}));self.refuse('aggregate_pair_mismatch')

    def test_T15_migrations_original_inventory(self):
        p=self.value['compatibility']['migration_member'];self.put(p,encode({'version':1,'migrations':[{'id':'inert','file':'a.sql','sha256':'a'*64}]}));self.refuse('aggregate_pair_mismatch')

    def test_T15_descriptor_references(self):
        self.value['compatibility']['descriptor_members']=self.value['compatibility']['descriptor_members'][:-1];self.pin();self.refuse('aggregate_pair_mismatch')

    def test_T16_lock_both_platform_manifest_subjects(self):
        self.lock(lambda d:d['targets']['linux/arm64']['api']['image']['value'].update(sha256='f'*64));self.refuse('aggregate_pair_mismatch')

    def test_T15_lock_source(self):
        self.lock(lambda d:d['source']['value'].update(revision='f'*40));self.refuse('aggregate_pair_mismatch')

    def test_T15_lock_release_tuple(self):
        self.lock(lambda d:d['release']['value'].update(event_version=2));self.refuse('aggregate_pair_mismatch')

    def test_T19_lock_GAP_declaration(self):
        self.lock(lambda d:d['owner_ports']['private_inspection'].update(state='GAP'))
        self.refuse('required_binding_gap')

    def test_T17_lock_image_path_cycle(self):
        self.lock(lambda d:d['targets']['linux/amd64']['core']['image']['value'].update(path=d['layout']['runtime_lock']))
        self.refuse('member_path_alias')

    def embedded(self,path,body):
        from receipt_fixture import tar
        import native_matrix_fixture as f
        layer=tar({path:body});manifest=encode({'layers':[f.desc(f.LAYER,layer)]})
        index=encode({'manifests':[f.desc(f.MANIFEST,manifest)]})
        return tar({'index.json':index,'blobs/sha256/'+sha256(manifest):manifest,'blobs/sha256/'+sha256(layer):layer})

    def test_T17_embedded_lock_path(self):
        _,a=self.source();raw=self.embedded('run/cortex/runtime.lock.json',b'different inert bytes')
        self.refuse('member_path_alias',lambda:a._no_embedded_lock(raw,self.bodies['runtime.lock.json'],a.PROVISIONAL_LIMITS))

    def test_T17_renamed_embedded_exact_lock_body(self):
        _,a=self.source();raw=self.embedded('app/renamed.data',self.bodies['runtime.lock.json'])
        self.refuse('member_path_alias',lambda:a._no_embedded_lock(raw,self.bodies['runtime.lock.json'],a.PROVISIONAL_LIMITS))

    def test_T17_non_lock_layer_positive(self):
        _,a=self.source();raw=self.embedded('app/inert.data',b'inert unrelated bytes')
        self.assertIsNone(a._no_embedded_lock(raw,self.bodies['runtime.lock.json'],a.PROVISIONAL_LIMITS))

    def test_T18_contract_original_pin(self):
        p=next(r['path'] for r in self.value['members'] if r['role']=='operations');raw=self.bodies[p];self.put(p,raw+b' ');self.refuse('aggregate_pair_mismatch')

    def test_T18_lock_contract_subject(self):
        self.lock(lambda d:d['contract_members']['config_schema']['value'].update(sha256='f'*64));self.refuse('aggregate_pair_mismatch')

    def test_T22_manifest_bound(self):
        a,_=self.source();self.policy['limits']['manifest_bytes']=1
        self.refuse('raw_limit')

    def test_T22_count_bound(self):
        self.policy['limits']['member_count']=1;self.refuse('raw_limit')

    def test_T22_depth_bound(self):
        self.policy['limits']['depth']=1;self.refuse('raw_limit')

    def test_member_order_and_duplicate_path(self):
        _,a=self.source()
        v=copy.deepcopy(self.value);v['members'].reverse()
        self.refuse('aggregate_shape',lambda:a._validate_manifest(encode(v),self.policy))
        v=copy.deepcopy(self.value);v['members'][-1]['path']=v['members'][-2]['path']
        self.refuse('aggregate_shape',lambda:a._validate_manifest(encode(v),self.policy))

    def test_record_declared_JSON_must_be_original_JSON(self):
        p=self.value['compatibility']['compatibility_member'];self.put(p,b'not-json\n');self.refuse('aggregate_pair_mismatch')

    def test_T18_only_one_pair_per_image(self):
        self.builds[-1]=copy.deepcopy(self.builds[0]);self.refuse('aggregate_pair_mismatch')

    def test_T18_mapped_input_role(self):
        g=self.builds[0];next(r for r in self.value['members'] if r['path']==g['input'])['role']='evidence';self.pin()
        self.refuse('aggregate_pair_mismatch')

    def test_T18_unreferenced_record_cannot_supply_closure(self):
        g=self.builds[0];g['paths']['unused-fixture']='members/absent';self.refuse('aggregate_pair_mismatch')

    def test_T22_owner_cannot_raise_caps(self):
        _,a=self.source();self.policy['limits']['image_bytes']=2**40
        limits=a._limits(self.policy);self.assertEqual(limits['image_bytes'],768*1024**2)

    def test_expected_inventory_independent_owner_exact(self):
        self.policy['expected_members']=self.policy['expected_members'][:-1];self.refuse('aggregate_pair_mismatch')

    def test_expected_raw_unsigned_identity(self):
        self.policy['expected_aggregate_sha256']='f'*64;self.refuse('aggregate_identity_mismatch')

    def test_original_manifest_not_reencoded(self):
        a,_=self.source();from cortex_core.release.custody import RootReader
        r=RootReader.for_aggregate(str(self.root),8*1024**2);self.addCleanup(r.close)
        self.refuse('aggregate_shape',lambda:a._assemble_original(json.dumps(self.value,indent=2).encode(),r,self.builds,context(self.policy)))

    def test_T30_post_validation_changed_receipt_no_output(self):
        a,_=self.source();from cortex_core.release.custody import RootReader
        r=RootReader.for_aggregate(str(self.root),8*1024**2);self.addCleanup(r.close);real=r.aggregate_finish
        def finish():
            p=self.builds[0]['receipt'];(self.root/p).write_bytes(self.bodies[p]);return real()
        with patch.object(r,'aggregate_finish',finish):self.refuse('member_custody_changed',lambda:a._assemble_original(encode(self.value),r,self.builds,context(self.policy)))

    def test_internal_error_sanitized_and_all_files_closed(self):
        a,_=self.source();from cortex_core.release.custody import RootReader
        r=RootReader.for_aggregate(str(self.root),8*1024**2);self.addCleanup(r.close)
        with patch.object(r,'aggregate_bytes',side_effect=RuntimeError('/private/token-fixture')):
            self.refuse('member_authority_unavailable',lambda:a._assemble_original(encode(self.value),r,self.builds,context(self.policy)))
        self.assertIsNone(r.fd);self.assertFalse(r._aggregate_files)


class AssemblyControls(unittest.TestCase):
    """Focused causal companions; original 76 deciding assertions unchanged."""
    source=Assembly.source;setUp=Assembly.setUp;run_assembly=Assembly.run_assembly
    refuse=Assembly.refuse;pin=Assembly.pin;put=Assembly.put;lock=Assembly.lock;image=Assembly.image

    def test_limits_required_typed_shape(self):
        _,a=self.source()
        for limits in (None,{},dict(self.policy['limits'],depth=True),dict(self.policy['limits'],depth=0)):
            with self.subTest(limits=limits):
                try:a._limits(dict(self.policy,limits=limits))
                except Exception as e:self.assertIsInstance(e,Refusal);self.assertEqual(e.reason,'required_binding_gap')
                else:self.fail('invalid independent limits accepted')

    def test_manifest_container_typed_refusal(self):
        _,a=self.source()
        for value in ([],None,{'members':None},{'members':[None]},{'members':[{'path':1}]}):
            with self.subTest(value=value):
                try:a._validate_manifest(encode(value),self.policy)
                except Exception as e:self.assertIsInstance(e,Refusal);self.assertEqual(e.reason,'aggregate_shape')
                else:self.fail('invalid original shape accepted')

    def test_manifest_schema_unknown_field(self):
        _,a=self.source();v=copy.deepcopy(self.value);v['native_qualified']=True
        self.refuse('aggregate_shape',lambda:a._validate_manifest(encode(v),self.policy))

    def test_manifest_raw_type_is_bounded(self):
        _,a=self.source();self.refuse('raw_limit',lambda:a._validate_manifest(None,self.policy))

    def test_parsed_manifest_count_is_bounded_before_transport(self):
        _,a=self.source();self.policy['limits']['member_count']=1
        self.refuse('raw_limit',lambda:a._validate_manifest(encode(self.value),self.policy))

    def test_build_group_count_required(self):
        self.builds.pop();self.refuse('required_binding_gap')

    def test_missing_input_binding(self):
        self.builds[0]['input']='members/absent';self.refuse('required_binding_gap')

    def test_each_original_descriptor_keeps_typed_role(self):
        p=self.value['compatibility']['descriptor_members'][0]
        next(r for r in self.value['members'] if r['path']==p)['role']='evidence';self.pin();self.refuse('aggregate_pair_mismatch')

    def test_each_original_SBOM_keeps_typed_role(self):
        p=self.builds[0]['paths']['B/sbom.json']
        next(r for r in self.value['members'] if r['path']==p)['role']='evidence';self.pin();self.refuse('aggregate_pair_mismatch')

    def test_invalid_lock_layout_path_is_closed_pair_reason(self):
        self.lock(lambda d:d['layout'].update(runtime_lock='relative/lock'));self.refuse('aggregate_pair_mismatch')

    def test_missing_mapped_original_binding(self):
        self.builds[0]['paths']['B/config.bin']='members/absent';self.refuse('required_binding_gap')

    def test_selected_A_archive_mapping_cannot_be_swapped_with_B(self):
        g=self.builds[0];g['paths']['A/archive.bin'],g['paths']['B/archive.bin']=g['paths']['B/archive.bin'],g['paths']['A/archive.bin']
        self.refuse('aggregate_pair_mismatch')

    def test_module_role_sole_fault_over_actual_valid_other_pair(self):
        # Exchange complete valid same-platform Core/API subjects and mappings;
        # hashes, original targets, receipt bytes, inputs and OCI remain valid.
        x,y=self.builds[0],self.builds[2];rx=next(r for r in self.value['members'] if r['path']==x['image']);ry=next(r for r in self.value['members'] if r['path']==y['image'])
        keep={'path','role','kind','target'}
        a,b=copy.deepcopy(rx),copy.deepcopy(ry)
        for key in set(rx)-keep:rx[key],ry[key]=b[key],a[key]
        self.bodies[x['image']],self.bodies[y['image']]=self.bodies[y['image']],self.bodies[x['image']]
        oldx,oldy=x['image'],y['image'];x['image'],y['image']=oldy,oldx
        x['paths']['A/archive.bin']=oldy;y['paths']['A/archive.bin']=oldx
        self.pin();write(self.root,self.bodies);self.refuse('aggregate_pair_mismatch')

    def test_release_module_version_sole_fault(self):
        self.value['release']['core_module_version']='0.1.019';self.policy['release']=copy.deepcopy(self.value['release'])
        self.lock(lambda d:d['release']['value'].update(core_module_version='0.1.019'));self.pin();self.refuse('aggregate_pair_mismatch')

    def test_image_input_target_sole_fault(self):
        self.value['release']['core_module_version']='0.1.019';self.policy['release']=copy.deepcopy(self.value['release'])
        for row in self.value['members']:
            if row['kind']=='image' and row['module_id']=='core':row['module_version']='0.1.019'
        def changed(lock):
            lock['release']['value']['core_module_version']='0.1.019'
            for target in lock['targets'].values():target['core']['image']['value']['version']='0.1.019'
        self.lock(changed);self.pin();self.refuse('aggregate_pair_mismatch')

    def test_unreferenced_mapping_sole_fault(self):
        raw=b'inert extra original\n';p='members/extra-original.bin'
        self.value['members'].append(dict(kind='record',role='evidence',path=p,media_type='application/octet-stream',bytes=len(raw),sha256=sha256(raw)))
        self.value['members'].sort(key=lambda r:r['path']);self.bodies[p]=raw;write(self.root,self.bodies)
        self.builds[0]['paths']['unused-fixture']=p;self.pin();self.refuse('aggregate_pair_mismatch')

    def test_compatibility_reference_tuple_sole_fault(self):
        self.value['compatibility']['api_contract']='other-fixture-api';self.pin();self.refuse('aggregate_pair_mismatch')

    def test_compatibility_reference_role_sole_fault(self):
        g=self.builds[0];iv=json.loads(self.bodies[g['input']]);self.value['compatibility']['schema_member']=g['paths'][iv['compatibility_inputs']['schema_manifest']['path']]
        self.pin();self.refuse('aggregate_pair_mismatch')

    def test_original_compatibility_api_contract_sole_fault(self):
        import aggregate_fixture as fixture
        with patch.dict(fixture.COMPAT,api_contract='other-fixture-api'):
            self.value,self.bodies,self.builds,self.policy=fixture.materials()
        write(self.root,self.bodies);self.refuse('aggregate_pair_mismatch')

    def test_release_schema_version_sole_fault(self):
        self.value['release']['schema_version']=2;self.value['compatibility']['schema_version']=2;self.policy['release']=copy.deepcopy(self.value['release'])
        self.lock(lambda d:d['release']['value'].update(schema_version=2));self.pin();self.refuse('aggregate_pair_mismatch')

    def test_release_event_version_sole_fault(self):
        self.value['release']['event_version']=2;self.value['compatibility']['event_version']=2;self.policy['release']=copy.deepcopy(self.value['release'])
        self.lock(lambda d:d['release']['value'].update(event_version=2));self.pin();self.refuse('aggregate_pair_mismatch')

    def test_declared_JSON_binary_subject_sole_fault(self):
        p=self.builds[0]['paths']['B/archive.bin'];next(r for r in self.value['members'] if r['path']==p)['media_type']='application/json';self.pin();self.refuse('aggregate_pair_mismatch')

    def test_duplicate_RC0_contract_role(self):
        original=next(r for r in self.value['members'] if r['role']=='operations');r=copy.deepcopy(original);r['path']='members/contracts/operations-copy.json'
        self.value['members'].append(r);self.value['members'].sort(key=lambda r:r['path']);self.bodies[r['path']]=self.bodies[original['path']];write(self.root,self.bodies);self.pin();self.refuse('aggregate_pair_mismatch')

    def test_pinned_contract_raw_sole_fault_with_coherent_lock_binding(self):
        p=next(r['path'] for r in self.value['members'] if r['role']=='operations');raw=self.bodies[p]+b' '
        self.put(p,raw);self.lock(lambda d:d['contract_members']['operations']['value'].update(sha256=sha256(raw),bytes=len(raw)))
        self.refuse('aggregate_pair_mismatch')

    def test_RC0_closed_schema_guard(self):
        self.lock(lambda d:d['budgets'].update(api_pool_max=0));self.refuse('aggregate_pair_mismatch')

    def test_owner_placement_guard(self):
        from dataclasses import replace
        a,_=self.source();from cortex_core.release.custody import RootReader
        r=RootReader.for_aggregate(str(self.root),8*1024**2);self.addCleanup(r.close)
        self.refuse('aggregate_scope_mismatch',lambda:a._assemble_original(encode(self.value),r,self.builds,replace(context(self.policy),placement='consumer')))

    def test_current_owner_recheck_composition(self):
        from dataclasses import replace
        a,_=self.source();from cortex_core.release.custody import RootReader
        r=RootReader.for_aggregate(str(self.root),8*1024**2);self.addCleanup(r.close)
        self.refuse('aggregate_replay',lambda:a._assemble_original(encode(self.value),r,self.builds,replace(context(self.policy),now=9999)))

    def test_missing_owner_before_member_reads(self):
        a,_=self.source();from cortex_core.release.custody import RootReader
        r=RootReader.for_aggregate(str(self.root),8*1024**2);self.addCleanup(r.close)
        with patch.object(r,'aggregate_begin',side_effect=AssertionError('read before independent context')):
            self.refuse('required_binding_gap',lambda:a._assemble_original(encode(self.value),r,self.builds,None))

    def test_finish_guard_change_occurs_before_finish_call(self):
        a,_=self.source();real=a._check_pairs
        def after(*args):
            real(*args);p=self.builds[0]['receipt'];(self.root/p).write_bytes(self.bodies[p])
        with patch.object(a,'_check_pairs',after):self.refuse('member_custody_changed')

    def test_actual_original_R01_and_embedded_detector_calls(self):
        _,a=self.source();inputs=[];receipts=[];embedded=[]
        input_body=a.admit_build_input;receipt_body=a.verify_build_receipt;embedded_body=a._no_embedded_lock
        def admit(parsed,*args):
            result=input_body(parsed,*args);inputs.append((parsed.raw,result.native_qualified));return result
        def verify(parsed,*args):
            result=receipt_body(parsed,*args);receipts.append((parsed.raw,result.native_qualified));return result
        def detector(image,lock,*args,**kwargs):
            embedded.append((sha256(image),sha256(lock)));return embedded_body(image,lock,*args,**kwargs)
        with patch.object(a,'admit_build_input',admit),patch.object(a,'verify_build_receipt',verify),patch.object(a,'_no_embedded_lock',detector):
            try:result=self.run_assembly()
            except Exception as e:self.fail('valid original graph refused: '+type(e).__name__)
        self.assertEqual(result,encode(self.value))
        self.assertEqual(inputs,[(self.bodies[g['input']],False) for g in self.builds])
        self.assertEqual(receipts,[(self.bodies[g['receipt']],False) for g in self.builds])
        self.assertEqual(embedded,[(r['sha256'],sha256(self.bodies['runtime.lock.json'])) for r in self.value['members'] if r['kind']=='image'])

    def test_wrong_pair_set_refuses_before_actual_R01_qualification(self):
        _,a=self.source();real=a.verify_build_receipt;called=[]
        def verify(*args):called.append(True);return real(*args)
        self.builds[-1]=copy.deepcopy(self.builds[0])
        with patch.object(a,'verify_build_receipt',verify):self.refuse('aggregate_pair_mismatch')
        self.assertEqual(called,[])

    def test_extra_physical_directory_is_closed_pair_refusal(self):
        (self.root/'members/undeclared').mkdir();self.refuse('aggregate_pair_mismatch')
