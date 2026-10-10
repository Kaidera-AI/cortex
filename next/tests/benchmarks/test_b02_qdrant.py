"""Frozen isolated Qdrant and persistent-client lifecycle controls."""
import asyncio
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from vector_baseline import benchmark


class Engine:
    def __init__(self, fault=None):
        self.resources = {k: {} for k in ('container', 'network', 'secret')}
        self.commands, self.fault, self.fail_remove = [], fault, False
    def __call__(self, args, **kwargs):
        self.commands.append((list(args), bool(kwargs.get('input'))))
        if args[:2] == ['network', 'create'] or args[:2] == ['secret', 'create'] or args[0] == 'create':
            kind = 'container' if args[0] == 'create' else args[0]
            name = args[args.index('--name')+1] if kind == 'container' else args[-2] if kind == 'secret' else args[-1]
            if name in self.resources[kind]:
                raise RuntimeError('collision')
            labels = dict(args[i+1].split('=', 1) for i, a in enumerate(args) if a == '--label')
            self.resources[kind][name] = {'id': kind+'-'+name, 'labels': labels}
            if self.fault == kind:
                raise RuntimeError('lost create acknowledgement')
            return kind+'-'+name
        if args[0] == 'start':
            raise RuntimeError('controlled start failure')
        if args[0] == 'ps':
            return '\n'.join(self.resources['container'])
        if args[:2] in (['network', 'ls'], ['secret', 'ls']):
            return '\n'.join(self.resources[args[0]])
        if args[1:2] == ['inspect']:
            kind, name = args[0], args[-1]; row = self.resources[kind][name]
            if kind == 'container':
                return json.dumps([{'Id': row['id'], 'Name': name, 'Config': {'Labels': row['labels']}}])
            if kind == 'secret':
                return json.dumps([{'ID': row['id'], 'Spec': {'Name': name, 'Labels': row['labels']}}])
            return json.dumps([{'id': row['id'], 'name': name, 'labels': row['labels']}])
        if args[0] == 'rm' or args[:2] in (['network', 'rm'], ['secret', 'rm']):
            kind = 'container' if args[0] == 'rm' else args[0]
            if self.fail_remove and kind == 'container':
                raise RuntimeError('controlled removal refusal')
            for name, row in list(self.resources[kind].items()):
                if args[-1] in (name, row['id']):
                    del self.resources[kind][name]
                    return ''
            raise RuntimeError('absent target')
        if args[:1] == ['info']:
            return 'arm64'
        raise AssertionError('unexpected fake engine operation: '+repr(args))


