"""S3b offline Core-boundary controls; no PostgreSQL durability/native credit."""
from contextlib import contextmanager
from types import SimpleNamespace
import traceback
import unittest
from unittest.mock import patch

import psycopg
from cortex_core.auth import AuthError
from cortex_core.modules.consumer import ModuleConsumer, ConsumerError
from cortex_core.modules.vector import projection
import test_c09_qdrant as fixtures

PRIVATE = 'SYNTHETIC-PRIVATE-CORE-FAILURE'


class CoreBoundaryTests(unittest.TestCase):
    def port(self, *, external=True, fault=None, after_checkpoint=None):
        x = fixtures.S3Tests(); binding = x.binding(); self.event = x.event()
        point = projection.build_point(binding, self.event, x.vector())
        self.receipt = projection.make_receipt(binding, self.event, point, point, 7)
        self.effects = []; self.record_attempts = []; self.transaction_exits = []
        def apply(*args):
            self.effects.append(args)
            return self.receipt if external else {k:self.receipt[k] for k in
                ('event_id','aggregate_id','revision','tombstone','payload_sha256','generation','stale')}
        def execute(sql, args):
            if 'c07_record' in sql:self.record_attempts.append(args)
            if fault == 'execute': raise psycopg.OperationalError(PRIVATE)
            def fetchone():
                if fault == 'fetch': raise psycopg.OperationalError(PRIVATE)
                if 'advisory' in sql:return (True,)
                if 'c07_poisoned' in sql:return (False,)
                return ({'generation':1,'state':'active','scan_cursor':0,'applied_cursor':0,'floor':0},)
            return SimpleNamespace(fetchone=fetchone)
        port = ModuleConsumer(SimpleNamespace(execute=execute),b'synthetic',binding.installation_id,
            binding.project_id,binding.module_id,SimpleNamespace(apply=apply),
            manifest=dict(module_id=binding.module_id,protocol_version=2 if external else 1,
                          schema_version=1,aggregate_kinds=['memory'],sink='qdrant' if external else 'postgres'),
            external_binding=binding if external else None,after_checkpoint=after_checkpoint)
        @contextmanager
        def authorized(*args):
            if fault == 'auth':raise AuthError(PRIVATE)
            if fault == 'begin':raise psycopg.OperationalError(PRIVATE)
            try:yield
            except BaseException:
                self.transaction_exits.append('rollback');raise
            else:
                self.transaction_exits.append('exit_attempt')
                if fault == 'commit':raise psycopg.OperationalError(PRIVATE)
        self.addCleanup(patch.stopall)
        patch('cortex_core.modules.consumer.authorized',authorized).start()
        return port

    def assert_scrubbed(self, action):
        caught=None;result=None
        try:result=action()
        except Exception as error:caught=error
        self.assertIsInstance(caught,ConsumerError,'a failed Core boundary must not return success')
        self.assertEqual(caught.code,'capability_unavailable')
        self.assertIsNone(caught.__cause__)
        self.assertTrue(caught.__suppress_context__)
        self.assertNotIn(PRIVATE,''.join(traceback.format_exception(caught)))
        self.assertIsNone(result)

    def test_external_core_call_entry_execute_fetch_and_commit_fail_scrubbed(self):
        for fault in ('auth','begin','execute','fetch','commit'):
            with self.subTest(fault=fault):
                port=self.port(fault=fault)
                self.assert_scrubbed(port.status)
                self.assertEqual(self.effects,[])
                patch.stopall()

    def test_external_receipt_record_commit_failure_does_not_return_success_or_run_callback(self):
        callbacks=[];port=self.port(fault='commit',after_checkpoint=lambda:callbacks.append(True))
        original=port._call
        port._call=lambda signature,args=():False if signature.startswith('c07_poisoned') else original(signature,args)
        self.assert_scrubbed(lambda:port._apply_event({'generation':1},self.event))
        self.assertEqual(len(self.effects),1)
        self.assertEqual(len(self.record_attempts),1)
        self.assertEqual(self.record_attempts[0][4],'applied')
        import json
        self.assertEqual(json.loads(self.record_attempts[0][-1]),self.receipt)
        self.assertEqual(callbacks,[])
        self.assertEqual(self.transaction_exits,['exit_attempt'])

    def test_external_invalid_receipt_is_scrubbed_and_never_reaches_record(self):
        port=self.port();port.sink.apply=lambda *args:dict(self.receipt,proof='acknowledged')
        self.assert_scrubbed(lambda:port._apply_event({'generation':1},self.event))
        self.assertEqual(self.record_attempts,[])

    def test_external_post_commit_callback_failure_is_scrubbed_without_second_record(self):
        def fail():raise RuntimeError(PRIVATE)
        port=self.port(after_checkpoint=fail)
        self.assert_scrubbed(lambda:port._apply_event({'generation':1},self.event))
        self.assertEqual(len(self.record_attempts),1)
        self.assertEqual(len(self.effects),1)

    def test_external_advisory_lock_driver_failure_is_scrubbed_before_effect(self):
        port=self.port(fault='execute')
        def acquire():
            with port.hold_lock():self.fail('failed lock must not yield')
        self.assert_scrubbed(acquire)
        self.assertEqual(self.effects,[])

    def test_success_requires_record_transaction_exit_before_callback(self):
        seen=[];port=self.port(after_checkpoint=lambda:seen.append(tuple(self.transaction_exits)))
        self.assertTrue(port._apply_event({'generation':1},self.event))
        self.assertEqual(len(self.effects),1)
        self.assertEqual(len(self.record_attempts),1)
        self.assertEqual(seen,[('exit_attempt','exit_attempt')])

    def test_v1_core_error_contract_and_cause_are_preserved(self):
        port=self.port(external=False,fault='execute')
        with self.assertRaises(ConsumerError) as caught:port.status()
        self.assertEqual(caught.exception.code,'core_unavailable')
        self.assertIsInstance(caught.exception.__cause__,psycopg.OperationalError)

    def test_v1_invalid_receipt_and_callback_contract_are_preserved(self):
        port=self.port(external=False);port.sink.apply=lambda *a:{}
        with self.assertRaises(ConsumerError) as caught:port._apply_event({'generation':1},self.event)
        self.assertEqual(caught.exception.code,'invalid_target_receipt')
        self.assertEqual(self.record_attempts,[])
        patch.stopall()
        def fail():raise RuntimeError(PRIVATE)
        port=self.port(external=False,after_checkpoint=fail)
        with self.assertRaises(ConsumerError) as caught:port._apply_event({'generation':1},self.event)
        self.assertEqual(caught.exception.code,'after_checkpoint')
        self.assertIsInstance(caught.exception.__cause__,RuntimeError)
        self.assertEqual(len(self.record_attempts),1)
