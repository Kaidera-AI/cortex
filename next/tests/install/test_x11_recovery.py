"""X11-d/X1-d source composition: real P1 IO, nonauthoritative native doubles.

Existing owner/R01 doubles provide admission DATA; they are not native proof.
Measurements are held independently of publication. No installed supplier is
bound here, no engine process runs, and no caller DATA gains execution power.
"""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import lifecycle as p1, owner, files, podman
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
from test_hold1_interface import Subject
import test_install_files as file_fixtures
import test_install_podman as podman_fixtures


class LostResult:
    """Legacy finite source fixture + real consumed P1; no fabricated SQL mandate."""
    def __init__(self, root, state='APPLIED'):
        self.subject = s = Subject(Path(root)/'history')
        s.helper.context(s.data, lambda c:c['effect'].update(resource_kind='file'))
        s.helper.scope(s.data, lambda c:c['effect'].update(resource_kind='file'))
        with s.installed():
            s.reserve(mandate=False); s.consume()
        self.original = s.entry(s.consumed)
        self.subject_key = {k:self.original[0][k] for k in ('identity','effect','request_ref')}
        self.marker = Path(root)/'resource'
        self.marker.write_bytes(b'after' if state=='APPLIED' else b'before')
        self.state = state; self.held = False; self.reads = 0
        self.dispatched = 1; self.after_measure = None; self.raw_edit = None
        self.raw = None
        s.recovery()
        # Independent fresh owner revision is always obtained from real history.
        with s.installed(): s.deadline=s.current().deadline

    def snapshot(self): raise AssertionError('recovery resnapshotted')
    def dispatch(self, effect): raise AssertionError('recovery redispatched')

    @contextmanager
    def measure(self, effect):
        self.reads += 1; self.held = True
        try:
            c=thaw(effect['context'])
            assert {k:c[k] for k in self.subject_key} == self.subject_key
            assert effect['journal_ref'] == freeze(self.subject.consumed)
            observed = self.marker.read_bytes()
            state = self.state if observed == (b'after' if self.state=='APPLIED' else b'before') else 'UNKNOWN'
            if state=='UNKNOWN': answer=('UNKNOWN',None)
            else:
                value={**self.subject_key,'state':'applied_verified' if state=='APPLIED' else 'not_applied_verified',
                       'observed':{'source_fixture':True,'resource_sha256':p1.hashlib.sha256(observed).hexdigest(),
                                   'cleanup':{'remaining':[],'reaped':True,'pipes_closed':True}}}
                if self.raw_edit:self.raw_edit(value)
                self.raw=canonical_bytes(value);answer=(state,self.raw)
            if self.after_measure:self.after_measure()
            yield answer
            assert self.marker.read_bytes()==observed, 'resource changed during P1 ACK'
        finally:self.held=False

    def run(self):
        with self.subject.installed(self):
            return p1.execute_effect(self.subject.data['request_raw'].encode())


class RecoveryTests(unittest.TestCase):
    def ok(self, case):
        try:return case.run()
        except owner.OwnerRefusal as error:self.fail('X1D first original result missing: '+error.detail)

    def once(self, state):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root,state);s=c.subject
            before=s.history.head()[0];revision=s.history.current_revision()
            old=c.original; raw_pre=s.history.read(old[1]['preimages_ref'])
            append=s.history.append; held=[]
            def durable(*args,**kwargs):
                held.append(c.held);return append(*args,**kwargs)
            with patch.object(s.history,'append',durable), \
                    patch.object(p1,'_consume',side_effect=AssertionError('reconsume')):
                first=self.ok(c)
                first_raw=c.raw
                # New history object reads actual durable files; same original
                # native fixture remains held, never reconstructed as authority.
                s.history=p1._History(s.root)
                second=self.ok(c)
            self.assertNotEqual(thaw(first),before)
            self.assertEqual(first,second);self.assertEqual(held,[True])
            self.assertEqual(s.history.current_revision(),revision+1)
            self.assertEqual(len(s.history.events()),3)
            e,o,a=s.entry(first)
            self.assertEqual(s.history.read(e['result_ref']),first_raw)
            self.assertEqual(c.raw,first_raw)
            self.assertEqual(e['identity'],old[0]['identity'])
            self.assertEqual(e['request_ref'],old[0]['request_ref'])
            self.assertEqual(e['effect'],old[0]['effect'])
            self.assertEqual(o['preimages_ref'],old[1]['preimages_ref'])
            self.assertEqual(s.history.read(o['preimages_ref']),raw_pre)
            self.assertEqual(a['attempt']['consumption_sequence'],old[2]['attempt']['consumption_sequence'])
            self.assertEqual(a['attempt']['consume_clock_ref'],old[2]['attempt']['consume_clock_ref'])
            self.assertEqual(a['attempt']['result_ref'],e['result_ref'])
            if state=='NOT_APPLIED':self.assertEqual(a['attempt']['reconciliation_ref'],e['result_ref'])
            self.assertEqual(c.dispatched,1);self.assertEqual(c.reads,2)
            self.assertFalse(c.held)
            self.assertEqual(old[2]['reservation']['expires_at'],a['reservation']['expires_at'])
            self.assertEqual(old[2]['reservation']['expires_monotonic_ns'],a['reservation']['expires_monotonic_ns'])

    def test_X1D_RECOVER_APPLIED(self):self.once('APPLIED')
    def test_X1D_RECOVER_NOT_APPLIED(self):self.once('NOT_APPLIED')

    def test_UNKNOWN_never_publishes_or_replays(self):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root,'UNKNOWN');s=c.subject;before=s.tree()
            with patch.object(s.history,'append',side_effect=AssertionError('UNKNOWN published')), \
                    patch.object(p1,'_consume',side_effect=AssertionError('UNKNOWN reconsumed')):
                with self.assertRaises(owner.OwnerRefusal):c.run()
            self.assertEqual(before,s.tree());self.assertEqual(c.dispatched,1);self.assertFalse(c.held)


