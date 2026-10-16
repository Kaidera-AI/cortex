"""X1-c source barrier joins; actual P1 IO, synthetic native stage/manager facts.

These 19 named fault variants are SOURCE seams, not authentic X0/native credit.
X6 remains responsible for real timed drain/close/reap and exact native objects.
"""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import lifecycle as p1, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
from test_hold1_interface import Subject

STAGES=('S01','S02','S03','S04','S05','S06','S07','S08')
FACTS={
 'S01':('materialized','owned_preimages','exact_tuple'),
 'S02':('new_empty_owned_root','init_complete','finite_absent'),
 'S03':('sql_session','core_lifetime','probe_closed'),
 'S04':('ledger','catalogue','acl','finite_absent','bootstrap_disabled','migrator_closed'),
 'S05':('restricted_factory','complete_clients','current_scope'),
 'S06':('api_listener','telemetry_listener','namespace','publication','fatal_pair'),
 'S07':('supervisor_session','current_lease','current_fence'),
 'S08':('stack_healthy','enrollment_current','scoped_read')}


class Chain:
    def __init__(self,root):
        self.root=Path(root);self.history=p1._History(self.root);self.prior=[]
        self.calls=[];self.counter=0;self.before=canonical_bytes({'source_fixture_preimage':'owned'})
        self.last=None;self.operation=None

    def subject(self,label,*,epoch=None):
        self.counter+=1;s=Subject(self.root);s.history=self.history
        c=json.loads(s.data['context_raw']);q=json.loads(s.data['request_raw'])
        if self.operation:q['identity']['operation_id']=self.operation
        q['identity'].update(request_id='request-'+str(self.counter),attempt_id='attempt-'+str(self.counter))
        raw=canonical_bytes(q);path=c['request_ref']['path']
        s.data['request_raw']=raw.decode();s.data['originals'][path]=raw.decode()
        effect=copy.deepcopy(c['effect']);effect.update(effect_id=label,resource_kind='service',
            expected_before_ref=p1._ref('stage/before.json',self.before))
        s.data['originals']['stage/before.json']=self.before.decode()
        s.helper.context(s.data,lambda c:c.update(identity=q['identity'],effect=effect,
            request_ref=p1._ref(path,raw),nonce='nonce-'+str(self.counter)))
        s.helper.scope(s.data,lambda c:c.update(identity=q['identity'],effect=effect))
        if epoch:
            s.helper.context(s.data,lambda c:c.update(owner_epoch=epoch,session_id=epoch+'-session'))
            s.helper.edit_record(s.data,'peer_ref',lambda c:c.update(owner_epoch=epoch,session_id=epoch+'-session'))
            s.helper.clock_receipt(s.data,lambda c:c.update(owner_epoch=epoch))
            s.helper.edit_record(s.data,'clock_ref',lambda c:c.update(owner_epoch=epoch))
            s.helper.clock(s.data,lambda c:c.update(owner_epoch=epoch))
        with s.installed():s.proposal=s.current();s.deadline=s.proposal.deadline
        self.last=s;return s

    def run(self,s,barrier,observed,*,state='applied_verified',after_snapshot=None):
        outer=self
        class Primitive:
            def snapshot(self):
                if after_snapshot:after_snapshot()
                return {'service':outer.before}
            def dispatch(self,effect):outer.calls.append(s.proposal.context['effect']['effect_id'])
            def measure(self,effect):
                c=thaw(effect['context'])
                return canonical_bytes({k:c[k] for k in ('identity','effect','request_ref')} | {
                    'state':state,'observed':observed})
        if type(barrier) is getattr(p1,'_Stop',None) and barrier.phase=='core':
            observed=copy.deepcopy(observed)
            observed['stop'].setdefault('dependents_result_ref',barrier.original_ref)
        primitive=Primitive()
        with s.installed(primitive),patch.object(p1,'_installed_lifecycle',return_value=p1._Bindings(
                self.history,{'service':primitive},barrier)):
            return p1.execute_effect(s.data['request_raw'].encode())


