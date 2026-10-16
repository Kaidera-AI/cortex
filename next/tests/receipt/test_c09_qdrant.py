"""Frozen C09 sourceA protocol controls; every effect is an injected memory fake."""
import copy
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from cortex_core.modules.vector import qdrant as module


class Server:
    def __init__(self, fault=None):
        self.fault, self.calls, self.points, self.indexes = fault, [], {}, {}
        self.config = None
    def __call__(self, method, path, body, role):
        self.calls.append((method, path, copy.deepcopy(body), role))
        status = 200; result = True
        if method == 'GET' and path == '/':
            value = {'version': '1.19.3' if self.fault == 'version' else '1.19.2'}
        elif method == 'PUT' and path == '/collections/c09-probe':
            self.config = copy.deepcopy(body); value = {'result': True}
        elif method == 'GET' and path == '/collections/c09-probe':
            schema = dict(self.indexes)
            if self.fault == 'index':
                schema.pop('generation', None)
            value = {'result': {'config': {'params': {'vectors': self.config['vectors']}}, 'payload_schema': schema}}
        elif path == '/collections/c09-probe/index':
            self.indexes[body['field_name']] = {'data_type': body['field_schema']}; value = {'result': True}
        elif method == 'PUT' and path == '/collections/c09-probe/points':
            self.points.update({p['id']: copy.deepcopy(p) for p in body['points']}); value = {'result': True}
        elif method == 'POST' and path == '/collections/c09-probe/points':
            points = [copy.deepcopy(self.points[i]) for i in body['ids']]
            if self.fault == 'vector':
                points[0]['vector'][0] = 9.0
            if self.fault == 'payload':
                points[0]['payload']['tenant'] = 'other'
            value = {'result': points}
        elif path == '/collections/c09-probe/points/count':
            value = {'result': {'count': len(self.points)}}
        elif path == '/collections/c09-probe/points/query':
            if self.fault == 'missing_api':
                status = 404; value = {'status': 'error'}
            else:
                selected = [copy.deepcopy(self.points[1])]
                if self.fault == 'filter':
                    selected.append(copy.deepcopy(self.points[3]))
                value = {'result': {'points': selected}}
        elif path == '/collections/c09-probe/points/delete':
            if self.fault != 'delete':
                for identity in body['points']:
                    self.points.pop(identity, None)
            value = {'result': True}
        elif path == '/collections/aliases':
            value = {'result': True}
        elif path == '/aliases':
            value = {'result': {'aliases': [{'alias_name': 'c09-probe-alias',
                     'collection_name': 'wrong' if self.fault == 'alias' else 'c09-probe'}]}}
        elif method == 'DELETE' and path == '/collections/c09-probe':
            self.points.clear(); self.config = None; value = {'result': True}
        else:
            raise AssertionError('unexpected fake protocol operation')
        if self.fault == 'oversize' and path == '/':
            value['padding'] = 'x' * (module.MAX_RESPONSE_BYTES + 1)
        if self.fault == 'redirect' and path == '/':
            status = 302
        return {'status': status, 'body': json.dumps(value).encode()}


