"""Resumed X4 deciding source DATA controls; no native SQL/authority claims."""
from contextlib import contextmanager
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from cortex_core.install import core_sql, lifecycle, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import thaw
from cortex_core.runtime import role_grants
import test_core_sql as phases

class Resumed(unittest.TestCase):
    def test_isolation_precedes_first_snapshot_query(self):
        a=role_grants._RoleAdapter.__new__(role_grants._RoleAdapter);a._finite_boundary=lambda:2_000_000_000
        queries=[]
        c=SimpleNamespace(execute=lambda q,*a,**k:queries.append(q) or 'result')
        with patch.object(role_grants.time,'monotonic_ns',return_value=1_000_000_000):
            self.assertEqual(a._execute(c,'SET TRANSACTION ISOLATION LEVEL READ COMMITTED'),'result')
        self.assertEqual(queries[0],'SET TRANSACTION ISOLATION LEVEL READ COMMITTED','X4_ISOLATION_QUERY_ORDER')
        self.assertIn('statement_timeout',queries[1])

    def test_fixed_owner_recovery_uses_existing_measure_signature(self):
        helper=phases.CoreSQL()
        with tempfile.TemporaryDirectory() as root:
            s=helper.fixture(root);p=helper.primitive(s,'init');called=[]
            class Inner:
                def measure(self,context):called.append(context);return ('NOT_APPLIED',b'original')
            p.primitive=Inner();ctx={'test':'DATA'}
            with patch.object(p,'_guard'),patch.object(p,'_finish',return_value=b'joined'):
                try:result=p.measure(ctx,recovery=object())
                except TypeError:self.fail('X4_RECOVERY_FORWARDING_CHANGED_FIXED_SIGNATURE')
            self.assertEqual(result,('NOT_APPLIED',b'joined'));self.assertEqual(called,[ctx])

    def test_finite_stop_survives_terminal_publication_until_ACK(self):
        helper=phases.CoreSQL()
        with tempfile.TemporaryDirectory() as root:
            s=helper.fixture(root);p=helper.primitive(s,'init')
            windows=json.loads(s.history.read(s.windows));windows['E']=103_000_000_000
            s.windows=s.history.retain(canonical_bytes(windows));p.windows_ref=s.windows
            now=[101_000_000_000];original=s.history._publish;published=[]
            def publish(folder,name,raw):
                answer=original(folder,name,raw)
                event=json.loads(raw) if folder=='events' else {}
                if event.get('result_ref') is not None:now[0]=104_000_000_000;published.append(True)
                return answer
            with s.installed(p),patch.object(lifecycle.time,'monotonic_ns',side_effect=lambda:now[0]),patch.object(s.history,'_publish',publish):
                try:lifecycle.execute_effect(s.data['request_raw'].encode())
                except owner.OwnerRefusal as error:self.assertEqual(error.detail,'reservation_expired')
                else:self.fail('X4_FINITE_LATE_ACK_ACCEPTED')
            self.assertTrue(published);self.assertEqual(s.effects,1)

    def test_ordinary_restart_uses_fresh_authority_not_old_finite_window(self):
        helper=phases.CoreSQL()
        with tempfile.TemporaryDirectory() as root:
            s=helper.fixture(root)
            for phase in phases.PHASES:helper.run_phase(s,phase)
            s.helper.edit_record(s.data,'clock_ref',lambda c:c.update(now_utc=1700000040,monotonic_ns=140_000_000_000))
            s.helper.clock(s.data,lambda c:c.update(now_utc=1700000041,monotonic_ns=141_000_000_000))
            s.helper.clock_receipt(s.data,lambda r:r['monotonic_deadlines'].update({k:200_000_000_000 for k in r['monotonic_deadlines']}))
            p=helper.primitive(s,'ordinary');count=s.effects
            with s.installed(p),patch.object(lifecycle.time,'monotonic_ns',return_value=141_000_000_000):
                try:ref=lifecycle.execute_effect(s.data['request_raw'].encode())
                except owner.OwnerRefusal as error:self.fail('X4_ORDINARY_BORROWED_OLD_WINDOW '+error.detail)
            self.assertIsNotNone(ref);self.assertEqual(s.effects,count)

class Baseline(unittest.TestCase):
    def fixture(self,root):
        s=phases.CoreSQL().fixture(root)
        def attestation(inputs):
            c=json.loads(inputs.context_raw);head=s.history.head()[0]
            value={'kind':'explicit_empty','identity':c['identity'],
                'generation':c['policy_generation'],'fence':c['host_lease']['fence'],
                'revision':c['state_revision'],'head_ref':head,'release_binding_ref':None,
                'journal':{'device':s.history._identity[0],'inode':s.history._identity[1],
                           'outside_database_restore':True}}
            return value
        s.attestation=attestation;s.baseline_damage=None
        original=s.inputs
        def inputs():
            i=original();c=json.loads(i.context_raw);state=owner._original(i,c['current_state_ref'])
            value=s.attestation(i)
            if s.baseline_damage:s.baseline_damage(value)
            raw=canonical_bytes(value);ref=lifecycle._ref('baseline.json',raw)
            state['baseline_ref']=ref;state_raw=canonical_bytes(state)
            c['current_state_ref']=lifecycle._ref(c['current_state_ref']['path'],state_raw)
            from dataclasses import replace
            return replace(i,context_raw=canonical_bytes(c),originals={**i.originals,
                ref['path']:raw,c['current_state_ref']['path']:state_raw})
        s.inputs=inputs;return s
    def read(self,s):
        fn=getattr(core_sql,'_current_baseline',None)
        self.assertTrue(callable(fn),'X4_BASELINE_PRODUCER_ABSENT')
        return fn(s.history,s.data['request_raw'].encode())
    def test_explicit_empty_retains_original_refs_no_database_or_second_journal(self):
        with tempfile.TemporaryDirectory() as root:
            s=self.fixture(root)
            with s.installed():b=self.read(s)
            self.assertEqual(b['kind'],'explicit_empty');self.assertIsNone(b['head_ref'])
            self.assertEqual(json.loads(s.history.read(b['baseline_ref']))['revision'],7)
            self.assertIsNone(s.history.head()[0])
    def test_missing_attestation_is_not_empty_inference(self):
        with tempfile.TemporaryDirectory() as root:
            s=phases.CoreSQL().fixture(root)
            with s.installed(),self.assertRaises(owner.OwnerRefusal):self.read(s)
    def test_wrong_generation_fence_or_journal_custody_refuses(self):
        for field in ('generation','fence','revision','journal','kind'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                s=self.fixture(root)
                def damage(v):
                    if field=='journal':v[field]['outside_database_restore']=False
                    elif field=='kind':v[field]='inferred_empty'
                    else:v[field]+=1
                s.baseline_damage=damage
                with s.installed(),self.assertRaises(owner.OwnerRefusal):self.read(s)
    def test_installed_needs_original_applied_cleaned_lineage(self):
        with tempfile.TemporaryDirectory() as root:
            s=self.fixture(root);s.baseline_damage=lambda v:v.update(kind='installed')
            with s.installed(),self.assertRaises(owner.OwnerRefusal):self.read(s)
