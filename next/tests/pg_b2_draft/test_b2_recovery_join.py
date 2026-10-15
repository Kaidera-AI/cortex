"""Recovery measurement SOURCE DATA: real P1 custody, fixed gate double, no SQL effect.

original/renewed helpers copied from Finn's test_b2_recovery_contract.py.
Only test-owned observations/connection recorders; no installed/native authority.
"""
import copy, hashlib, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from cortex_core.install import lifecycle, owner
from cortex_core.runtime import role_grants
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
from cortex_core.runtime import signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
import test_lifecycle_receipt as storage
import test_owner_proposal as proposals
import test_hale_review as hale
from test_pg_b2_rework import Connection

class RecoveryFixture(unittest.TestCase):
    def original(self, root):
        check=storage.LifecycleReceiptOracle()
        history=lifecycle._History(Path(root)/'history')
        helper,data,_=check.setup_data()
        helper.context(data,lambda c:c['effect'].update(resource_kind='database_role'))
        helper.scope(data,lambda s:s['effect'].update(resource_kind='database_role'))
        first,_=helper.run_gate(owner,data)
        with patch.object(check,'setup_data',return_value=(helper,data,first)):
            data,first,reserved,consumed=check.consume(lifecycle,history)
        current, _=check.current(helper,data,history.current_revision())
        c=thaw(current.context)
        _, obs=lifecycle._load_entry(history,consumed)
        effect=freeze({'context':c,'journal_ref':consumed,'preimages_ref':obs['preimages_ref']})
        adapter=object.__new__(role_grants._RoleAdapter)
        adapter.history=history;adapter.identity=c['identity'];adapter.context=effect
        fresh=copy.deepcopy(data)
        helper.context(fresh,lambda c:c.update(state_revision=history.current_revision()))
        helper.edit_record(fresh,'current_state_ref',lambda s:s.update(revision=history.current_revision()))
        return check,helper,history,data,fresh,effect,adapter

    def renewed(self, helper, data, consumed, *, recover):
        d=copy.deepcopy(data)
        q=json.loads(d['request_raw'])
        if recover:
            old=q['identity']['attempt_id'];q['identity'].update(request_id='p2b-recovery-request',attempt_id='p2b-recovery-attempt',parent_attempt_id=old)
            q.update(operation='recover',journal_ref=thaw(consumed))
        raw=proposals.encode(q);oldref=json.loads(d['context_raw'])['request_ref']
        d['request_raw']=raw.decode();d['originals'][oldref['path']]=raw.decode()
        helper.context(d,lambda c:c.update(identity=q['identity'],owner_epoch='p2b-renewed-epoch',session_id='p2b-renewed-session',nonce='p2b-renewed-nonce',request_ref={**oldref,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}))
        helper.edit_record(d,'peer_ref',lambda p:p.update(owner_epoch='p2b-renewed-epoch',session_id='p2b-renewed-session'))
        helper.clock_receipt(d,lambda r:r.update(owner_epoch='p2b-renewed-epoch'))
        helper.edit_record(d,'clock_ref',lambda r:r.update(owner_epoch='p2b-renewed-epoch'))
        helper.clock(d,lambda r:r.update(owner_epoch='p2b-renewed-epoch'))
        helper.scope(d,lambda s:s.update(identity=q['identity'],operation=q['operation']))
        helper.context(d,lambda c:c['host_lease'].update(fence=c['host_lease']['fence']+1))
        helper.edit_record(d,'current_state_ref',lambda s:s.update(host_fence=s['host_fence']+1))
        proposal,_=helper.run_gate(owner,d)
        self.assertEqual(proposal.context['owner_epoch'],'p2b-renewed-epoch')
        self.assertEqual(proposal.context['identity'],q['identity'])
        return d,proposal


    def fixture(self, root):
        roles={'roles':[[oid,name,False,False,False,False,False,False,False,-1,None,None]
                        for name,oid in hale.OIDS.items()], 'memberships':[]}
        self.before={'role':canonical_bytes(roles),'acl':canonical_bytes({'objects':[], 'defaults':[]}),
            'database':canonical_bytes({'database':[55,'hale_source_db',hale.OIDS[hale.ROLES['bootstrap']],True,-1],'bindings':None}),
            'hba':b'# exact before\r\n','carrier':b'{"source":"before"}\n'}
        with patch.object(storage.LifecycleReceiptOracle,'preimages',return_value=self.before):
            values=self.original(root)
        check,helper,history,old,current,effect,a=values
        a.roles=dict(hale.ROLES);a.created={};a.connection=Connection();a.recovery=Connection()
        a._sql_transaction=None;a.database=(55,'hale_source_db',hale.OIDS[hale.ROLES['bootstrap']],True,-1)
        a.live=dict(self.before);a.reads=[]
        def rows(conn):a.reads.append('role');return json.loads(a.live['role'])
        a._roles=rows;a._acl=lambda conn:json.loads(a.live['acl'])
        class Materials:
            def snapshot(inner):a.reads.append('material');return {k:a.live[k] for k in ('hba','carrier')}
            def restore(inner,values):raise AssertionError('RECOVERY_MUST_NOT_RESTORE')
        a.materials=Materials()
        return values

    def authority(self,helper,data,history,effect):
        fresh=copy.deepcopy(data)
        helper.context(fresh,lambda c:c.update(state_revision=history.current_revision()))
        helper.edit_record(fresh,'current_state_ref',lambda s:s.update(revision=history.current_revision()))
        return self.renewed(helper,fresh,effect['journal_ref'],recover=True)[0]

    def bound(self,helper,data):
        from contextlib import ExitStack
        stack=ExitStack()
        stack.enter_context(patch.object(owner,'_installed_owner_inputs',side_effect=lambda:helper.inputs(owner,data)))
        double=NonAuthoritativeSignedMemberDouble(proposals.F['runtime_lock_raw'].encode(),aggregate_id=data['aggregate_id'],member_name='runtime.lock.json')
        stack.enter_context(double.installed(signed_member))
        return stack

    def answer(self,a,effect,helper,data):
        before=a.history.head()[0]
        facts={p.name:p.read_bytes() for p in (a.history.root/'facts').iterdir()}
        with self.bound(helper,data):
            try:answer=role_grants._Primitive(a,'prepare',a.connection,{}).measure(effect)
            except owner.OwnerRefusal as error:self.fail('B2_FRESH_RECOVERY_HAS_NO_ANSWER:'+error.detail)
        self.assertEqual(a.history.head()[0],before,'RECOVERY_APPENDED_HISTORY')
        self.assertEqual({p.name:p.read_bytes() for p in (a.history.root/'facts').iterdir()},facts,'RECOVERY_RETAINED_NEW_FACTS')
        self.assertFalse(any(not q.startswith('SELECT ') for c in (a.connection,a.recovery) for q,_ in c.calls),'RECOVERY_SQL_EFFECT')
        self.assertIs(type(answer),tuple)
        self.assertIn(answer[0],('APPLIED','NOT_APPLIED','UNKNOWN'))
        return answer

    def truth(self,a,effect,*,state='applied_verified',reconciliation=False,live=None,edit=None):
        live=a.live if live is None else live
        c=thaw(effect['context'])
        result={'identity':c['identity'],'effect':c['effect'],'request_ref':c['request_ref'],
                'state':state,'observed':{'adapter':'finite_pg_roles','originals':{k:a.history.retain(v) for k,v in live.items()}}}
        # Existing P1 canonical result is returned with all opaque observations intact.
        result['observed']['acceptance_note']='original-owner-test-observation'
        if edit:edit(result)
        raw=canonical_bytes(result)
        if reconciliation:
            event,obs=lifecycle._load_entry(a.history,effect['journal_ref'])
            attempt=json.loads(a.history.read(obs['attempt_ref']));attempt['reconciliation_ref']=a.history.retain(raw)
            reservation=json.loads(a.history.read(obs['reservation_ref']));context=json.loads(a.history.read(obs['context_ref']))
            lifecycle._publish_entry(a.history,context=context,reservation=reservation,attempt=attempt,
                preimages_ref=obs['preimages_ref'],request_ref=event['request_ref'],release_ref=event['release_binding_ref'],
                expected_ref=a.history.head()[0],phase='held',recorded_at=1700000001)
        else:lifecycle._record_result(a.history,thaw(effect['journal_ref']),raw)
        return raw

    def refuse(self,a,effect,helper,data):
        a.reads.clear();a.connection.calls.clear();a.recovery.calls.clear()
        with self.bound(helper,data):
            with self.assertRaises(owner.OwnerRefusal):a.measure(effect)
        self.assertEqual(a.reads+a.connection.calls+a.recovery.calls,[],'RECOVERY_AUTHORITY_CHECK_AFTER_READ')

    def rewrite(self,helper,data,change):
        q=json.loads(data['request_raw']);change(q);raw=proposals.encode(q)
        c=json.loads(data['context_raw']);ref=c['request_ref'];data['request_raw']=raw.decode();data['originals'][ref['path']]=raw.decode()
        helper.context(data,lambda c:c.update(identity=q['identity'],target=q['target'],request_ref={**ref,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}))
        helper.scope(data,lambda s:s.update(identity=q['identity'],target=q['target'],operation=q['operation']))
        return data

    def test_finn_fresh_recovery_scope_gets_defined_owner_answer(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_finn_renewed_original_guard_stays_then_child_recovery_answers(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root)
            data,_=self.renewed(h,current,effect['journal_ref'],recover=False)
            with self.bound(h,data):
                with self.assertRaises(owner.OwnerRefusal) as got:a._current(effect)
            self.assertEqual(got.exception.detail,'reservation_invalidated')
            data=self.authority(h,current,history,effect)
            self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_normal_dispatch_cannot_use_recovery_authority(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            with self.bound(h,data):
                with self.assertRaises(owner.OwnerRefusal) as got:a._current(effect)
            self.assertEqual(got.exception.detail,'scope_denied')

    def test_durable_applied_reply_loss_returns_identical_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);raw=self.truth(a,effect)
            data=self.authority(h,current,history,effect)
            self.assertEqual(self.answer(a,effect,h,data),('APPLIED',raw))

    def test_unknown_with_p1_reconciliation_applied_returns_original_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);raw=self.truth(a,effect,reconciliation=True)
            data=self.authority(h,current,history,effect)
            self.assertEqual(self.answer(a,effect,h,data),('APPLIED',raw))

    def test_authoritative_not_applied_returns_exact_result(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);raw=self.truth(a,effect,state='not_applied_verified',reconciliation=True)
            data=self.authority(h,current,history,effect)
            self.assertEqual(self.answer(a,effect,h,data),('NOT_APPLIED',raw))

    def test_matching_before_without_acceptance_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_missing_resource_without_acceptance_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);a.live['role']=canonical_bytes({'roles':[],'memberships':[]})
            data=self.authority(h,current,history,effect);self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_role_oid_drift_cannot_reuse_applied(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);self.truth(a,effect)
            rows=json.loads(a.live['role']);rows['roles'][0][0]+=9000;a.live['role']=canonical_bytes(rows)
            data=self.authority(h,current,history,effect);self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_null_role_oid_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);self.truth(a,effect)
            rows=json.loads(a.live['role']);rows['roles'][0][0]=None;a.live['role']=canonical_bytes(rows)
            data=self.authority(h,current,history,effect);self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_each_material_or_acl_change_is_unknown(self):
        for field in ('hba','carrier','acl'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                _,h,history,old,current,effect,a=self.fixture(root);self.truth(a,effect)
                a.live[field]=(canonical_bytes({'objects':[], 'defaults':[[999]]}) if field=='acl' else a.live[field]+b' ');data=self.authority(h,current,history,effect)
                self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_database_identity_drift_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);self.truth(a,effect);a.database=(999,'foreign',1,True,-1)
            data=self.authority(h,current,history,effect);self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_foreign_result_identity_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root)
            self.truth(a,effect,reconciliation=True,edit=lambda r:r['identity'].update(attempt_id='foreign'))
            data=self.authority(h,current,history,effect);self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_incomplete_five_original_result_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root)
            self.truth(a,effect,reconciliation=True,edit=lambda r:r['observed']['originals'].pop('carrier'))
            data=self.authority(h,current,history,effect);self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_not_applied_with_changed_live_bytes_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);self.truth(a,effect,state='not_applied_verified',reconciliation=True)
            a.live['hba']=b'changed';data=self.authority(h,current,history,effect)
            self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_wrong_parent_refuses_before_reads(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            self.rewrite(h,data,lambda q:q['identity'].update(parent_attempt_id='foreign'));self.refuse(a,effect,h,data)

    def test_foreign_transaction_refuses_before_reads(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            self.rewrite(h,data,lambda q:q['identity'].update(transaction_id='foreign'));self.refuse(a,effect,h,data)

    def test_changed_release_refuses_before_reads(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            self.rewrite(h,data,lambda q:q['release'].update(version='999.0.0'));self.refuse(a,effect,h,data)

    def test_reused_nonce_refuses_before_reads(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            h.context(data,lambda c:c.update(nonce=effect['context']['nonce']));self.refuse(a,effect,h,data)

    def test_unrenewed_epoch_refuses_before_reads(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            epoch=effect['context']['owner_epoch'];h.context(data,lambda c:c.update(owner_epoch=epoch))
            h.edit_record(data,'peer_ref',lambda q:q.update(owner_epoch=epoch));h.clock_receipt(data,lambda q:q.update(owner_epoch=epoch));h.edit_record(data,'clock_ref',lambda q:q.update(owner_epoch=epoch));h.clock(data,lambda q:q.update(owner_epoch=epoch))
            self.refuse(a,effect,h,data)

    def test_unrenewed_session_refuses_before_reads(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            session=effect['context']['session_id'];h.context(data,lambda c:c.update(session_id=session));h.edit_record(data,'peer_ref',lambda q:q.update(session_id=session));self.refuse(a,effect,h,data)

    def test_unrenewed_fence_refuses_before_reads(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            fence=effect['context']['host_lease']['fence'];h.context(data,lambda c:c['host_lease'].update(fence=fence));h.edit_record(data,'current_state_ref',lambda q:q.update(host_fence=fence));self.refuse(a,effect,h,data)

    def test_expired_recovery_scope_refuses_before_reads(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            h.edit_record(data,'consent_ref',lambda q:q.update(expires_at=1700000000));self.refuse(a,effect,h,data)

    def test_ambient_recovery_connection_refuses_before_reads(self):
        from psycopg.pq import TransactionStatus
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            a.recovery.info.transaction_status=TransactionStatus.INTRANS;self.refuse(a,effect,h,data)

    def test_original_expired_deadline_does_not_renew_normal_path(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root)
            # Original authority remains unusable after its immutable reservation deadline.
            h.clock(current,lambda q:q.update(now_utc=1700000006))
            with self.bound(h,current):
                with self.assertRaises(owner.OwnerRefusal):a._current(effect)

    def test_fresh_recovery_uses_own_deadline_after_original_expired(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            delta=100
            h.context(data,lambda c:c['host_lease'].update(expires_at=c['host_lease']['expires_at']+delta))
            h.edit_record(data,'peer_ref',lambda q:q.update(not_before=q['not_before']+delta,expires_at=q['expires_at']+delta))
            h.edit_record(data,'consent_ref',lambda q:q.update(not_before=q['not_before']+delta,expires_at=q['expires_at']+delta))
            h.edit_record(data,'policy_ref',lambda q:q['validity'].update(not_before=q['validity']['not_before']+delta,expires_at=q['validity']['expires_at']+delta))
            h.clock_receipt(data,lambda q:q.update(monotonic_deadlines={k:v+delta*1000000000 for k,v in q['monotonic_deadlines'].items()}))
            h.edit_record(data,'clock_ref',lambda q:q.update(now_utc=q['now_utc']+delta,monotonic_ns=q['monotonic_ns']+delta*1000000000))
            h.clock(data,lambda q:q.update(now_utc=q['now_utc']+delta,monotonic_ns=q['monotonic_ns']+delta*1000000000))
            self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_fresh_deadline_expiring_during_snapshot_refuses(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            original=a.materials.snapshot
            def expired():
                value=original();h.clock(data,lambda q:q.update(now_utc=1700000006));return value
            a.materials.snapshot=expired
            with self.bound(h,data):
                with self.assertRaises(owner.OwnerRefusal):a.measure(effect)
            self.assertIn('material',a.reads)

    def test_journal_advance_during_snapshot_refuses(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            original=a.materials.snapshot
            def advanced():
                value=original();self.truth(a,effect,reconciliation=True);return value
            a.materials.snapshot=advanced
            with self.bound(h,data):
                with self.assertRaises(owner.OwnerRefusal):a.measure(effect)
            self.assertIn('material',a.reads)

    def test_null_acceptance_original_ref_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root)
            self.truth(a,effect,reconciliation=True,edit=lambda r:r['observed']['originals'].update(role=None))
            data=self.authority(h,current,history,effect);self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_missing_original_role_even_matching_truth_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root)
            roles=json.loads(a.live['role']);roles['roles'].pop();a.live['role']=canonical_bytes(roles)
            self.truth(a,effect);data=self.authority(h,current,history,effect)
            self.assertEqual(self.answer(a,effect,h,data),('UNKNOWN',None))

    def test_normal_measurement_keeps_existing_result_contract(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root)
            with self.bound(h,current):raw=role_grants._Primitive(a,'prepare',a.connection,{}).measure(effect)
            c=thaw(effect['context']);bundle=json.loads(history.read(effect['preimages_ref']))
            expected=canonical_bytes({'identity':c['identity'],'effect':c['effect'],'request_ref':c['request_ref'],
                'state':'applied_verified','observed':{'adapter':'finite_pg_roles','originals':bundle['originals']}})
            self.assertIs(type(raw),bytes);self.assertEqual(raw,expected)

    def test_applied_poststate_may_differ_from_original_preimages(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root)
            a.live['hba']=b'# independently accepted after\n';a.live['carrier']=b'{"source":"after"}\n'
            raw=self.truth(a,effect);data=self.authority(h,current,history,effect)
            self.assertNotEqual(a.live,self.before)
            self.assertEqual(self.answer(a,effect,h,data),('APPLIED',raw))

    def test_durable_not_applied_returns_original_result_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);raw=self.truth(a,effect,state='not_applied_verified')
            data=self.authority(h,current,history,effect)
            self.assertEqual(self.answer(a,effect,h,data),('NOT_APPLIED',raw))

    def test_recovery_authority_already_in_p1_history_refuses(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root);data=self.authority(h,current,history,effect)
            c=json.loads(data['context_raw']);event,obs=lifecycle._load_entry(history,effect['journal_ref'])
            attempt=json.loads(history.read(obs['attempt_ref']));attempt.update(identity=c['identity'],request_ref=c['request_ref'])
            reservation=json.loads(history.read(obs['reservation_ref']))
            history.retain(data['request_raw'].encode())
            lifecycle._publish_entry(history,context=c,reservation=reservation,attempt=attempt,
                preimages_ref=obs['preimages_ref'],request_ref=c['request_ref'],release_ref=event['release_binding_ref'],
                expected_ref=history.head()[0],phase='held',recorded_at=1700000001)
            self.refuse(a,effect,h,data)

    def test_null_acceptance_observed_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _,h,history,old,current,effect,a=self.fixture(root)
            self.truth(a,effect,reconciliation=True,edit=lambda r:r.update(observed=None))
            data=self.authority(h,current,history,effect)
            try:answer=self.answer(a,effect,h,data)
            except AttributeError:self.fail('B2_NULL_ACCEPTANCE_ESCAPED_UNKNOWN')
            self.assertEqual(answer,('UNKNOWN',None))
