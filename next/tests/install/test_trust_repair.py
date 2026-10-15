"""Vera P1-ROOT-001 / P1-RECEIPT-001 deciding original-byte controls.

Frozen before source repairs. Existing P1 fixtures/double are nonauthoritative;
no declaration or passing source receipt qualifies a physical root or effect.
"""
import copy
import json
import unittest

import test_contracts as contracts_oracle
import test_planner as planner_oracle
from cortex_core.install import contracts, receipt
from cortex_core.release.codec import sha256

OBSERVATIONS = {}


def ref(path, raw):
    return {'path':path, 'bytes':len(raw), 'sha256':sha256(raw)}


def root_subject(target='linux/arm64', route='manual', *, alias=False, backend=None, zeros=False):
    helper = planner_oracle.PlannerOracle()
    request = helper.q(target, route)
    members = {name:raw.encode() for name,raw in contracts_oracle.SUBJECTS['members'].items()}
    if alias or zeros:
        witness = json.loads(members['evidence.custody.json'])
        # Literal sole deciding identity from Vera: same backend, device1/inode10.
        witness['filesystem_identity'] = {'device':0,'inode':0} if zeros else {'device':1,'inode':10}
        witness['backend_id'] = backend
        if zeros:
            app = json.loads(members['application.custody.json'])
            app['filesystem_identity'] = {'device':0,'inode':0}
            members['application.custody.json'] = contracts_oracle.encode(app)
        members['evidence.custody.json'] = contracts_oracle.encode(witness)
        for root in request['roots']:
            name = root['custody_ref']['path']
            original_ref = ref(name, members[name])
            root['custody_ref'] = original_ref
            request['acquisition']['local_members'] = [original_ref if r['path']==name else r
                                                       for r in request['acquisition']['local_members']]
    return helper, request, members


def prior_subject(operation):
    request = copy.deepcopy(contracts_oracle.SUBJECTS['requests']['linux/arm64/manual'])
    request['operation'] = operation
    request['identity']['installation_id'] = 'prior-installation'
    prior = copy.deepcopy(contracts_oracle.SUBJECTS['receipt']['request_ref'])
    request['previous_receipt_ref'] = prior
    result = copy.deepcopy(contracts_oracle.SUBJECTS['receipt'])
    result['identity'] = copy.deepcopy(request['identity'])
    result['request_ref'] = ref('request.json', contracts_oracle.encode(request))
    result['disposition'] = 'verified_owned_continuation'
    result['phase'] = 'planned' if operation=='plan' else 'verified'
    result['evidence_refs'] = [prior]
    witness = json.loads(contracts_oracle.SUBJECTS['members']['application.custody.json'])
    result['resources'] = [{
        'resource_id':'prior-application','product':'cortex','installation_id':'prior-installation',
        'kind':'directory','role':'application','ownership':'verified_owned_continuation',
        'created_by':prior,'intent_event_ref':prior,'result_event_ref':prior,
        'before_witness':copy.deepcopy(witness),'after_witness':copy.deepcopy(witness),
        'source_installer_ref':copy.deepcopy(request['release']['installer_ref']),
        'source_receipt_ref':prior,'consumers_ref':None,'data_class':'runtime',
        'removal_policy':'remove_if_unchanged_unshared'}]
    return request, result


