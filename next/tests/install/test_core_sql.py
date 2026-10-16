"""X4 source-only original P1 phase composition. All native observations are DATA doubles."""
import copy
import importlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import lifecycle, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
from test_hold1_interface import Subject

PHASES = ('init', 'ready', 'bootstrap', 'migrate', 'prepare', 'bind', 'close')


class CoreSQL(unittest.TestCase):
    def api(self):
        # New-surface RED is a BODY assertion, explicitly not a semantic old-code kill.
        self.assertTrue((Path(lifecycle.__file__).parent/'core_sql.py').is_file(), 'X4_SURFACE_ABSENT')
        return importlib.import_module('cortex_core.install.core_sql')

    def fixture(self, root):
        s=Subject(root)
        c=thaw(s.proposal.context)
        schema=json.loads(s.data['runtime_lock_raw'])['current_schema']
        selection={'manifest_sha256':schema['manifest_sha256'],'through':schema['migrations'][-1]['id'],
                   'migrations':[[r['id'],r['sha256']] for r in schema['migrations']]}
        s.selection=s.history.retain(canonical_bytes(selection))
        s.windows=s.history.retain(canonical_bytes({'operation':lifecycle._operation(c['identity']),
            'owner_epoch':c['owner_epoch'],'E':s.deadline.expires_monotonic_ns,
            'init_start':101000000000,'finite_start':101000000000}))
        s.previous=[];s.selection_data=selection;s.damage=None;s.measure_damage=None
        return s

    def select(self,s,phase):
        q=json.loads(s.data['request_raw']);q['identity']={**q['identity'],
            'request_id':'x4-request-'+phase,'attempt_id':'x4-attempt-'+phase}
        raw=canonical_bytes(q);c=json.loads(s.data['context_raw']);ref={**c['request_ref'],
            'bytes':len(raw),'sha256':__import__('hashlib').sha256(raw).hexdigest()}
        s.data['request_raw']=raw.decode();s.data['originals'][ref['path']]=raw.decode()
        effect={**c['effect'],'effect_id':'x4-'+phase}
        s.helper.context(s.data,lambda v:v.update(identity=q['identity'],effect=effect,request_ref=ref,nonce='x4-nonce-'+phase))
        s.helper.scope(s.data,lambda v:v.update(identity=q['identity'],effect=effect))
        s.phase=phase

    def observation(self,s,context):
        c=thaw(context['context'])
        v={'identity':c['identity'],'phase':s.phase,'selection_ref':s.selection,'windows_ref':s.windows,
           'root':{'held':True,'owned':True,'empty':True,'uid':1001,'server_uid':1001},
           'fresh':True,'sql_ready':True,'probe_closed':True,'bootstrap_closed':True,
           'finite_phase':'bootstrap' if s.phase in ('init','ready','bootstrap') else 'migrator',
           'committed':True,'ledger':copy.deepcopy(s.selection_data['migrations']),
           'roles_bound':True,'acl_exact':True,'finite_disabled':True,'auth_fenced':True,
           'unresolved':{'sessions':[],'recovery':[],'carriers':[],'children':[],'authentication':[]},
           'old_generation_absent':True,'applied_cleaned':True}
        if s.damage:s.damage(v)
        return canonical_bytes(v)

    def primitive(self,s,phase):
        self.select(s,phase)
        test=self
        class Inner:
            sql_mandate_raw=None
            def snapshot(self):return s.before
            def dispatch(self,context):s.effects+=1
            def measure(self,context):
                if s.measure_damage:s.measure_damage()
                c=thaw(context['context'])
                return canonical_bytes({**{k:c[k] for k in ('identity','effect','request_ref')},
                    'state':'applied_verified','observed':{'adapter':'source_DATA_only','marker':s.effects}})
        return self.api()._CoreSQL(history=s.history,identity=json.loads(s.data['request_raw'])['identity'],
            phase=phase,primitive=Inner(),observe=lambda ctx:test.observation(s,ctx),
            selection_ref=s.selection,windows_ref=s.windows,previous_refs=tuple(s.previous))

    def run_phase(self,s,phase):
        p=self.primitive(s,phase)
        # Trusted owner clock remains DATA; don't turn CPU/filesystem scheduling into expiry.
        with s.installed(p),patch.object(lifecycle.time,'monotonic_ns',return_value=101000000000):
            ref=lifecycle.execute_effect(s.data['request_raw'].encode())
        s.previous.append(thaw(ref));return ref

    def prefix(self,s,phase):
        for p in PHASES[:PHASES.index(phase)]:self.run_phase(s,p)

    def rejected(self,s,phase,damage,*,effects=0):
        self.prefix(s,phase);s.damage=damage;before=s.effects
        with self.assertRaises(owner.OwnerRefusal):self.run_phase(s,phase)
        self.assertEqual(s.effects-before,effects)
        self.assertIsNone(s.history.head()[1]['result_ref'])

    def test_X4_ROOT_substitution_uid_and_freshness_refuse_before_init(self):
        for fault in ('held','owned','empty','uid','fresh'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as root:
                s=self.fixture(root)
                def damage(v):
                    if fault=='fresh':v['fresh']=False
                    elif fault=='uid':v['root']['uid']=0
                    else:v['root'][fault]=False
                self.rejected(s,'init',damage)

    def test_X4_SQLREADY_process_or_port_and_unclosed_probe_do_not_advance(self):
        for field in ('sql_ready','probe_closed'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                s=self.fixture(root);self.rejected(s,'ready',lambda v:v.update({field:False}))

    def test_X4_PHASE_bootstrap_survivor_or_service_migrator_view_refuses(self):
        for field,value in (('bootstrap_closed',False),('finite_phase','runtime')):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                s=self.fixture(root);self.rejected(s,'migrate',lambda v:v.update({field:value}))

    def test_X4_TXN_committed_selected_ledger_and_exact_role_acl_required(self):
        for phase,field,value in (('migrate','committed',False),('migrate','ledger',[]),
                                  ('bind','roles_bound',False),('bind','acl_exact',False)):
            with self.subTest(phase=phase,field=field),tempfile.TemporaryDirectory() as root:
                s=self.fixture(root);self.rejected(s,phase,lambda v:v.update({field:value}),effects=1)

    def test_X4_BUDGET_original_window_not_renewed_after_observer_wait(self):
        with tempfile.TemporaryDirectory() as root:
            s=self.fixture(root);p=self.primitive(s,'init');seen=[]
            original=p.observe
            def delayed(ctx):
                value=original(ctx);seen.append(True);s.expired=True;return value
            p.observe=delayed
            with s.installed(p),self.assertRaises(owner.OwnerRefusal):lifecycle.execute_effect(s.data['request_raw'].encode())
            self.assertTrue(seen);self.assertEqual(s.effects,0)

    def test_X4_COMMIT_lost_result_keeps_consumed_subject_and_never_replays(self):
        with tempfile.TemporaryDirectory() as root:
            s=self.fixture(root);self.prefix(s,'migrate');s.measure_damage=lambda:(_ for _ in ()).throw(RuntimeError('lost SQL reply'))
            with self.assertRaises(owner.OwnerRefusal):self.run_phase(s,'migrate')
            count=s.effects;head=s.history.head()[0];self.assertIsNone(s.history.head()[1]['result_ref'])
            s.measure_damage=None
            with self.assertRaises(owner.OwnerRefusal):self.run_phase(s,'migrate')
            self.assertEqual(s.effects,count);self.assertEqual(s.history.head()[0],head)

    def test_X4_CLOSE_any_finite_or_recovery_carrier_auth_survivor_blocks(self):
        for kind in ('sessions','recovery','carriers','children','authentication'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as root:
                s=self.fixture(root)
                self.rejected(s,'close',lambda v:v['unresolved'][kind].append({'original_lifetime':kind}),effects=1)

    def test_X4_TUPLE_RESTART_mixed_selection_and_unknown_ordinary_refuse(self):
        with tempfile.TemporaryDirectory() as root:
            s=self.fixture(root);self.rejected(s,'init',lambda v:v.update(selection_ref=s.windows))
        with tempfile.TemporaryDirectory() as root:
            s=self.fixture(root)
            for phase in PHASES:self.run_phase(s,phase)
            before=s.effects;s.damage=lambda v:v.update(old_generation_absent=False)
            with self.assertRaises(owner.OwnerRefusal):self.run_phase(s,'ordinary')
            self.assertEqual(s.effects,before)

    def test_positive_original_chain_and_ordinary_restart_zero_effects(self):
        with tempfile.TemporaryDirectory() as root:
            s=self.fixture(root)
            for phase in PHASES:self.run_phase(s,phase)
            self.assertEqual(s.effects,len(PHASES))
            event,obs=lifecycle._load_entry(s.history,s.previous[-1]);result=json.loads(s.history.read(event['result_ref']))
            original=result['observed']['core_sql']
            self.assertEqual(original['phase'],'close');self.assertEqual(original['selection_ref'],s.selection)
            self.assertEqual(original['windows_ref'],s.windows);self.assertEqual(len(original['previous_refs']),6)
            self.assertEqual(json.loads(s.history.read(original['observation_ref']))['unresolved'],
                {'sessions':[],'recovery':[],'carriers':[],'children':[],'authentication':[]})
            self.run_phase(s,'ordinary');self.assertEqual(s.effects,len(PHASES))

    def test_phase_ref_must_be_original_result_same_operation_and_sequence(self):
        with tempfile.TemporaryDirectory() as root:
            s=self.fixture(root);self.run_phase(s,'init');s.previous=[s.selection]
            with self.assertRaises(owner.OwnerRefusal):self.run_phase(s,'ready')
            self.assertEqual(s.effects,1)