class Barriers(unittest.TestCase):
    def stage(self,name,prior=(),**kw):
        cls=getattr(p1,'_Stage',None)
        self.assertIsNotNone(cls,'X1C fixed stage barrier absent')
        return cls(name,tuple(prior),**kw)

    def stop(self,phase,original_ref=None):
        cls=getattr(p1,'_Stop',None)
        self.assertIsNotNone(cls,'X1C common stop barrier absent')
        return cls(phase,original_ref)

    def observed(self,stage,**changes):
        value={'stage':stage,'barriers':dict.fromkeys(FACTS[stage],True),'remaining':[]}
        if stage=='S08':value['readiness']='ready'
        value.update(changes);return value

    def ok(self,c,name,**kw):
        s=c.subject(name);r=c.run(s,self.stage(name,c.prior,**kw),self.observed(name))
        c.prior.append(thaw(r));return thaw(r)

    def test_fresh_all_eight_exact_original_results_are_read_back(self):
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root)
            for name in STAGES:self.ok(c,name)
            self.assertEqual(c.calls,list(STAGES));self.assertEqual(len(c.history.events()),24)
            self.assertEqual(p1._History(c.root).head()[0],c.prior[-1])
            for name,ref in zip(STAGES,c.prior):
                event,obs=p1._load_entry(c.history,ref);result=p1._decode(c.history.read(event['result_ref']))
                self.assertEqual(result['observed']['stage'],name)
                self.assertEqual(result['identity'],event['identity'])
                self.assertEqual(result['effect'],event['effect']);self.assertEqual(result['request_ref'],event['request_ref'])
                self.assertEqual(p1._decode(c.history.read(obs['reservation_ref']))['state'],'consumed')

    def _mech(self,case,bad,fact,next_stage):
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root)
            for name in STAGES[:STAGES.index(bad)]:self.ok(c,name)
            s=c.subject(bad);observed=self.observed(bad);observed['barriers'][fact]=False
            observed['remaining']=[case+'-unresolved']
            with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stage(bad,c.prior),observed)
            failed=c.history.head()[0];c.prior.append(failed)
            s=c.subject(next_stage);before=len(c.calls)
            with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stage(next_stage,c.prior),self.observed(next_stage))
            self.assertEqual(len(c.calls),before)
            self.assertEqual(p1._History(c.root).head()[0],failed)
            self.assertEqual(p1._decode(c.history.read(c.history.head()[1]['result_ref']))['observed']['remaining'],[case+'-unresolved'])
        # Matched restoration has exact successful original P1 receipts.
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root)
            for name in STAGES[:STAGES.index(next_stage)+1]:self.ok(c,name)
            self.assertEqual(c.calls[-1],next_stage)

    def test_MECH01(self):
        self._mech('MECH01','S03','sql_session','S04')

    def test_MECH02A(self):
        self._mech('MECH02A','S04','ledger','S05')

    def test_MECH02B(self):
        self._mech('MECH02B','S04','catalogue','S05')

    def test_MECH03(self):
        self._mech('MECH03','S04','finite_absent','S05')

    def test_MECH04(self):
        self._mech('MECH04','S05','complete_clients','S06')

    def test_MECH05(self):
        self._mech('MECH05','S06','telemetry_listener','S07')

    def test_MECH06(self):
        self._mech('MECH06','S07','current_lease','S08')


    def test_missing_reordered_foreign_and_changed_predecessors_refuse_before_consume(self):
        for mode in ('missing','reordered','foreign','changed','not-applied'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                c=Chain(root);self.ok(c,'S01');self.ok(c,'S02');prior=list(c.prior)
                if mode=='missing':prior=prior[:1]
                elif mode=='reordered':prior.reverse()
                elif mode=='foreign':
                    s=c.subject('S03');s.helper.context(s.data,lambda x:x['target'].update(arch='arm64'))
                    s.helper.scope(s.data,lambda x:x['target'].update(arch='arm64'))
                elif mode=='changed':pass
                else:
                    # A genuine durable NOT_APPLIED never becomes a stage success.
                    c2=Chain(Path(root)/'not-applied');s2=c2.subject('S01')
                    with self.assertRaises(owner.OwnerRefusal):c2.run(s2,self.stage('S01'),self.observed('S01'),state='not_applied_verified')
                    c=c2;prior=[c.history.head()[0]]
                if mode!='foreign':s=c.subject('S03' if mode!='not-applied' else 'S02')
                before=len(c.history.events());calls=len(c.calls)
                if mode=='changed':
                    p=c.root/prior[0]['path'];p.write_bytes(p.read_bytes()+b' ')
                with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stage('S03' if mode!='not-applied' else 'S02',prior),self.observed('S03'))
                count=len(list((c.root/'events').glob('*.json'))) if mode=='changed' else len(c.history.events())
                self.assertEqual(count,before);self.assertEqual(len(c.calls),calls)

    def test_enrollment_required_is_healthy_without_writing_first_identity(self):
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root)
            for name in STAGES[:-1]:self.ok(c,name)
            s=c.subject('S08');observed=self.observed('S08',readiness='enrollment_required')
            observed['barriers'].update(enrollment_current=False,scoped_read=False)
            r=c.run(s,self.stage('S08',c.prior),observed)
            self.assertEqual(c.calls[-1],'S08');self.assertEqual(c.history.head()[0],thaw(r))

    def stop_before(self,c):
        c.operation='stop-operation'
        s=c.subject('stop-dependents');d=s.deadline
        c.before=canonical_bytes({'t0_ns':101000000000,'deadline_ns':105000000000,
            'expires_monotonic_ns':d.expires_monotonic_ns,'owner_epoch':d.owner_epoch,
            'installation_id':s.proposal.context['identity']['installation_id'],
            'operation_id':s.proposal.context['identity']['operation_id'],'inventory':['api','lease','core']})
        s=c.subject('stop-dependents')
        return s

    def stop_observed(self,phase,**change):
        # T=4s, rational cutoffs rounded DOWN: never gain time.
        value={'phase':phase,'t0_ns':101000000000,'deadline_ns':105000000000,
            'c1_ns':102333333333,'c2_ns':103666666666,'c3_ns':104333333333,
            'dependents_absent_at_ns':103000000000,'lease_fenced':True,
            'core_term_at_ns':None,'core_force_at_ns':None,'core_absent_at_ns':None,'remaining':[]}
        if phase=='core':value.update(core_term_at_ns=104000000000,core_force_at_ns=104500000000,core_absent_at_ns=104900000000)
        value.update(change);return {'stop':value}

    def stop_pair(self,c):
        s=self.stop_before(c);dep=thaw(c.run(s,self.stop('dependents'),self.stop_observed('dependents')))
        s=c.subject('stop-core');core=thaw(c.run(s,self.stop('core',dep),self.stop_observed('core')))
        return dep,core

    def _stop_unproved(self,case):
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root);s=self.stop_before(c)
            changes={'STOP01':{'dependents_absent_at_ns':None,'remaining':['api']},
                     'STOP02':{'dependents_absent_at_ns':103666666667},
                     'STOP03':{'lease_fenced':False}}
            o=self.stop_observed('dependents',**changes[case])
            with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stop('dependents'),o)
            anchor=c.history.head()[0];s=c.subject('stop-core');before=len(c.calls)
            with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stop('core',anchor),self.stop_observed('core'))
            self.assertEqual(len(c.calls),before);self.assertNotIn('stop-core',c.calls)
        with tempfile.TemporaryDirectory() as root:self.stop_pair(Chain(root))

    def test_STOP01(self):
        self._stop_unproved('STOP01')

    def test_STOP02(self):
        self._stop_unproved('STOP02')

    def test_STOP03(self):
        self._stop_unproved('STOP03')


    def test_STOP04_Core_last_force_cutoff_and_final_absence_are_required(self):
        for change in ({'core_term_at_ns':102900000000},{'core_force_at_ns':104000000000},
                       {'core_absent_at_ns':105000000001},{'core_absent_at_ns':None,'remaining':['core']}):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as root:
                c=Chain(root);s=self.stop_before(c)
                dep=c.run(s,self.stop('dependents'),self.stop_observed('dependents'));s=c.subject('stop-core')
                with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stop('core',thaw(dep)),self.stop_observed('core',**change))
        with tempfile.TemporaryDirectory() as root:self.stop_pair(Chain(root))

    def test_Core_receipt_retains_the_exact_dependent_original(self):
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root);s=self.stop_before(c)
            dep=c.run(s,self.stop('dependents'),self.stop_observed('dependents'))
            s=c.subject('stop-core')
            with self.assertRaises(owner.OwnerRefusal):
                c.run(s,self.stop('core',thaw(dep)),self.stop_observed('core',dependents_result_ref={'wrong':'original'}))

    def test_STOP05_06_duplicates_and_manager_escalation_never_create_new_stop_budget(self):
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root);dep,core=self.stop_pair(c);s=c.last;before=c.history.head()[0]
            for _ in range(3):
                with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stop('core',dep),self.stop_observed('core'))
                self.assertEqual(c.history.head()[0],before)
            self.assertEqual(c.calls,['stop-dependents','stop-core'])
            # A different original D/preimage cannot replace the retained stop.
            c.before=canonical_bytes(json.loads(c.before)|{'deadline_ns':106000000000})
            s=c.subject('stop-core-escalated')
            with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stop('core',dep),self.stop_observed('core'))
            self.assertEqual(c.calls,['stop-dependents','stop-core'])

    def ordinary(self,c,baseline,stop_ref,**change):
        old_context=p1._decode(c.history.read(p1._load_entry(c.history,baseline[0])[1]['context_ref']))
        data={'manager_epoch':'fresh-manager','old_generation_absent':dict.fromkeys(('core','api','telemetry','finite','supervisor'),True),'original_stop_ref':stop_ref,
              'target':old_context['target']};data.update(change)
        c.before=canonical_bytes(data)
        # A separately admitted current restart operation retains the data.
        c.operation='restart-operation'
        s=c.subject('S01',epoch='fresh-manager')
        q=json.loads(s.data['request_raw']);q['identity']['operation_id']='restart-operation'
        raw=canonical_bytes(q);path=json.loads(s.data['context_raw'])['request_ref']['path']
        s.data['request_raw']=raw.decode();s.data['originals'][path]=raw.decode()
        s.helper.context(s.data,lambda x:x.update(identity=q['identity'],request_ref=p1._ref(path,raw)))
        s.helper.scope(s.data,lambda x:x.update(identity=q['identity']))
        return s

    def _restart_blocked(self,case):
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root)
            for name in STAGES:self.ok(c,name)
            baseline=list(c.prior);dep,stop_ref=self.stop_pair(c)
            absent=dict.fromkeys(('core','api','telemetry','finite','supervisor'),True)
            if case in ('RESTART01','RESTART02A','RESTART02B'):
                absent[{'RESTART01':'core','RESTART02A':'api','RESTART02B':'finite'}[case]]=False
            bad_baseline=baseline[:-1] if case=='RESTART05' else baseline
            bad_stop=dep if case=='RESTART03' else stop_ref
            s=self.ordinary(c,baseline,bad_stop,old_generation_absent=absent)
            before=len(c.calls)
            with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stage('S01',baseline=tuple(bad_baseline),stop_ref=bad_stop),self.observed('S01'))
            self.assertEqual(len(c.calls),before)
            # Same exact prior source truth, independently restored absence.
            s=self.ordinary(c,baseline,stop_ref)
            r=c.run(s,self.stage('S01',baseline=tuple(baseline),stop_ref=stop_ref),self.observed('S01'))
            self.assertEqual(c.calls[-1],'S01');self.assertEqual(thaw(r),c.history.head()[0])

    def test_RESTART01(self):
        self._restart_blocked('RESTART01')

    def test_RESTART02A(self):
        self._restart_blocked('RESTART02A')

    def test_RESTART02B(self):
        self._restart_blocked('RESTART02B')

    def test_RESTART03(self):
        self._restart_blocked('RESTART03')

    def test_RESTART05(self):
        self._restart_blocked('RESTART05')


    def test_RESTART04_ordinary_refuses_init_bootstrap_migrate_and_second_enrollment(self):
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root)
            for name in STAGES:self.ok(c,name)
            baseline=list(c.prior);_,stop_ref=self.stop_pair(c)
            s=self.ordinary(c,baseline,stop_ref);r=c.run(s,self.stage('S01',baseline=tuple(baseline),stop_ref=stop_ref),self.observed('S01'))
            for name in ('S02','S04'):
                s=c.subject(name,epoch='fresh-manager');before=len(c.calls)
                with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stage(name,(thaw(r),),baseline=tuple(baseline),stop_ref=stop_ref),self.observed(name))
                self.assertEqual(len(c.calls),before)
            prior=[thaw(r)]
            for name in ('S03','S05','S06','S07','S08'):
                s=c.subject(name,epoch='fresh-manager')
                r=c.run(s,self.stage(name,tuple(prior),baseline=tuple(baseline),stop_ref=stop_ref),self.observed(name))
                prior.append(thaw(r))
            self.assertEqual(c.calls[-6:],['S01','S03','S05','S06','S07','S08'])

    def test_stage_authority_loss_after_snapshot_prevents_consume_and_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            c=Chain(root);s=c.subject('S01')
            with self.assertRaises(owner.OwnerRefusal):c.run(s,self.stage('S01'),self.observed('S01'),after_snapshot=lambda:setattr(s,'revoked',True))
            self.assertEqual(c.calls,[]);self.assertEqual(c.history.events(),[])