class X11JoinTests(unittest.TestCase):
    def join(self, primitive, history):
        cls=getattr(p1,'_X11',None)
        self.assertIsNotNone(cls,'X11D held same-subject P1 projection absent')
        return cls(primitive,history)

    @contextmanager
    def opened(self, root, kind='file'):
        s=Subject(Path(root)/'history')
        s.helper.context(s.data,lambda c:c['effect'].update(resource_kind=kind))
        s.helper.scope(s.data,lambda c:c['effect'].update(resource_kind=kind))
        with s.installed():c=thaw(s.current().context)
        subject={k:c[k] for k in ('identity','effect','request_ref')}
        if kind=='file':
            inputs=file_fixtures.Inputs(Path(root))
            inputs.plan.update(subject=subject,deadline_ns=103000000000)
            inputs.now=101000000000
            with patch.object(files,'_installed_files',return_value=inputs),files._open() as primitive:
                yield s,self.join(primitive,s.history),inputs
        else:
            engine=podman_fixtures.Engine(kind)
            engine.plan.update(subject=subject,deadline_ns=103000000000)
            engine.now=101000000000
            with patch.object(podman,'_installed_engine',return_value=engine),podman._open() as primitive:
                yield s,self.join(primitive,s.history),engine

    def recover(self,s):
        s.recovery()
        with s.installed():s.deadline=s.current().deadline

    def test_real_files_normal_publication_uses_original_snapshot_and_held_measure(self):
        with tempfile.TemporaryDirectory() as root, self.opened(root) as (s,j,inputs):
            with s.installed(j):result=p1.execute_effect(s.data['request_raw'].encode())
            e,o,a=s.entry(result)
            self.assertEqual((inputs.root/'published').read_bytes(),b'original member\n')
            pre=json.loads(s.history.read(o['preimages_ref']))
            self.assertEqual(set(pre['originals']),{'file'})
            self.assertEqual(s.history.read(pre['originals']['file']),canonical_bytes(None))
            self.assertEqual(a['attempt']['dispatch_state'],'applied_verified')
            self.assertTrue(inputs.held)
            self.assertEqual(a['attempt'].get('sql_mandate_ref'),None)

    def lost(self,root,kind,state):
        with self.opened(root,kind) as (s,j,inputs):
            with s.installed(j):
                s.before=j.snapshot();s.reserve(mandate=False);s.consume()
                e,o,_=s.entry(s.consumed)
                effect=freeze({'context':json.loads(s.history.read(o['context_ref'])),
                    'journal_ref':s.consumed,'preimages_ref':o['preimages_ref'],'deadline':s.deadline})
                if state=='NOT_APPLIED':
                    # Failure before any created file; actual complete preimage
                    # and no writers/stage are measured, not a reply inference.
                    if kind=='file':
                        with patch.object(j.primitive,'_build',side_effect=OSError('before first write')):j.dispatch(effect)
                    else:
                        inputs.on_run=lambda *args:None
                        j.dispatch(effect)
                else:j.dispatch(effect)
            self.recover(s)
            with s.installed(j):
                try:first=p1.execute_effect(s.data['request_raw'].encode());again=p1.execute_effect(s.data['request_raw'].encode())
                except owner.OwnerRefusal as error:self.fail('X11D recovery not joined: '+error.detail)
            self.assertEqual(first,again);self.assertEqual(len(s.history.events()),3)
            e,_,a=s.entry(first);raw=json.loads(s.history.read(e['result_ref']))
            self.assertEqual(raw['state'],'applied_verified' if state=='APPLIED' else 'not_applied_verified')
            self.assertEqual(raw['identity'],e['identity'])
            self.assertEqual(a['attempt']['consumption_sequence'],1)
            if kind!='file':self.assertEqual(len(inputs.calls),1)
            return raw

    def test_files_lost_APPLIED_and_NOT_APPLIED_recover_once(self):
        for state in ('APPLIED','NOT_APPLIED'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as root:self.lost(root,'file',state)

    def test_podman_lost_APPLIED_and_NOT_APPLIED_recover_once(self):
        for state in ('APPLIED','NOT_APPLIED'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as root:self.lost(root,'volume',state)