class C09Tests(unittest.TestCase):
    def port(self, server, descriptor=None, platform='linux/arm64', config=None):
        return module.InertPort(descriptor or module.pinned_descriptor(), platform,
            config or module.private_config('10.89.0.2'), transport=module.FakeProtocol(server))

    def test_descriptor_wrong_version_source_license_and_child_refuse_before_transport(self):
        for key in ('version', 'source_commit', 'license', 'license_sha256', 'index_digest', 'platforms', 'profile', 'binding'):
            with self.subTest(key=key):
                value = module.pinned_descriptor(); value[key] = {} if key == 'platforms' else 'wrong'
                server = Server(); caught = None
                try:
                    self.port(server, value)
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ValueError, 'bad descriptor must fail before any fake effect')
                self.assertEqual(server.calls, [])

    def test_unknown_platform_and_secret_bearing_descriptor_fields_refuse(self):
        for platform, extra in [('darwin/arm64', False), ('linux/arm64', True)]:
            with self.subTest(platform=platform, extra=extra):
                value = module.pinned_descriptor()
                if extra:
                    value['api_key'] = 'SYNTHETIC-NEVER-LOG'
                caught = None
                try:
                    self.port(Server(), value, platform)
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ValueError)

    def test_private_endpoint_and_delivery_refuse_public_wildcard_host_ports_root(self):
        for args in [dict(address='8.8.8.8'), dict(address='0.0.0.0'), dict(address='localhost'),
                     dict(address='127.0.0.1'), dict(address='10.89.0.2', published_ports=(6333,)),
                     dict(address='10.89.0.2', host_network=True), dict(address='10.89.0.2', uid=0)]:
            with self.subTest(args=args):
                caught = None
                try:
                    module.private_config(**args)
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ValueError)

    def test_private_config_only_refs_and_rejects_missing_equal_or_path_traversal_refs(self):
        for writer, reader in [('', 'qdrant/read'), ('same', 'same'), ('../key', 'qdrant/read')]:
            with self.subTest(writer=writer):
                caught = None
                try:
                    module.private_config('10.89.0.2', writer_key_ref=writer, reader_key_ref=reader)
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ValueError)
        value = module.private_config('10.89.0.2')
        self.assertNotIn('api_key', json.dumps(value))
        self.assertEqual(value['delivery']['published_ports'], [])

    def test_complete_fake_required_api_protocol_both_pinned_platforms(self):
        for platform in ('linux/amd64', 'linux/arm64'):
            with self.subTest(platform=platform):
                server = Server(); evidence = self.port(server, platform=platform).probe()
                self.assertEqual(evidence['verified_operations'], ['version', 'collection', 'indexes', 'upsert',
                                 'readback', 'filtered_query', 'delete', 'aliases', 'cleanup'])
                self.assertFalse(server.points)
                self.assertFalse(evidence['module_ready'])
                self.assertFalse(evidence['core_auth_bound'])

    def test_wrong_runtime_version_missing_api_redirect_oversize_are_typed_refusals(self):
        for fault in ('version', 'missing_api', 'redirect', 'oversize'):
            with self.subTest(fault=fault):
                caught = None
                try:
                    self.port(Server(fault)).probe()
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, module.ProtocolFailure)

    def test_readback_indexes_vectors_payload_filter_delete_alias_are_verified(self):
        for fault in ('index', 'vector', 'payload', 'filter', 'delete', 'alias'):
            with self.subTest(fault=fault):
                caught = None
                try:
                    self.port(Server(fault)).probe()
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, module.ProtocolFailure)

    def test_transport_error_never_echoes_exception_text_or_retries(self):
        calls = []
        def broken(*args):
            calls.append(1); raise RuntimeError('SYNTHETIC-PRIVATE-DO-NOT-ECHO')
        caught = None
        try:
            self.port(broken).probe()
        except Exception as error:
            caught = error
        self.assertIsInstance(caught, module.ProtocolFailure)
        self.assertNotIn('SYNTHETIC-PRIVATE-DO-NOT-ECHO', str(caught))
        self.assertEqual(len(calls), 1)

    def test_inert_profile_has_no_default_transport_network_registration_or_ready(self):
        with self.assertRaises(TypeError):
            module.InertPort(module.pinned_descriptor(), 'linux/arm64', module.private_config('10.89.0.2'))
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
            port = self.port(Server()); evidence = port.probe()
        self.assertFalse(evidence['module_ready'])
        self.assertEqual(evidence['state'], 'unbound')
        self.assertFalse(hasattr(port, 'register'))
        self.assertFalse(hasattr(port, 'apply'))
        self.assertEqual(evidence['engine_decision'], 'UNDECIDED')

    def test_boolean_numeric_readback_and_coerced_delivery_flags_refuse(self):
        for fault in ('id', 'revision', 'vector', 'delivery'):
            with self.subTest(fault=fault):
                class Bad(Server):
                    def __call__(self, method, path, body, role):
                        response = super().__call__(method, path, body, role)
                        if method == 'POST' and path == '/collections/c09-probe/points':
                            value = json.loads(response['body']); point = value['result'][0]
                            if fault == 'id':
                                point['id'] = True
                            elif fault == 'revision':
                                point['payload']['revision'] = True
                            elif fault == 'vector':
                                point['vector'][0] = True
                            response['body'] = json.dumps(value).encode()
                        return response
                config = module.private_config('10.89.0.2')
                if fault == 'delivery':
                    config['delivery']['read_only_root'] = 1
                caught = None
                try:
                    self.port(Bad(), config=config).probe()
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ValueError if fault == 'delivery' else module.ProtocolFailure)

    def test_fake_requests_bind_predicates_vectors_and_separate_service_roles(self):
        server = Server(); self.port(server).probe()
        request = next(body for method, path, body, role in server.calls if path.endswith('/points/query'))
        matches = {row['key']: row['match']['value'] for row in request['filter']['must']}
        self.assertEqual(matches, {'tenant': 'fake-tenant', 'project': 'fake-project',
                                  'generation': 'fake-generation', 'deleted': False})
        self.assertEqual(len(request['query']), 768)
        self.assertTrue(request['with_vector'])
        self.assertTrue(all(role == 'writer' for method, path, body, role in server.calls
                            if method in ('PUT', 'DELETE') or path in ('/collections/aliases', '/collections/c09-probe/points/delete')))

    def test_fake_partial_effect_cleanup_failure_retains_primary_and_never_retries(self):
        class Broken(Server):
            def __call__(self, method, path, body, role):
                if method == 'DELETE':
                    self.calls.append((method, path, body, role)); raise RuntimeError('SYNTHETIC-CLEANUP-SECRET')
                response = super().__call__(method, path, body, role)
                if method == 'PUT' and path.endswith('/points'):
                    raise RuntimeError('SYNTHETIC-UPsert-SECRET')
                return response
        server = Broken(); caught = None
        try:
            self.port(server).probe()
        except Exception as error:
            caught = error
        self.assertIsInstance(caught, module.ProtocolFailure)
        self.assertEqual(getattr(caught, 'primary_reason', None), 'fake_protocol_failed')
        self.assertEqual(getattr(caught, 'cleanup_reason', None), 'fake_cleanup_unverified')
        self.assertNotIn('SECRET', str(caught))
        self.assertEqual(sum(m == 'PUT' and p.endswith('/points') for m, p, b, r in server.calls), 1)