class QdrantTests(unittest.IsolatedAsyncioTestCase):
    def surface(self, name='qdrant'):
        self.assertIsNotNone(importlib.util.find_spec('vector_baseline.'+name), name+' missing')
        return importlib.import_module('vector_baseline.'+name)

    def test_pins_nonroot_private_network_and_resource_envelope(self):
        module = self.surface()
        self.assertTrue(module.PINS['arm64'].endswith('9eb80ba088703193c124db5e50a59f2028439c8bbc62fa1918625995116b50d0'))
        self.assertTrue(module.PINS['amd64'].endswith('0e8273b9130ca3b0dd9dcfaa8f55342066711f8aaeb2aa072e53344c42ecf57f'))
        stack = module.DisposableQdrant(architecture='arm64')
        q, client = stack.run_args('qdrant'), stack.run_args('client')
        for args in (q, client):
            self.assertEqual(args[args.index('--user')+1], '10001:10001')
            self.assertIn('--read-only', args)
            self.assertIn('--cap-drop=ALL', args)
            self.assertIn('--security-opt=no-new-privileges', args)
            self.assertEqual(args[args.index('--network')+1], stack.network_name)
            self.assertFalse(any(x == '-p' or x.startswith('--publish') or 'network=host' in x for x in args))
        self.assertIn('--memory=768m', q)
        self.assertIn('--memory=256m', client)
        self.assertIn('--cpus=1.5', q)
        self.assertIn('--cpus=0.5', client)
        self.assertIn('--internal', stack.network_args())
        with self.assertRaises(ValueError):
            module.DisposableQdrant(architecture='foreign')

    def test_configuration_and_argv_never_expose_key(self):
        module = self.surface()
        key = 'SYNTHETIC-KEY-NEVER-IN-ARGV'
        cfg = module.configuration(key)
        self.assertEqual(cfg['service']['api_key'], key)
        self.assertIsNone(cfg['service']['grpc_port'])
        self.assertFalse(cfg['service']['enable_cors'])
        self.assertFalse(cfg['service']['enable_tls'])
        self.assertFalse(cfg['service']['enable_snapshot_url_recovery'])
        self.assertFalse(cfg['cluster']['enabled'])
        self.assertTrue(cfg['telemetry_disabled'])
        stack = module.DisposableQdrant(architecture='arm64')
        stack.password = key
        self.assertNotIn(key, repr(stack.run_args('qdrant') + stack.run_args('client')))
        self.assertFalse(any(x in ('-e', '--env') for x in stack.run_args('qdrant')))

    def test_memory_and_team_slot_gate_refuse_before_effect(self):
        module = self.surface()
        for memory, inventory in [(34, ''), (35, 'another-test')]:
            with self.subTest(memory=memory), tempfile.TemporaryDirectory() as tmp:
                engine = Engine()
                def gate(runner):
                    module.check_preflight(memory, inventory)
                stack = module.DisposableQdrant(architecture='arm64', runner=engine, gate=gate, lock_path=Path(tmp)/'slot')
                with self.assertRaises(RuntimeError):
                    stack.__enter__()
                self.assertFalse(any(args[0] in ('create', 'start') or args[1:2] == ['create'] for args, _ in engine.commands))
                self.assertTrue(stack.cleanup_verified)
                self.assertIsNone(stack.lock)
        module.check_preflight(35, '')

    def test_pending_creates_are_cleaned_and_foreign_collisions_preserved(self):
        module = self.surface()
        for fault in ('network', 'secret', 'container'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                engine = Engine(fault)
                stack = module.DisposableQdrant(architecture='arm64', runner=engine, gate=lambda _: None, lock_path=Path(tmp)/'slot')
                with self.assertRaises(RuntimeError):
                    stack.__enter__()
                self.assertFalse(any(engine.resources.values()))
                self.assertTrue(stack.cleanup_verified)
                self.assertIsNone(stack.password)
                self.assertIsNone(stack.lock)
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine()
            stack = module.DisposableQdrant(architecture='arm64', runner=engine, gate=lambda _: None, lock_path=Path(tmp)/'slot')
            engine.resources['network'][stack.network_name] = {'id': 'foreign', 'labels': {'kaidera.b02.lifecycle': 'other'}}
            with self.assertRaises(RuntimeError):
                stack.__enter__()
            self.assertIn(stack.network_name, engine.resources['network'])
            self.assertFalse(any(args[-1] == 'foreign' and args[1:2] == ['rm'] for args, _ in engine.commands))

    def test_failed_cleanup_retains_lock_and_credential_until_verified_absence(self):
        module = self.surface()
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(); engine.fail_remove = True
            stack = module.DisposableQdrant(architecture='arm64', runner=engine, gate=lambda _: None, lock_path=Path(tmp)/'slot')
            with self.assertRaises(RuntimeError):
                stack.__enter__()
            self.assertFalse(stack.cleanup_verified)
            self.assertIsNotNone(stack.lock, 'failed cleanup must retain serialization')
            self.assertIsNotNone(stack.password, 'custody remains until absence is verified')
            engine.fail_remove = False
            stack.close()
            self.assertTrue(stack.cleanup_verified)
            self.assertIsNone(stack.lock)
            self.assertIsNone(stack.password)

    def test_dense_request_preserves_all_filters_and_refuses_sparse(self):
        module = self.surface()
        q = {'mode': 'dense', 'tenant': 'geometry', 'project': 'p', 'generation': 'g', 'vector': [1, 0],
             'lo': 1, 'hi': 20, 'kind': 3, 'time_lo': 202609, 'time_hi': 202610}
        body = module.dense_request(q)
        self.assertEqual(body['params'], {'hnsw_ef': 200, 'exact': False})
        self.assertFalse(body['with_vector'])
        self.assertEqual(body['limit'], 10)
        filters = {x['key']: x for x in body['filter']['must']}
        self.assertEqual(filters['generation']['match']['value'], 'g')
        self.assertEqual(filters['deleted']['match']['value'], False)
        self.assertEqual(filters['ordinal']['range'], {'gte': 1, 'lte': 20})
        self.assertEqual(filters['kind']['match']['value'], 3)
        self.assertEqual(filters['time']['range'], {'gte': 202609, 'lte': 202610})
        self.assertEqual(filters['tenant']['match']['value'], 'geometry')
        self.assertEqual(filters['project']['match']['value'], 'p')
        with self.assertRaises(ValueError):
            module.dense_request({**q, 'mode': 'hybrid'})

    def test_response_mapping_refuses_unknown_duplicate_or_payload_mismatch(self):
        module = self.surface()
        good = {'result': {'points': [{'id': 1, 'payload': {'comparison_id': 'a'}}]}}
        self.assertEqual(module.decode_hits(good, {1: 'a'}), ['a'])
        for points in ([{'id': 2, 'payload': {'comparison_id': 'a'}}],
                       [{'id': 1, 'payload': {'comparison_id': 'wrong'}}],
                       good['result']['points'] * 2):
            with self.subTest(points=points), self.assertRaises(ValueError):
                module.decode_hits({'result': {'points': points}}, {1: 'a'})

    async def test_persistent_proxy_child_is_reaped_after_cancel_and_partial_open(self):
        module = self.surface()
        class Stack:
            client_name = 'owned-double'
            id_map = {}
            pid = None
            def proxy_command(self):
                return [sys.executable, '-u', '-c', 'import os,json,time;print(json.dumps({"kind":"ready","pid":os.getpid()}),flush=True);time.sleep(60)']
            async def stop_proxy(self, pid):
                self.pid = pid
                os.kill(pid, signal.SIGTERM)
        stack = Stack(); client = module.Client(stack)
        await client.open()
        child = client.process
        await client.aclose()
        self.assertIsNotNone(child.returncode)
        self.assertEqual(stack.pid, child.pid)
        await client.aclose()
        class Partial(Stack):
            def proxy_command(self):
                return [sys.executable, '-u', '-c', 'import time;print("invalid",flush=True);time.sleep(60)']
        partial = module.Client(Partial())
        with self.assertRaises(ValueError):
            await partial.open()
        self.assertIsNotNone(partial.process.returncode)

    def test_proxy_sanitizes_failure_text_and_keeps_sequence(self):
        module = self.surface('qdrant_proxy')
        class Connection:
            def request(self, *args, **kwargs):
                raise RuntimeError('SYNTHETIC-SECRET-ERROR')
            def close(self):
                pass
        output = io.StringIO()
        module.serve(Connection(), 'SYNTHETIC-SECRET-KEY', io.StringIO(json.dumps({'seq': 7, 'method': 'GET', 'path': '/', 'body': None})+'\n'), output, session_pid=123)
        self.assertNotIn('SYNTHETIC-SECRET', output.getvalue())
        response = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual((response['seq'], response['error']), (7, 'RuntimeError'))

    def test_unknown_engine_refuses_before_opening_corpus(self):
        self.surface()
        with self.assertRaises(ValueError):
            benchmark.execute('never-read', 'never-created', engine='foreign')

    def test_partial_import_failure_is_not_retried(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from vector_baseline import corpus
        module = self.surface()
        opened = []
        class API:
            def __init__(self, stack):
                pass
            async def open(self):
                opened.append('open')
            async def aclose(self):
                pass
            async def call(self, method, path, body=None):
                if path == '/':
                    return {'version': '1.19.2'}
                if '/points?' in path:
                    raise RuntimeError('controlled partial import failure')
                return {'result': True}
        with tempfile.TemporaryDirectory() as tmp:
            c = corpus.write_corpus(Path(tmp)/'corpus', [[1, 0]], [{'id': 'a'}],
                                    {'provider': 'synthetic', 'model': 'fixture', 'dimension': 2, 'metric': 'cosine', 'generation': 'g'})
            stack = SimpleNamespace(id_map={}, binding={})
            clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 46]), sleep=Mock())
            with patch.object(module, 'Client', API), patch.object(module, 'time', clock):
                with self.assertRaises(RuntimeError):
                    module.install(stack, c)
        self.assertEqual(opened, ['open'], 'retry must not replay a partially applied import')

    async def test_dual_observation_includes_both_owned_containers(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        self.assertTrue(callable(getattr(benchmark, 'observe_qdrant', None)), 'both-container observer missing')
        stack = SimpleNamespace(client_name='owned-client', lifecycle='lifecycle', owned_resource=lambda *args: 'owned')
        with patch.object(benchmark, 'observe_owned', new=AsyncMock(return_value={'stats': ['owned-stats']})) as observe:
            result = await benchmark.observe_qdrant(stack)
        self.assertEqual(observe.await_count, 2)
        self.assertEqual(set(result['components']), {'qdrant', 'client'})
        self.assertFalse(result['whole_product_stack_complete'])

    def test_pg_arm_preserves_default_and_applies_nemo_slot_gate(self):
        from unittest.mock import patch
        from vector_baseline import postgres
        from test_postgres_cleanup import ResourceEngine
        module = self.surface()
        stack = postgres.DisposablePostgres(runner=ResourceEngine())
        stack.worker = 'nemo'
        labels = dict(stack.labels()[i+1].split('=', 1) for i, v in enumerate(stack.labels()) if v == '--label')
        self.assertEqual(labels['worker'], 'nemo')
        self.assertIn('cortex.test', labels)
        engine = ResourceEngine(); stack = postgres.DisposablePostgres(runner=engine); stack.worker = 'nemo'
        with patch.object(module, 'preflight', side_effect=RuntimeError('controlled memory gate')):
            with self.assertRaises(RuntimeError):
                stack.__enter__()
        self.assertFalse(engine.created, 'Nemo PG memory gate must precede create effects')
        self.assertTrue(stack.cleanup_verified)

    async def test_proxy_cancellation_reaps_pending_response_child(self):
        module = self.surface()
        class Stack:
            client_name = 'owned-double'
            def proxy_command(self):
                return [sys.executable, '-u', '-c', 'import os,json,time;print(json.dumps({"kind":"ready","pid":os.getpid()}),flush=True);time.sleep(60)']
            async def stop_proxy(self, pid):
                os.kill(pid, signal.SIGTERM)
        client = module.Client(Stack())
        await client.open()
        child = client.process
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(client.call('GET', '/'), .05)
        self.assertIsNotNone(child.returncode, 'cancelled response must reap the owned process')
        self.assertTrue(client.closed)

    def test_invalid_latency_budget_refuses_before_input_read(self):
        from unittest.mock import Mock, patch
        self.surface()
        reader = Mock(side_effect=RuntimeError('input read'))
        caught = None
        with patch.object(benchmark.corpus, 'load', reader):
            try:
                benchmark.execute('never-read', 'never-created', budget_ms=0)
            except Exception as error:
                caught = type(error)
        self.assertIs(caught, ValueError)
        self.assertEqual(reader.call_count, 0)

    def test_observed_container_contract_rejects_root_ports_and_env_keys(self):
        import copy
        module = self.surface()
        self.assertTrue(callable(getattr(module, 'verify_container', None)), 'observed container contract missing')
        row = {'Config': {'User': '10001:10001', 'Env': ['PATH=/usr/bin']},
               'HostConfig': {'PortBindings': {}, 'ReadonlyRootfs': True, 'Memory': 805306368,
                              'NanoCpus': 1500000000, 'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges']}}
        checked = module.verify_container(row, memory_bytes=805306368, cpus=1.5)
        self.assertEqual(checked['uid'], 10001)
        self.assertEqual(checked['published_ports'], False)
        for field, value in [('User', '0:0'), ('Env', ['QDRANT__SERVICE__API_KEY=SYNTHETIC-NEVER-LOG'])]:
            bad = copy.deepcopy(row); bad['Config'][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                module.verify_container(bad, memory_bytes=805306368, cpus=1.5)
        for field, value in [('PortBindings', {'6333/tcp': [{'HostPort': '6333'}]}), ('ReadonlyRootfs', False), ('Memory', 2147483648)]:
            bad = copy.deepcopy(row); bad['HostConfig'][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                module.verify_container(bad, memory_bytes=805306368, cpus=1.5)

    async def test_upload_binds_stored_geometry_payload_indexes_and_exact_count(self):
        from vector_baseline import corpus
        module = self.surface()
        class API:
            def __init__(self, count=2):
                self.calls, self.count = [], count
            async def call(self, method, path, body=None):
                self.calls.append((method, path, body))
                if path == '/':
                    return {'version': '1.19.2'}
                if path.endswith('/count'):
                    return {'result': {'count': self.count}}
                return {'result': {'status': 'green'}}
        with tempfile.TemporaryDirectory() as tmp:
            c = corpus.write_corpus(Path(tmp)/'corpus', [[3, 4], [1, 0]],
                                    [{'id': 'a'}, {'id': 'b'}],
                                    {'provider': 'synthetic', 'model': 'fixture', 'dimension': 2,
                                     'metric': 'cosine', 'generation': 'g'})
            api = API(); mapping, binding = await module.upload(api, c)
            self.assertEqual(mapping, {1: 'a', 2: 'b'})
            create = next(body for method, path, body in api.calls if method == 'PUT' and path == '/collections/b02')
            self.assertEqual(create['vectors'], {'size': 2, 'distance': 'Cosine'})
            self.assertEqual(create['hnsw_config'], {'m': 16, 'ef_construct': 64})
            indexes = {body['field_name']: body['field_schema'] for _, path, body in api.calls if '/index?' in path}
            self.assertEqual(indexes, {'tenant': 'keyword', 'project': 'keyword', 'generation': 'keyword',
                                       'deleted': 'bool', 'ordinal': 'integer', 'kind': 'integer', 'time': 'integer'})
            points = next(body['points'] for _, path, body in api.calls if '/points?' in path)
            self.assertEqual(points[0]['vector'], [3., 4.])
            self.assertEqual(points[0]['payload']['comparison_id'], 'a')
            self.assertEqual(points[0]['payload']['generation'], 'g')
            self.assertEqual(binding['exact_count'], 2)
            self.assertFalse(binding['hnsw_use_qualified'])
            with self.assertRaises(ValueError):
                await module.upload(API(count=1), c)

    def test_storage_mount_uses_supported_nonroot_tmpfs_ownership(self):
        module = self.surface()
        stack = module.DisposableQdrant(architecture='arm64')
        args = stack.run_args('qdrant')
        mounts = [args[i+1] for i, value in enumerate(args) if value == '--mount']
        storage = [value for value in mounts if 'destination=/qdrant/storage' in value]
        self.assertEqual(len(storage), 1, 'owned storage needs supported tmpfs ownership syntax')
        fields = dict(value.split('=', 1) for value in storage[0].split(','))
        self.assertEqual(fields, {'type': 'tmpfs', 'destination': '/qdrant/storage',
                                  'tmpfs-size': '536870912', 'tmpfs-mode': '0700', 'U': 'true'})
        self.assertFalse(any('uid=' in value or 'gid=' in value for i, value in enumerate(args)
                             if i and args[i-1] == '--tmpfs'))

    def test_podman_empty_capability_sets_are_admitted_but_nonempty_refused(self):
        import copy
        module = self.surface()
        row = {'Config': {'User': '10001:10001', 'Env': []},
               'EffectiveCaps': None, 'BoundingCaps': None,
               'HostConfig': {'PortBindings': {}, 'ReadonlyRootfs': True, 'Memory': 805306368,
                              'NanoCpus': 1500000000, 'CapDrop': ['CAP_CHOWN', 'CAP_SETUID'],
                              'CapAdd': [], 'Privileged': False, 'SecurityOpt': ['no-new-privileges']}}
        caught = None
        try:
            checked = module.verify_container(row, memory_bytes=805306368, cpus=1.5)
        except Exception as error:
            caught = type(error)
        self.assertIsNone(caught, 'Podman null effective/bounding sets are explicit empty sets')
        self.assertTrue(checked['cap_drop_all'])
        for field in ('EffectiveCaps', 'BoundingCaps'):
            bad = copy.deepcopy(row); bad[field] = ['CAP_NET_ADMIN']
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                module.verify_container(bad, memory_bytes=805306368, cpus=1.5)
        for field, value in [('CapAdd', ['CAP_NET_ADMIN']), ('Privileged', True)]:
            bad = copy.deepcopy(row); bad['HostConfig'][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                module.verify_container(bad, memory_bytes=805306368, cpus=1.5)
