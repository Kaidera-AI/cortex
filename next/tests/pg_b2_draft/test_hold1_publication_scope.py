"""Actual P1/lifecycle publication scope probe; SOURCE only, no native claim."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import lifecycle as p1, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import thaw
from psycopg.pq import TransactionStatus
from test_hold1_producer import SQLSubject


CORTEX_TEST_OBSERVATIONS = {}


class PublicationScope(unittest.TestCase):
    def foreign_scope(self,s):
        q=json.loads(s.data['request_raw'])
        q['operation']='install';q['journal_ref']=None
        q['identity'].update(request_id='foreign-publication-request',attempt_id='foreign-publication-attempt',
            parent_attempt_id=None,operation_id='00000000-0000-4000-8000-000000000099')
        raw=canonical_bytes(q);c=json.loads(s.data['context_raw']);path=c['request_ref']['path']
        s.data['request_raw']=raw.decode();s.data['originals'][path]=raw.decode()
        s.helper.context(s.data,lambda c:c.update(identity=q['identity'],request_ref=p1._ref(path,raw),
            nonce='foreign-publication-nonce'))
        s.helper.context(s.data,lambda c:c['effect'].update(effect_id='foreign-publication-effect'))
        effect=json.loads(s.data['context_raw'])['effect']
        s.helper.scope(s.data,lambda scope:scope.update(identity=q['identity'],operation='install',
            effect=effect,resource_ids=['foreign-owned-role']))
        s.private['operation_id']=q['identity']['operation_id'];s.set_private()
        # Prove this is a valid unrelated common-owner DATA proposal, not an
        # invalid/refused fixture or a modified historical SQL attempt.
        current=owner.propose_effect(raw)
        admitted=p1._admitted_deadline(s.deadline)[1]
        self.assertEqual(current.context['identity']['request_id'],'foreign-publication-request')
        self.assertEqual(admitted.deadline,s.deadline)
        return thaw(admitted.context)

    def export(self,s,name,**values):
        head,event=s.history.head()
        values.update(head=head,event=event,events=len(s.history.events()),python=sys.version,
            qualifier='SOURCE DATA: real P1/flock, scripted SQL observations, nonauthoritative native/signed-member inputs')
        CORTEX_TEST_OBSERVATIONS[name] = values

    def test_control_current_recovery_scope_publishes_one_terminal(self):
        with tempfile.TemporaryDirectory() as root:
            s=SQLSubject(root)
            with s.installed(s.primitive):
                effect=s.dispatch(schedule=5);s.recover(effect)
                request=s.data['request_raw'].encode();before=len(s.history.events())
                p1.execute_effect(request)
                self.assertEqual(len(s.history.events()),before+1)
                _,_,parts=s.entry();self.assertEqual(parts['attempt']['dispatch_state'],'applied_verified')
                self.export(s,'SCOPE_CONTROL',qualified=True)

    def test_foreign_scope_at_journal_acquisition_must_not_publish_old_terminal(self):
        with tempfile.TemporaryDirectory() as root:
            s=SQLSubject(root)
            with s.installed(s.primitive):
                effect=s.dispatch(schedule=5);s.recover(effect)
                request=s.data['request_raw'].encode();before=s.history.head()[0]
                original_lock=s.history._lock;switched=[]
                @contextmanager
                def lock(name,*,deadline=None):
                    if name=='.journal.lock' and not switched:
                        self.assertEqual(s.reader.info.transaction_status,TransactionStatus.INTRANS)
                        switched.append(self.foreign_scope(s))
                    with original_lock(name,deadline=deadline):yield
                refusal=None;refusal_trace=None
                with patch.object(s.history,'_lock',side_effect=lock):
                    try:p1.execute_effect(request)
                    except owner.OwnerRefusal as error:
                        import traceback
                        refusal=error.detail;refusal_trace=traceback.format_exc()
                self.export(s,'SCOPE_GAP',before_head=before,current_foreign_context=switched[0] if switched else None,
                    returned_refusal=refusal,refusal_trace=refusal_trace,terminal_was_appended=s.history.head()[0]!=before)
                self.assertEqual(len(switched),1,'scope replacement did not reach journal acquisition')
                self.assertIsNotNone(refusal,'stale old caller received success')
                self.assertEqual(s.history.head()[0],before,
                    'HOLD1_FOREIGN_PUBLICATION_SCOPE_APPENDED_TERMINAL_BEFORE_REFUSAL')