# S2 exercises HTTPX with a controlled in-memory transport; never native credit.
import asyncio
import time
import httpx

try:
    from cortex_core.modules.vector import qdrant_client as s2
except ImportError:
    s2 = None  # RED is a BODY assertion for the absent new source surface.

PLATFORMS = ('linux/amd64', 'linux/arm64')
WRITER, READER = 'synthetic-writer-key', 'synthetic-reader-key'


class HTTPServer:
    def __init__(self, fault=None):
        self.fault, self.calls, self.collections = fault, [], {}
        self.streams = []

    async def __call__(self, request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        key = request.headers.get('api-key')
        self.calls.append((method, str(request.url), body, key))
        def reply(value, status=200, **kwargs):
            return httpx.Response(status, json=value, **kwargs)
        if key not in (WRITER, READER):
            return reply({'status': 'denied'}, 401)
        if self.fault == 'redirect':
            return httpx.Response(307, headers={'location': 'https://example.invalid/steal'})
        if method == 'GET' and path == '/':
            if self.fault == 'malformed':
                return httpx.Response(200, content=b'{bad', headers={'content-type': 'application/json'})
            if self.fault == 'duplicate':
                return httpx.Response(200, content=b'{"version":"1.19.2","version":"1.19.2"}',
                                      headers={'content-type': 'application/json'})
            if self.fault == 'oversize':
                return reply({'version': '1.19.2', 'padding': 'x' * (1024 * 1024)})
            if self.fault == 'encoding':
                return httpx.Response(200, content=b'no', headers={
                    'content-type': 'application/json', 'content-encoding': 'gzip'})
            return reply({'version': 'bad' if self.fault == 'version' else '1.19.2'})
        collection = path.removesuffix('/points')
        if method in ('PUT', 'DELETE') and key == READER and self.fault != 'reader_write':
            return reply({'status': 'denied'}, 403)
        if method == 'PUT' and not path.endswith('/points'):
            if self.fault == 'writer_deny':
                return reply({'status': 'denied'}, 403)
            self.collections[path] = {}
            return reply({'result': True, 'status': 'ok'})
        if method == 'DELETE':
            if self.fault == 'cleanup':
                return reply({'error': 'SYNTHETIC-PRIVATE'}, 500)
            self.collections.pop(path, None)
            return reply({'result': True, 'status': 'ok'})
        points = self.collections[collection]
        if method == 'PUT':
            if self.fault in ('missing_api', 'mode'):
                return reply({'status': 'unsupported'}, 404 if self.fault == 'missing_api' else 400)
            for point in body['points']:
                old = points.get(point['id'])
                mode = body.get('update_mode', 'upsert')
                if mode == 'insert_only' and old and self.fault != 'ignore_insert':
                    continue
                if mode == 'update_only' and old is None and self.fault != 'ignore_update':
                    continue
                must = body.get('update_filter', {}).get('must', [])
                if old and self.fault != 'ignore_filter' and any(
                        old['payload'].get(row['key']) != row['match']['value'] for row in must):
                    continue
                points[point['id']] = copy.deepcopy(point)
            if self.fault == 'ambiguous':
                raise RuntimeError('SYNTHETIC-PRIVATE-PARTIAL-EFFECT')
            return reply({'status': 'ok', 'result': {'operation_id': 1,
                          'status': 'acknowledged' if self.fault == 'ack' else 'completed'}})
        if method == 'POST':
            values = [copy.deepcopy(points[i]) for i in body['ids'] if i in points]
            if self.fault == 'readback' and values:
                values[0]['payload']['revision'] = True
            return reply({'status': 'ok', 'result': values})
        raise AssertionError('unexpected controlled HTTP operation')


class BodyStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, gate=None, error=None):
        self.chunks, self.gate, self.error, self.closed = chunks, gate, error, False

    async def __aiter__(self):
        if self.gate is not None:
            await self.gate.wait()
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


