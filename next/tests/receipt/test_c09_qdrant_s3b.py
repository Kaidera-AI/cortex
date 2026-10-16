"""Core boundary preservation controls: recorders prove no database durability.

Cox's accepted C04/C11 precedence is retained. Engine failures alone map to
capability_unavailable. The earlier broad mapping experiment is withdrawn.
"""
from contextlib import contextmanager
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

import psycopg
from cortex_core.auth import AuthError
from cortex_core.modules.consumer import ModuleConsumer, ConsumerError
from cortex_core.outbox import OutboxError
from cortex_core.modules.vector import projection
import test_c09_qdrant as fixtures


class CoreBoundaryTests(unittest.TestCase):
    def port(self, *, external=True, fault=None, after_checkpoint=None):
        x=fixtures.S3Tests();binding=x.binding();self.event=x.event()
        point=projection.build_point(binding,self.event,x.vector())
        self.receipt=projection.make_receipt(binding,self.event,point,point,7)
        self.effects=[];self.record_attempts=[];self.transaction_exits=[]
        def apply(*args):
            self.effects.append(args)
            return self.receipt if external else {k:self.receipt[k] for k in
                ('event_id','aggregate_id','revision','tombstone','payload_sha256','generation','stale')}
        def execute(sql,args):
            if 'c07_record' in sql:self.record_attempts.append(args)
            if fault=='execute':raise psycopg.OperationalError('synthetic_driver_refusal')
            def fetchone():
                if fault=='fetch':raise psycopg.OperationalError('synthetic_driver_refusal')
                if 'advisory' in sql:return (True,)
                if 'c07_poisoned' in sql:return (False,)
                return ({'generation':1,'state':'active','scan_cursor':0,'applied_cursor':0,'floor':0},)
            return SimpleNamespace(fetchone=fetchone)
        port=ModuleConsumer(SimpleNamespace(execute=execute),b'synthetic',binding.installation_id,
            binding.project_id,binding.module_id,SimpleNamespace(apply=apply),
            manifest=dict(module_id=binding.module_id,protocol_version=2 if external else 1,
                          schema_version=1,aggregate_kinds=['memory'],sink='qdrant' if external else 'postgres'),
            external_binding=binding if external else None,after_checkpoint=after_checkpoint)
        @contextmanager
        def authorized(*args):
            if fault=='auth':raise AuthError('forbidden')
            if fault=='begin':raise psycopg.OperationalError('synthetic_begin_refusal')
            try:yield
            except BaseException:
                self.transaction_exits.append('rollback');raise
            else:
                self.transaction_exits.append('exit_attempt')
                if fault=='commit':raise psycopg.OperationalError('synthetic_commit_refusal')
        self.addCleanup(patch.stopall)
        patch('cortex_core.modules.consumer.authorized',authorized).start()
        return port

    def test_v2_core_call_failure_preserves_core_error_and_never_starts_effect(self):
        for fault in ('auth','begin','execute','fetch','commit'):
            with self.subTest(fault=fault):
                port=self.port(fault=fault)
                with self.assertRaises(ConsumerError) as caught:port.status()
                self.assertEqual(caught.exception.code,'core_unavailable')
                self.assertEqual(self.effects,[])
                patch.stopall()

    def test_record_commit_failure_does_not_return_success_or_run_callback(self):
        callbacks=[];port=self.port(fault='commit',after_checkpoint=lambda:callbacks.append(True))
        original=port._call
        port._call=lambda signature,args=():False if signature.startswith('c07_poisoned') else original(signature,args)
        with self.assertRaises(ConsumerError) as caught:port._apply_event({'generation':1},self.event)
        self.assertEqual(caught.exception.code,'core_unavailable')
        self.assertEqual(len(self.effects),1);self.assertEqual(len(self.record_attempts),1)
        self.assertEqual(self.record_attempts[0][4],'applied')
        self.assertEqual(json.loads(self.record_attempts[0][-1]),self.receipt)
        self.assertEqual(callbacks,[]);self.assertEqual(self.transaction_exits,['exit_attempt'])

    def test_invalid_external_receipt_preserves_rejection_and_never_reaches_record(self):
        port=self.port();port.sink.apply=lambda *args:dict(self.receipt,proof='acknowledged')
        with self.assertRaises(ConsumerError) as caught:port._apply_event({'generation':1},self.event)
        self.assertEqual(caught.exception.code,'invalid_target_receipt')
        self.assertEqual(self.record_attempts,[])

    def test_success_requires_record_transaction_exit_before_callback(self):
        seen=[];port=self.port(after_checkpoint=lambda:seen.append(tuple(self.transaction_exits)))
        self.assertTrue(port._apply_event({'generation':1},self.event))
        self.assertEqual(len(self.effects),1);self.assertEqual(len(self.record_attempts),1)
        self.assertEqual(seen,[('exit_attempt','exit_attempt')])

    def test_post_commit_callback_failure_has_no_second_record(self):
        def fail():raise RuntimeError('synthetic_post_commit_stop')
        port=self.port(after_checkpoint=fail)
        with self.assertRaises(ConsumerError) as caught:port._apply_event({'generation':1},self.event)
        self.assertEqual(caught.exception.code,'after_checkpoint')
        self.assertEqual(len(self.record_attempts),1);self.assertEqual(len(self.effects),1)

    def test_feed_authority_precedence_and_outbox_error_remain_distinct(self):
        for external in (True,False):
            for error_type,code in ((AuthError,'forbidden'),(OutboxError,'core_unavailable')):
                with self.subTest(external=external,error_type=error_type.__name__):
                    port=self.port(external=external)
                    def fail(**kwargs):raise error_type(code)
                    port.feed=SimpleNamespace(page=fail)
                    expected=AuthError if error_type is AuthError else ConsumerError
                    with self.assertRaises(expected) as caught:port.cycle()
                    self.assertEqual(caught.exception.code,code)
                    self.assertEqual(self.effects,[]);self.assertEqual(self.record_attempts,[])
                    patch.stopall()

    def test_v1_core_and_invalid_receipt_contract_are_preserved(self):
        port=self.port(external=False,fault='execute')
        with self.assertRaises(ConsumerError) as caught:port.status()
        self.assertEqual(caught.exception.code,'core_unavailable')
        self.assertIsInstance(caught.exception.__cause__,psycopg.OperationalError)
        patch.stopall();port=self.port(external=False);port.sink.apply=lambda *a:{}
        with self.assertRaises(ConsumerError) as caught:port._apply_event({'generation':1},self.event)
        self.assertEqual(caught.exception.code,'invalid_target_receipt');self.assertEqual(self.record_attempts,[])