class TrustRepairOracle(unittest.TestCase):
    def test_declared_root_alias_refuses_on_all_routes_and_targets(self):
        for target in ('linux/arm64','linux/amd64'):
            for route in ('manual','kos_prerequisite'):
                with self.subTest(target=target,route=route):
                    helper, q, data = root_subject(target, route)
                    positive, _, _, _ = helper.select(q, target=target, members=data)
                    self.assertEqual((positive.result, positive.reason_code), ('PASS','completed'))
                    self.assertFalse(positive.effect_authority); self.assertFalse(positive.native_qualified)
                    helper, q, data = root_subject(target, route, alias=True)
                    witnesses = [json.loads(data[root['custody_ref']['path']]) for root in q['roots']]
                    self.assertEqual([w['backend_id'] for w in witnesses], [None,None])
                    self.assertEqual([w['filesystem_identity'] for w in witnesses], [{'device':1,'inode':10}]*2)
                    self.assertNotEqual(q['roots'][0]['absolute_path'],q['roots'][1]['absolute_path'])
                    for root in q['roots']:
                        self.assertEqual(root['custody_ref'],ref(root['custody_ref']['path'],data[root['custody_ref']['path']]))
                        self.assertIn(root['custody_ref'],q['acquisition']['local_members'])
                    answer, calls, _, config = helper.select(q, target=target, members=data)
                    self.assertFalse(answer.effect_authority); self.assertFalse(answer.native_qualified)
                    self.assertIn(('test-only-aggregate','application.custody.json'),calls)
                    self.assertIn(('test-only-aggregate','evidence.custody.json'),calls)
                    OBSERVATIONS['root/'+target+'/'+route] = {'positive':'PASS/completed',
                        'expected':'FAIL/unsafe_custody','actual':answer.result+'/'+answer.reason_code,
                        'original_request_sha256':sha256(contracts_oracle.encode(q)),
                        'root_refs':q['roots'],'witnesses':witnesses,'config_reads':len(config)}
                    self.assertEqual((answer.result, answer.reason_code), ('FAIL','unsafe_custody'))

    def test_equal_identity_in_other_backend_and_zero_placeholders_remain_readonly(self):
        for kwargs in ({'alias':True,'backend':'different-test-backend'}, {'zeros':True}):
            with self.subTest(case=kwargs):
                helper, q, data = root_subject(**kwargs)
                answer, _, _, _ = helper.select(q, members=data)
                self.assertEqual((answer.result,answer.reason_code),('PASS','completed'))
                self.assertFalse(answer.effect_authority); self.assertFalse(answer.native_qualified)
                self.assertEqual(answer.authority_status,'UNBOUND')

    def test_readonly_prior_resource_relabel_refuses_plan_and_verify(self):
        for operation in ('plan','verify'):
            with self.subTest(operation=operation):
                q, r = prior_subject(operation)
                positive = receipt.verify_product_receipt(contracts_oracle.encode(r),contracts_oracle.encode(q),request_ref=r['request_ref'])
                self.assertFalse(positive.effect_authority); self.assertFalse(positive.native_qualified)
                negative = copy.deepcopy(r)
                negative['resources'][0]['ownership'] = 'created_by_this_transaction'
                self.assertEqual({k:v for k,v in negative['resources'][0].items() if k!='ownership'},
                                 {k:v for k,v in r['resources'][0].items() if k!='ownership'})
                # Sole-fault relabel still meets the original closed receipt grammar.
                shape = contracts.parse_install_message(contracts_oracle.encode(negative),expected_kind='receipt')
                self.assertFalse(shape.effect_authority)
                called = []
                def live_reader(*args,**kwargs):
                    called.append(True);self.fail('read-only verifier reached a live reader')
                with self.assertRaises(contracts.InstallRefusal) as caught:
                    receipt.verify_product_receipt(contracts_oracle.encode(negative),contracts_oracle.encode(q),
                        request_ref=r['request_ref'],live_readers={'resource':live_reader})
                self.assertEqual(caught.exception.reason_code,'bad_shape')
                self.assertEqual(called,[])
                OBSERVATIONS['resource/'+operation] = {'positive':'verified_owned_continuation',
                    'changed_only':'resources[0].ownership','expected':'InstallRefusal/bad_shape',
                    'actual':caught.exception.reason_code,'original_request_sha256':sha256(contracts_oracle.encode(q)),
                    'positive_receipt_sha256':sha256(contracts_oracle.encode(r)),
                    'negative_receipt_sha256':sha256(contracts_oracle.encode(negative)),'live_reader_calls':len(called)}

    def test_readonly_external_resources_remain_observations(self):
        for operation in ('plan','verify'):
            with self.subTest(operation=operation):
                q, r = prior_subject(operation)
                resource = r['resources'][0]
                resource['ownership'] = 'external_read_only'
                for name in ('created_by','intent_event_ref','result_event_ref'): resource[name] = None
                answer = receipt.verify_product_receipt(contracts_oracle.encode(r),contracts_oracle.encode(q),request_ref=r['request_ref'])
                self.assertFalse(answer.effect_authority); self.assertFalse(answer.native_qualified)


if __name__ == '__main__':
    unittest.main()