class S2Tests(unittest.IsolatedAsyncioTestCase):
    def client(self, server=None, *, platform='linux/arm64', resolver=None, **kwargs):
        async def resolve(ref):
            return {'qdrant/admin': WRITER, 'qdrant/read': READER}[ref]
        transport = kwargs.pop('transport', httpx.MockTransport(server or HTTPServer()))
        descriptor = kwargs.pop('descriptor', module.pinned_descriptor())
        config = kwargs.pop('config', module.private_config('10.89.0.2'))
        return s2.PrivateClient(descriptor, platform, config, resolve_key=resolver or resolve,
                                transport=transport, **kwargs)

    async def refusal(self, client, *, deadline=None):
        caught = None
        try:
            await client.probe(deadline=deadline)
        except Exception as error:
            caught = error
        self.assertIsInstance(caught, module.ProtocolFailure)
        self.assertEqual(caught.code, 'capability_unavailable')
        self.assertNotIn('SYNTHETIC-PRIVATE', str(caught))
        return caught

    async def test_s2_complete_controlled_probe_both_platforms_never_ready(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                server = HTTPServer()
                with patch.object(socket.socket, 'connect', side_effect=AssertionError('IP forbidden')):
                    async with self.client(server, platform=platform) as client:
                        value = await client.probe()
                        self.assertEqual(value['state'], 'unavailable')
                        self.assertFalse(value['module_ready'])
                        self.assertFalse(value['core_auth_bound'])
                        self.assertFalse(value['native_qualified'])
                        self.assertEqual(value['verified_operations'], [
                            'version', 'reader_denied_write', 'insert_only', 'filter_miss',
                            'existing_insert_only', 'missing_update_only', 'update_only', 'cleanup'])
                        self.assertFalse(hasattr(client, 'apply'))
                        self.assertFalse(hasattr(client, 'register'))
                self.assertFalse(server.collections)
                writes = [body for method, url, body, key in server.calls if url.split('?')[0].endswith('/points') and method == 'PUT']
                self.assertTrue(all(body['update_mode'] in ('insert_only', 'update_only') for body in writes))
                self.assertTrue(all('wait=true' in url and 'ordering=strong' in url
                                    for method, url, body, key in server.calls if method == 'PUT' and '/points' in url))

    async def test_s2_descriptor_and_platform_refuse_before_custody_or_http(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            for field in ('version', 'source_commit', 'license', 'index_digest', 'platforms', 'binding'):
                with self.subTest(platform=platform, field=field):
                    descriptor = module.pinned_descriptor(); descriptor[field] = 'wrong'
                    with self.assertRaises(module.ProtocolFailure):
                        self.client(platform=platform, descriptor=descriptor)
        with self.assertRaises(module.ProtocolFailure):
            self.client(platform='darwin/arm64')

    async def test_s2_exact_private_config_both_platforms(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            for section, field, value in [
                    ('service', 'host', '8.8.8.8'), ('service', 'host', '0.0.0.0'),
                    ('service', 'host', '127.0.0.1'), ('service', 'host', 'localhost'),
                    ('service', 'host', 'fd00::1%eth0'), ('service', 'grpc_port', 6334),
                    ('service', 'enable_cors', True), ('service', 'enable_snapshot_url_recovery', True),
                    ('delivery', 'published_ports', [6333]), ('delivery', 'host_network', True),
                    ('delivery', 'uid', 0), ('delivery', 'read_only_root', 1),
                    ('secret_refs', 'reader', 'qdrant/admin')]:
                with self.subTest(platform=platform, field=field, value=value):
                    config = module.private_config('10.89.0.2'); config[section][field] = value
                    with self.assertRaises(module.ProtocolFailure):
                        self.client(platform=platform, config=config)
        async with self.client(config=module.private_config('fd00::2')) as client:
            await client.probe()

    async def test_s2_native_or_unknown_transport_refused_before_keys(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        calls = []
        async def resolve(ref):
            calls.append(ref); return WRITER
        for transport in (None, object()):
            for platform in PLATFORMS:
                with self.assertRaises(module.ProtocolFailure):
                    async with self.client(platform=platform, transport=transport, resolver=resolve):
                        self.fail('unqualified native transport entered')
        self.assertEqual(calls, [])

    async def test_s2_resolved_keys_missing_equal_invalid_and_raising_refuse_before_http(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            for value in (None, '', ' ', 'same', 'bad\r\nheader', 4):
                with self.subTest(platform=platform, value=value):
                    async def resolve(ref):
                        return value
                    server = HTTPServer()
                    async with self.client(server, platform=platform, resolver=resolve) as client:
                        await self.refusal(client)
                    self.assertEqual(server.calls, [])
            async def broken(ref):
                raise RuntimeError('SYNTHETIC-PRIVATE-CUSTODY')
            server = HTTPServer()
            async with self.client(server, platform=platform, resolver=broken) as client:
                await self.refusal(client)
            self.assertEqual(server.calls, [])

    async def test_s2_credentials_resolve_fresh_and_roles_are_enforced(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            for fault in (None, 'reader_write', 'writer_deny'):
                with self.subTest(platform=platform, fault=fault):
                    calls = []
                    async def resolve(ref):
                        calls.append(ref); return WRITER if ref.endswith('admin') else READER
                    server = HTTPServer(fault)
                    async with self.client(server, platform=platform, resolver=resolve) as client:
                        if fault:
                            await self.refusal(client)
                        else:
                            await client.probe(); await client.probe()
                    self.assertEqual(calls, ['qdrant/admin', 'qdrant/read'] * (1 if fault else 2))
                    self.assertFalse(server.collections)

    async def test_s2_api_version_mode_completion_and_sequential_semantics_refuse(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            for fault in ('version', 'missing_api', 'mode', 'ack', 'ignore_filter',
                          'ignore_insert', 'ignore_update', 'readback'):
                with self.subTest(platform=platform, fault=fault):
                    server = HTTPServer(fault)
                    async with self.client(server, platform=platform) as client:
                        await self.refusal(client)
                    self.assertFalse(server.collections)

    async def test_s2_redirect_proxy_and_transport_policy(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            server = HTTPServer('redirect')
            with patch.dict('os.environ', {'HTTP_PROXY': 'http://127.0.0.1:1',
                                          'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
                async with self.client(server, platform=platform) as client:
                    self.assertFalse(client._http.trust_env)
                    self.assertFalse(client._http.follow_redirects)
                    await self.refusal(client)
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(server.calls[0][1], 'http://10.89.0.2:6333/')

    async def test_s2_closed_routes_and_no_ordinary_upsert_before_http(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            server = HTTPServer()
            async with self.client(server, platform=platform) as client:
                for method, path, body in [
                        ('GET', 'https://example.invalid/', None), ('GET', '//evil/', None),
                        ('GET', '/../collections', None), ('GET', '/collections?url=evil', None),
                        ('GET', '/collections/c09-probe-' + 'a'*32 + '%2fpoints', None),
                        ('PUT', '/collections/c09-probe-' + 'a'*32 + '/points', {'points': []}),
                        ('PUT', '/collections/c09-probe-' + 'a'*32 + '/points', {'points': [], 'update_mode': 'upsert'})]:
                    with self.subTest(platform=platform, path=path, body=body):
                        with self.assertRaises(module.ProtocolFailure):
                            await client._request(method, path, body, WRITER, time.monotonic()+1)
            self.assertEqual(server.calls, [])

    async def test_s2_response_bounds_encoding_duplicate_and_malformed_refuse(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            for fault in ('malformed', 'duplicate', 'oversize', 'encoding'):
                with self.subTest(platform=platform, fault=fault):
                    async with self.client(HTTPServer(fault), platform=platform) as client:
                        await self.refusal(client)

    async def test_s2_stream_bounds_and_late_error_close_response(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            for chunks, error in [([b'x'*65536]*17, None),
                                  ([b'{"version":"1.19.2"}'], RuntimeError('SYNTHETIC-PRIVATE-STREAM'))]:
                stream = BodyStream(chunks, error=error)
                async def handler(request):
                    return httpx.Response(200, stream=stream, headers={'content-type': 'application/json'})
                async with self.client(platform=platform, transport=httpx.MockTransport(handler)) as client:
                    await self.refusal(client)
                self.assertTrue(stream.closed)

    async def test_s2_request_bound_and_deadline_validation_before_http(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            server = HTTPServer()
            async with self.client(server, platform=platform) as client:
                with self.assertRaises(module.ProtocolFailure):
                    await client._request('PUT', '/collections/c09-probe-'+'a'*32,
                                          {'padding': 'x'*(1024*1024)}, WRITER, time.monotonic()+1)
                for deadline in (False, float('nan'), float('inf'), time.monotonic()-1):
                    await self.refusal(client, deadline=deadline)
            self.assertEqual(server.calls, [])

    async def test_s2_custody_connect_and_stream_share_total_deadline(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            for stage in ('custody', 'connect', 'stream'):
                with self.subTest(platform=platform, stage=stage):
                    gate = asyncio.Event(); calls = []
                    async def resolve(ref):
                        if stage == 'custody':
                            await gate.wait()
                        return WRITER if ref.endswith('admin') else READER
                    stream = BodyStream([b'{"version":"1.19.2"}'], gate=gate)
                    async def handler(request):
                        calls.append(request)
                        if stage == 'connect':
                            await gate.wait()
                        return httpx.Response(200, stream=stream, headers={'content-type': 'application/json'})
                    client = self.client(platform=platform, resolver=resolve,
                                         transport=httpx.MockTransport(handler), timeout_seconds=.04)
                    async with client:
                        started = time.monotonic(); await self.refusal(client)
                        self.assertLess(time.monotonic()-started, .5)
                        gate.set(); await asyncio.sleep(.01)
                    if stage == 'custody':
                        self.assertEqual(calls, [])
                    if stage == 'stream':
                        self.assertTrue(stream.closed)

    async def test_s2_caller_deadline_and_blocking_callback_checked_after_await(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            server = HTTPServer()
            async def blocking(ref):
                time.sleep(.025)
                return WRITER if ref.endswith('admin') else READER
            async with self.client(server, platform=platform, resolver=blocking) as client:
                await self.refusal(client, deadline=time.monotonic()+.01)
            self.assertEqual(server.calls, [])

    async def test_s2_queue_is_inside_deadline_and_eight_in_flight(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            gate = asyncio.Event(); started = asyncio.Event(); active = 0
            async def handler(request):
                nonlocal active
                active += 1
                if active == 8:
                    started.set()
                await gate.wait()
                return httpx.Response(200, json={'version': 'bad'})
            async with self.client(platform=platform, transport=httpx.MockTransport(handler)) as client:
                jobs = [asyncio.create_task(client.probe()) for _ in range(8)]
                await asyncio.wait_for(started.wait(), 1)
                await self.refusal(client, deadline=time.monotonic()+.02)
                self.assertEqual(active, 8)
                gate.set()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def test_s2_cancellation_retains_owned_work_until_settled(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            entered, release = asyncio.Event(), asyncio.Event()
            async def stubborn(ref):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                return WRITER if ref.endswith('admin') else READER
            client = self.client(platform=platform, resolver=stubborn)
            await client.__aenter__()
            task = asyncio.create_task(client.probe())
            await entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertGreater(client.status()['pending_operations'], 0)
            release.set(); await asyncio.sleep(.01)
            await client.aclose()
            self.assertEqual(client.status()['pending_operations'], 0)

    async def test_s2_close_bounds_noncooperative_owner_and_never_grants_cleanup(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            entered, release = asyncio.Event(), asyncio.Event()
            async def stubborn(ref):
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                return WRITER if ref.endswith('admin') else READER
            client = self.client(platform=platform, resolver=stubborn)
            await client.__aenter__()
            task = asyncio.create_task(client.probe())
            await entered.wait()
            with self.assertRaises(module.ProtocolFailure):
                await client.aclose(deadline=time.monotonic()+.02)
            self.assertGreater(client.status()['pending_operations'], 0)
            release.set(); await asyncio.sleep(.01)
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()

    async def test_s2_ambiguous_write_not_retried_cleanup_is_owned(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            server = HTTPServer('ambiguous')
            async with self.client(server, platform=platform) as client:
                await self.refusal(client)
            self.assertEqual(sum(m == 'PUT' and '/points' in u and k == WRITER for m,u,b,k in server.calls), 1)
            self.assertEqual(sum(m == 'DELETE' for m,u,b,k in server.calls), 1)
            self.assertFalse(server.collections)

    async def test_s2_cleanup_failure_and_client_lifecycle_remain_unavailable(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            client = self.client(HTTPServer('cleanup'), platform=platform)
            await self.refusal(client)  # no request outside owned lifecycle
            async with client:
                error = await self.refusal(client)
                self.assertEqual(error.cleanup_reason, 'qdrant_cleanup_unverified')
                self.assertFalse(client.status()['module_ready'])
            self.assertTrue(client._http.is_closed)
            await client.aclose()
            await self.refusal(client)
