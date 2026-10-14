"""Coordinated RC0 revision and effect-free OFF selection boundaries."""
import asyncio
import copy
import unittest

import test_e0 as e0
from cortex_core.embeddings.pg_search import CapabilityUnavailable
from cortex_core.runtime.signed_member import Refusal


class AdmissionRevision(unittest.TestCase):
    def positive(self, lock, cfg):
        try:return e0.admit(lock,cfg)
        except Refusal as error:raise e0.SemanticRed('valid ratified declaration refused: '+error.reason) from None

    def test_off_both_targets_retained_or_absent_provider_never_selected(self):
        for target in ('linux/amd64','linux/arm64'):
            for retained in (True,False):
                with self.subTest(target=target,retained=retained):
                    lock,cfg=e0.materials()
                    # admit() is the frozen arm64 join; these target-specific
                    # assertions use the same admitted member port explicitly.
                    cfg=copy.deepcopy(e0.FIXTURE['mode_configs'][target]['api'])
                    cfg['ai_mode']='off';cfg['runtime_lock_sha256']=e0.sha256(e0.encode(lock))
                    if not retained:cfg.pop('embedding');cfg['resolver']['handles'].pop('provider')
                    double=e0.NonAuthoritativeSignedMemberDouble(e0.encode(lock))
                    with double.installed(e0.signed_member):member=e0.signed_member.request_member('test-only-aggregate','runtime.lock.json')
                    try:plan=e0.config.admit_config(member,e0.encode(cfg),aggregate_id='test-only-aggregate',observed_target=target,mode='api')
                    except Refusal as error:raise e0.SemanticRed(error.reason) from None
                    ai=e0.field(plan,'ai')
                    self.assertEqual(ai.mode,'off');self.assertIsNone(ai.identity)
                    self.assertNotIn('provider',plan.secret_handles);self.assertFalse(plan.effect_authority)

    def test_contract_descriptor_half_update_and_non_ai_gap_refuse(self):
        lock,cfg=e0.materials('off')
        self.positive(lock,cfg)
        for key in ('lock_schema','config_schema','operations'):
            with self.subTest(key=key):
                bad=copy.deepcopy(lock);bad['contract_members'][key]['value']['sha256']='1'*64
                cfg2=copy.deepcopy(cfg);cfg2['runtime_lock_sha256']=e0.sha256(e0.encode(bad))
                with self.assertRaises(Refusal):e0.admit(bad,cfg2)
        bad=copy.deepcopy(cfg);bad['embedding']={'state':'GAP','gap_id':'embedding','owner_role':'provider-owner','reason':'unbound','blocks':['api']}
        with self.assertRaises(Refusal):e0.admit(lock,bad)


class SelectionBoundary(unittest.IsolatedAsyncioTestCase):
    def gate(self):
        cls=e0.surface('cortex_core.runtime.selection','SelectionGate')
        return cls(e0.admitted_ai())

    async def test_epoch_never_reused_across_apply_or_gate_instances(self):
        gate=self.gate();other=self.gate();old=gate.snapshot.revision
        self.assertNotEqual(old,other.snapshot.revision)
        await gate.apply(e0.admitted_ai())
        self.assertNotEqual(old,gate.snapshot.revision)
        called=[]
        async def work(deadline):called.append(True)
        with self.assertRaises(CapabilityUnavailable):await gate.run(work,revision=old,deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual(called,[])
