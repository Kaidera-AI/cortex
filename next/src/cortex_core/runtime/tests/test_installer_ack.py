"""Shared ACK/Core resource SOURCE regressions with named NONSECRET IO doubles.

Actual decoder/vault/record/ACK/resource implementations; no replacement seed,
Nix sequence, migration engine or actual cryptography/FD/DB/network effects.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cortex_core.runtime import custody, custody_fs, operator, resources
from cortex_core.runtime.signed_member import Refusal
from cortex_core.runtime.tests.test_installer_custody import InstallerCore1Tests, PURPOSES
from cortex_core.runtime.tests.test_custody import intent, receipt, SECRET


class NonsecretDriverConnection:
    """Connector IO only: statement capture, source transaction/cleanup events."""
    def __init__(self, role, events, close_error=None):
        self.role, self.events, self.close_error = role, events, close_error
        self.closed = False
        self.queries = []
        events.append(('connect-double', role))

    @contextmanager
    def transaction(self):
        self.events.append(('begin-double',self.role))
        try: yield
        except BaseException:
            self.events.append(('rollback-double',self.role))
            raise
        else: self.events.append(('commit-double',self.role))

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if 'set_config' not in sql and not sql.startswith('SET TRANSACTION'):
            raise RuntimeError('NONSECRET driver boundary: no database')
        return SimpleNamespace(fetchone=lambda: (None,None))

    def close(self):
        self.closed=True
        self.events.append(('close-double',self.role))
        if self.close_error: raise self.close_error


class InstallerAckTests(unittest.TestCase):
    setUp = InstallerCore1Tests.setUp
    config = InstallerCore1Tests.config
    opened = InstallerCore1Tests.opened

    def setUp(self):
        InstallerCore1Tests.setUp(self)
        # Deterministic original clock, no product timeout-driver replacement.
        self.stack.enter_context(patch.object(resources,'time', SimpleNamespace(monotonic=lambda:self.adapters.clock)))
        # Only certificate/file IO input is replaced, not connector admission.
        self.stack.enter_context(patch.object(resources,'_certificate', return_value=('/NONSECRET/ca',b'NONSECRET-ca-digest')))

    def vault(self, mode='seed'):
        return custody.open_custody(self.config(mode),unlock_fd=99,key_id='NONSECRET-id')

    def prepared(self, vault, *, committed=True, transform=None):
        value = intent(self.value)
        if transform: transform(value)
        result = receipt(value)
        with vault.seed_record() as record:
            record.create(intent=value,credential=SECRET)
            if committed: record.commit(receipt=result)
        return value,result

    def ack(self, vault, result, operation_key='test-operation', deadline=200.0):
        return operator._reload_seed_receipt(vault._config,custody=vault,
                operation_key=operation_key,deadline=deadline,result=result)

    def test_shared_exact_ack_and_lost_ack_replay_are_read_only(self):
        with self.vault() as vault:
            value,result = self.prepared(vault)
            before = dict(self.adapters.records)
            writes = self.adapters.events.count('write-double')
            for _ in range(2):
                actual = self.ack(vault, result)
                self.assertEqual(actual, result)
                self.assertIsNot(actual,result)
            self.assertEqual(self.adapters.records, before)
            self.assertEqual(self.adapters.events.count('write-double'),writes)

    def test_missing_prepared_and_wrong_operation_never_ack(self):
        with self.vault() as vault:
            with self.assertRaisesRegex(Refusal,'seed_receipt'): self.ack(vault,{})
            _,result = self.prepared(vault,committed=False)
            with self.assertRaisesRegex(Refusal,'seed_receipt'): self.ack(vault,result)
            with vault.seed_record() as record: record.load();record.commit(receipt=result)
            with self.assertRaisesRegex(Refusal,'seed_receipt'):
                self.ack(vault,result,operation_key='OTHER-NONSECRET-operation')

    def test_all_eight_fields_removal_addition_and_bool_are_rejected(self):
        with self.vault() as vault:
            _,result = self.prepared(vault)
            variants = [None,True,{**result,'extra':True}]
            for key in result:
                missing=dict(result);missing.pop(key);variants.append(missing)
                variants.append({**result,key:'OTHER-NONSECRET-value'})
            for variant in variants:
                with self.subTest(value=variant), self.assertRaises(Refusal): self.ack(vault,variant)

    def test_committed_but_noncanonical_or_zero_identifiers_refuse(self):
        for key in ('principal_id','credential_id'):
            for invalid in ('00000000-0000-0000-0000-000000000000',
                            '10000000000000000000000000000004','NOT-A-UUID',True):
                self.adapters.records.clear()
                with self.subTest(key=key,invalid=invalid), self.vault() as vault:
                    _,result=self.prepared(vault,transform=lambda v:v.update({key:invalid}))
                    with self.assertRaisesRegex(Refusal,'seed_receipt'): self.ack(vault,result)

    def test_tampered_receipt_cipher_and_wrong_credential_hash_refuse(self):
        with self.vault() as vault:
            _,result=self.prepared(vault)
            name=(vault._config.vault_root,'client')
            original=self.adapters.records[name]
            self.adapters.records[name]=b'NONSECRET-corruption'
            with self.assertRaises(Refusal): self.ack(vault,result)
            self.adapters.records[name]=original
            envelope=json.loads(original)
            token=custody._unbase(envelope['ciphertext'],maximum=48000)
            nonce,plain,aad=self.adapters.payloads[token]
            payload=json.loads(plain)
            payload['receipt']['credential_sha256']='0'*64
            self.adapters.payloads[token]=(nonce,json.dumps(payload).encode(),aad)
            with self.assertRaisesRegex(Refusal,'seed_receipt'):
                self.ack(vault,payload['receipt'])

    def test_readonly_accessor_has_no_mutating_handle_and_lock_cleanup_failure_is_red(self):
        with self.vault() as vault:
            _,result=self.prepared(vault)
            with vault._read_seed_receipt(200.0) as material:
                self.assertIs(type(material),custody.SeedMaterial)
                for method in ('create','commit','load','write'):
                    self.assertFalse(hasattr(material,method))
            original=self.adapters.lock
            @contextmanager
            def failing_lock_exit(directory,name):
                with original(directory,name): yield
                raise Refusal('NONSECRET_lock_cleanup')
            with patch.object(custody_fs,'record_lock',failing_lock_exit):
                with self.assertRaisesRegex(Refusal,'NONSECRET_lock_cleanup'): self.ack(vault,result)

    def test_expiry_during_record_cleanup_cannot_ack(self):
        with self.vault() as vault:
            _,result=self.prepared(vault)
            original=self.adapters.lock
            @contextmanager
            def expires_at_exit(directory,name):
                with original(directory,name): yield
                self.adapters.clock=200.0
            with patch.object(custody_fs,'record_lock',expires_at_exit):
                with self.assertRaisesRegex(Refusal,'resources_deadline'): self.ack(vault,result)

    def test_install_ack_in_import_refuses_before_record_and_has_no_close_boolean(self):
        with self.opened() as owner:
            before=list(self.adapters.events)
            with self.assertRaises(Refusal):
                operator._reload_seed_receipt(owner._config,custody=owner._vault,
                        operation_key='test-operation',deadline=owner._deadline,result=True)
            with self.assertRaises(Refusal):
                with owner._vault._read_seed_receipt(owner._deadline): self.fail('IMPORT ACK')
            with self.assertRaises(TypeError): owner.run('test-operation',seed_ack=True)
            self.assertEqual(before,self.adapters.events)

    def driver(self, **kwargs):
        connection=NonsecretDriverConnection(kwargs['user'],self.driver_events)
        self.connections.append(connection)
        self.driver_calls.append(kwargs)
        return connection

    @contextmanager
    def driver_doubles(self):
        self.driver_events=[];self.connections=[];self.driver_calls=[]
        # Actual _connect, private_driver_logs, deadline, resources, transaction
        # and close run. Only the external driver's connect is synthetic IO.
        with patch.object(resources,'_driver_connect',self.driver): yield

    def test_ordinary_seed_resources_use_actual_connector_and_reverse_close(self):
        with self.vault() as vault, self.driver_doubles():
            for purpose in ('bootstrap','installer_enrollment','api_request'):
                vault.import_credential(purpose,b'NONSECRET-'+purpose.encode())
            with resources.open_seed_resources(vault._config,custody=vault,deadline=200.0) as owned:
                self.assertIs(type(owned),resources.SeedResources)
                self.assertIs(resources.current_seed_resources(vault._config,200.0),owned)
                self.assertIs(owned.custody,vault)
                for purpose in ('bootstrap','installer_enrollment','api_request'):
                    with owned.transaction(purpose): pass
                self.assertEqual(len(self.driver_calls),3)
                self.assertTrue(all(call['sslmode']=='verify-full' for call in self.driver_calls))
            expected=[('close-double',vault._config.roles[p].login) for p in
                      ('api_request','installer_enrollment','bootstrap')]
            self.assertEqual([e for e in self.driver_events if e[0]=='close-double'],expected)
            self.assertTrue(all(c.closed for c in self.connections))
            with self.assertRaisesRegex(Refusal,'resources_closed'): owned.check_deadline()
            self.assertIsNone(resources._SEED.get())

    def test_ack_while_actual_seed_sql_scope_is_open_refuses_before_record(self):
        with self.vault() as vault, self.driver_doubles():
            _, result = self.prepared(vault)
            for purpose in ('bootstrap','installer_enrollment','api_request'):
                vault.import_credential(purpose,b'NONSECRET-'+purpose.encode())
            with resources.open_seed_resources(vault._config,custody=vault,deadline=200.0):
                before=list(self.adapters.events)
                with self.assertRaisesRegex(Refusal,'seed_receipt'): self.ack(vault,result)
                self.assertEqual(before,self.adapters.events)
            self.assertEqual(self.ack(vault,result),result)

    def test_partial_connector_failure_closes_owned_session_and_resets_seed(self):
        with self.vault() as vault,self.driver_doubles():
            for purpose in ('bootstrap','installer_enrollment','api_request'):
                vault.import_credential(purpose,b'NONSECRET-'+purpose.encode())
            actual=self.driver
            def fails_second(**kwargs):
                if self.connections: raise RuntimeError('NONSECRET connector failure')
                return actual(**kwargs)
            with patch.object(resources,'_driver_connect',fails_second):
                with self.assertRaisesRegex(Refusal,'resources_unavailable'):
                    with resources.open_seed_resources(vault._config,custody=vault,deadline=200.0):
                        self.fail('partial open')
            self.assertTrue(self.connections[0].closed)
            self.assertIsNone(resources._SEED.get())

    def test_interrupt_after_connector_returns_closes_not_yet_yielded_connection(self):
        with self.vault() as vault, self.driver_doubles():
            vault.import_credential('bootstrap',b'NONSECRET-bootstrap')
            interrupted=[]
            def clock_input():
                if self.connections and not interrupted:
                    interrupted.append(True)
                    raise KeyboardInterrupt()
                return 100.0
            with patch.object(resources,'time',SimpleNamespace(monotonic=clock_input)):
                with self.assertRaises(KeyboardInterrupt):
                    with resources.open_bootstrap_resources(vault._config,custody=vault,deadline=200.0):
                        self.fail('interrupted connector admitted')
            self.assertEqual(len(self.connections),1)
            self.assertTrue(self.connections[0].closed)

    def test_reverse_cleanup_attempts_every_session_even_on_interrupt(self):
        connections=[NonsecretDriverConnection(str(i),[],KeyboardInterrupt() if i==1 else None) for i in range(3)]
        self.assertTrue(resources._close(dict(enumerate(connections)),deadline=200.0))
        self.assertTrue(all(c.closed for c in connections))

    def test_cleanup_failure_never_counts_as_success_and_retained_handle_is_closed(self):
        with self.vault() as vault,self.driver_doubles():
            vault.import_credential('bootstrap',b'NONSECRET-bootstrap')
            with self.assertRaisesRegex(Refusal,'resources_cleanup'):
                with resources.open_bootstrap_resources(vault._config,custody=vault,deadline=200.0) as owned:
                    self.connections[0].close_error=RuntimeError('NONSECRET close')
            with self.assertRaisesRegex(Refusal,'resources_closed'): owned.check_deadline()


if __name__ == '__main__': unittest.main()
