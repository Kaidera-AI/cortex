"""P1-01..08 actual common path; the sole member double lives in Nemo tests."""
import ast
import copy
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from test_contracts import NEXT,SUBJECTS,encode
from cortex_core.release.codec import sha256
from cortex_core.runtime import signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble


class PlannerOracle(unittest.TestCase):
    def api(self):
        self.assertTrue((NEXT/'src/cortex_core/install/planner.py').is_file(),'P1 planner surface absent')
        return importlib.import_module('cortex_core.install.planner')

    def q(self,target='linux/arm64',route='manual'):
        return copy.deepcopy(SUBJECTS['requests'][target+'/'+route])

    def select(self,q=None,*,target='linux/arm64',members=None,config=None,environ=None,owner_uid=501,source_envelopes=None,after=None):
        api=self.api();q=self.q(target) if q is None else q
        data={k:v.encode() for k,v in SUBJECTS['members'].items()} if members is None else members
        calls=[];reads=[];config_calls=[]
        def authority(aggregate,name):
            calls.append((aggregate,name))
            double=NonAuthoritativeSignedMemberDouble(data[name],member_name=name)
            answer=double(aggregate,name)
            if after:after(data,calls)
            return answer
        def config_reader(path,limit):
            config_calls.append((path,limit))
            return SUBJECTS['configs'][target].encode() if config is None else config
        with patch.object(signed_member,'_original_authority',authority),patch.object(signed_member,'read_member',wraps=signed_member.read_member) as read:
            result=api.plan(encode(q),aggregate_id='test-only-aggregate',observed_target=target,
                owner_uid=owner_uid,host_identity_ref=q['caller']['host_identity_ref'],environ={} if environ is None else environ,
                config_reader=config_reader,source_envelopes=source_envelopes)
            reads=list(read.call_args_list)
        return result,calls,reads,config_calls

    def test_paired_manual_and_kos_use_same_shared_path_and_bytes(self):
        for target in ['linux/arm64','linux/amd64']:
            for route in ['manual','kos_prerequisite']:
                with self.subTest(target=target,route=route):
                    q=self.q(target,route);result,calls,reads,config_calls=self.select(q,target=target)
                    self.assertEqual(result.result,'PASS')
                    self.assertEqual(result.reason_code,'completed')
                    self.assertFalse(result.effect_authority);self.assertFalse(result.native_qualified)
                    self.assertEqual(result.authority_status,'UNBOUND')
                    self.assertEqual(result.receipt['invocation_result']['outcome'],'not_executed')
                    self.assertIsNone(result.receipt['journal_ref'])
                    self.assertEqual(result.receipt['caller']['route'],route)
                    self.assertIn(('test-only-aggregate','runtime.lock.json'),calls)
                    self.assertGreater(len(reads),0)
                    self.assertEqual(config_calls,[('/run/cortex/inspection.json',65536)])
                    self.assertEqual(result.runtime_plan.mode,'inspect')

    def test_production_unbound_precedes_request_schema_environment_and_readers(self):
        api=self.api()
        def fail(*a,**kw):self.fail('unbound reached an input/effect')
        with patch.object(Path,'read_bytes',fail),patch('builtins.open',fail),patch('socket.socket',fail),patch('subprocess.Popen',fail):
            result=api.plan(b'not even JSON',aggregate_id='unbound',observed_target='linux/arm64',owner_uid=501,
                            host_identity_ref={},environ={'PGPASSWORD':'SENTINEL'},config_reader=fail)
        self.assertEqual((result.result,result.reason_code,result.unit),('BLOCKED','dependency_not_run',signed_member.MISSING_UNIT))
        self.assertIsNone(result.receipt)

    def test_real_UNBOUND_envelopes_and_GAP_refuse_before_port_or_config_reader(self):
        api=self.api()
        envelopes={name:(NEXT/f'release/recipes/{name}/source-envelope.json').read_bytes() for name in ['core','api']}
        def fail(*a,**kw):self.fail('unbound/GAP reached a reader')
        with patch.object(signed_member,'request_member',fail):
            result=api.plan(encode(self.q()),aggregate_id='anything',observed_target='linux/arm64',owner_uid=501,
                 host_identity_ref={},environ={},config_reader=fail,source_envelopes=envelopes)
        self.assertEqual(result.reason_code,'dependency_not_run')
        gap={'core':encode({'state':'GAP','gap_id':'owner-input'}),'api':encode({'state':'GAP'})}
        with patch.object(signed_member,'request_member',fail):
            result=api.plan(encode(self.q()),aggregate_id='anything',observed_target='linux/arm64',owner_uid=501,
                 host_identity_ref={},environ={},config_reader=fail,source_envelopes=gap)
        self.assertEqual(result.reason_code,'dependency_not_run')

    def test_mutating_operations_are_never_given_a_reader_or_capability(self):
        for op in ['install','recover','rollback','uninstall','purge','status']:
            q=self.q();q['operation']=op
            if op=='purge':q['purge_selection']={'product':'cortex','exact_data_roots':['/fixture/cortex/data'],'confirmation_refs':[]}
            result,_,_,readers=self.select(q)
            self.assertEqual((result.result,result.reason_code),('BLOCKED','dependency_not_run'))
            self.assertEqual(readers,[])
            self.assertFalse(result.effect_authority)

    def test_inherited_environment_and_wrong_operator_prevent_config_read(self):
        for kwargs in [{'environ':{'PGPASSWORD':'SECRET_MUST_NOT_BE_LOGGED'}},{'owner_uid':502}]:
            result,_,_,readers=self.select(**kwargs)
            self.assertEqual(result.reason_code,'unsafe_custody');self.assertEqual(readers,[])
            self.assertNotIn('SECRET_MUST_NOT_BE_LOGGED',str(result))

    def test_wrong_target_uuid_operation_and_scope_refuse(self):
        q=self.q();q['target']['backend_platform']='linux/amd64'
        result,_,_,_=self.select(q);self.assertEqual(result.reason_code,'unsupported_target')
        q=self.q();q['identity']['operation_id']='00000000-0000-4000-8000-000000000099'
        result,_,_,_=self.select(q);self.assertEqual(result.reason_code,'state_drift')
        q=self.q();q['release']['qualification_scope']='native'
        result,_,_,readers=self.select(q);self.assertEqual(result.reason_code,'dependency_not_run');self.assertEqual(readers,[])

    def test_original_refs_and_entrypoint_are_cross_bound(self):
        for which in ['digest','entrypoint','compatibility']:
            q=self.q()
            if which=='digest':q['release']['product_manifest_ref']['sha256']='0'*64
            elif which=='entrypoint':q['invocation']['entrypoint_ref']=q['release']['product_manifest_ref']
            else:q['target']['compatibility_ref']=q['release']['product_manifest_ref']
            result,_,_,_=self.select(q)
            self.assertEqual(result.reason_code,'wrong_release')

    def test_root_path_uid_mode_link_and_shared_root_drift(self):
        for field,value in [('canonical_path','/foreign'),('uid',502),('mode',511),('link_target','/foreign')]:
            data={k:v.encode() for k,v in SUBJECTS['members'].items()}
            witness=json.loads(data['application.custody.json']);witness[field]=value;data['application.custody.json']=encode(witness)
            q=self.q();ref={'path':'application.custody.json','bytes':len(data['application.custody.json']),'sha256':sha256(data['application.custody.json'])};q['roots'][0]['custody_ref']=ref
            q['acquisition']['local_members']=[ref if x['path']==ref['path'] else x for x in q['acquisition']['local_members']]
            result,_,_,_=self.select(q,members=data);self.assertEqual(result.reason_code,'unsafe_custody')

    def test_original_member_reobserved_after_full_plan(self):
        def change(data,calls):
            if any(name=='application.custody.json' for _,name in calls):data['product.json']=b'changed after original admission'
        result,_,_,_=self.select(after=change)
        self.assertEqual(result.reason_code,'wrong_release')

    def test_config_gap_and_opaque_dependency_failure_are_sanitized(self):
        cfg=json.loads(SUBJECTS['configs']['linux/arm64']);cfg['resolver']['implementation']={'state':'GAP','gap_id':'fixture-gap','owner_role':'core-contract-owner','reason':'original_owner_binding_not_delivered','blocks':['execution']}
        result,_,_,_=self.select(config=encode(cfg));self.assertNotEqual(result.result,'PASS')
        api=self.api()
        double=NonAuthoritativeSignedMemberDouble(SUBJECTS['members']['runtime.lock.json'].encode())
        def fail(*a):raise ValueError('SECRET_MUST_NOT_BE_LOGGED')
        with double.installed(signed_member):
            result=api.plan(encode(self.q()),aggregate_id='test-only-aggregate',observed_target='linux/arm64',owner_uid=501,
                  host_identity_ref=self.q()['caller']['host_identity_ref'],environ={},config_reader=fail)
        self.assertEqual(result.reason_code,'dependency_not_run');self.assertNotIn('SECRET_MUST_NOT_BE_LOGGED',str(result))

    def test_empty_or_external_profile_is_never_install_permission(self):
        q=self.q();q['selection']['install_missing']=True
        result,_,_,_=self.select(q)
        self.assertEqual(result.result,'PASS');self.assertEqual(result.receipt['resources'],[])
        self.assertEqual(result.receipt['disposition'],'absent');self.assertEqual(result.receipt['readiness']['state'],'not_installed')

    def test_default_module_and_closed_carrier(self):
        self.api();self.assertTrue((NEXT/'src/cortex_core/install/__main__.py').is_file(),'P1 client surface absent')
        entry=importlib.import_module('cortex_core.install.__main__')
        def fail(*a,**kw):self.fail('default client read input before unbound')
        with patch.object(Path,'read_bytes',fail),patch('builtins.open',fail),patch.object(sys,'argv',object()):
            result=entry.main()
        self.assertEqual(result.reason_code,'dependency_not_run');self.assertEqual(result.unit,signed_member.MISSING_UNIT)
        for argv in [['--shell','bad'],['--request-file','relative','--json'],['--request-file','/fixture/request','--json','--extra']]:
            result=entry.plan_entrypoint(argv,aggregate_id='unbound',observed_target='linux/arm64',owner_uid=501,
                  host_identity_ref={},environ={},request_reader=fail,config_reader=fail)
            self.assertEqual(result.reason_code,'dependency_not_run')

    def test_no_test_double_or_second_port_imports_in_product(self):
        self.api()
        for path in (NEXT/'src/cortex_core/install').glob('*.py'):
            tree=ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Constant) and isinstance(node.value,str):
                    self.assertNotIn('non_authoritative_signed_member_double',node.value)
                if isinstance(node,(ast.Import,ast.ImportFrom)):
                    self.assertNotIn('non_authoritative_signed_member_double',ast.unparse(node))
        registry=json.loads((NEXT/'contracts/install/resource-witnesses.v1.json').read_bytes())
        self.assertEqual(registry['entries'],[]);self.assertIsNone(registry['target_policy_ref'])


    def test_root_original_reobserved_after_root_witness_measurement(self):
        def change(data,calls):
            if calls[-1][1]=='application.custody.json':
                data['application.custody.json']=b'root changed after measurement'
        result,_,_,_=self.select(after=change)
        self.assertEqual(result.reason_code,'wrong_release')

    def test_admitted_client_carrier_rejects_bad_argv_before_input_reader(self):
        self.api();entry=importlib.import_module('cortex_core.install.__main__')
        double=NonAuthoritativeSignedMemberDouble(SUBJECTS['members']['runtime.lock.json'].encode())
        def fail(*a,**kw):self.fail('bad carrier reached request reader')
        for argv in [['--other','/fixture/request','--json'],['--request-file','relative','--json'],['--request-file','/fixture/request','--json','extra']]:
            with double.installed(signed_member):
                result=entry.plan_entrypoint(argv,aggregate_id='test-only-aggregate',observed_target='linux/arm64',owner_uid=501,
                   host_identity_ref=self.q()['caller']['host_identity_ref'],environ={},request_reader=fail,config_reader=fail)
            self.assertEqual(result.reason_code,'bad_shape')


if __name__=='__main__':unittest.main()
