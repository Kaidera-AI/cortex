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

    async def test_s2_custody_protocol_failure_reason_is_also_scrubbed(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            async def broken(ref):
                raise module.ProtocolFailure('SYNTHETIC-PRIVATE-CUSTODY')
            server = HTTPServer()
            async with self.client(server, platform=platform, resolver=broken) as client:
                error = await self.refusal(client)
                self.assertEqual(error.reason, 'qdrant_credentials_refused')
            self.assertEqual(server.calls, [])

    async def test_s2_pending_work_has_a_separate_finite_admission_bound(self):
        self.assertIsNotNone(s2, 'S2 source surface absent')
        for platform in PLATFORMS:
            gate, entered = asyncio.Event(), asyncio.Event()
            active = 0
            async def handler(request):
                nonlocal active
                active += 1
                if active == 8:
                    entered.set()
                await gate.wait()
                return httpx.Response(200, json={'version': 'bad'})
            async with self.client(platform=platform, transport=httpx.MockTransport(handler)) as client:
                jobs = [asyncio.create_task(client.probe()) for _ in range(16)]
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await self.refusal(client)
                    self.assertEqual(client.status()['pending_operations'], 16)
                    self.assertEqual(active, 8)
                finally:
                    gate.set()
                    await asyncio.gather(*jobs, return_exceptions=True)

# S3 controls are appended; the complete S2 prefix remains immutable.
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID
from cortex_core.embeddings.projection_revision import EmbeddingIdentity
try:
    from cortex_core.modules.vector import projection as s3
except ImportError:
    s3 = None


class ProjectionServer:
    """Controlled destination with conditional whole-point replacement, no Core."""
    def __init__(self, fault=None):
        self.fault, self.points, self.calls = fault, {}, []
        self.before_write = None
        self.reads = 0

    async def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, copy.deepcopy(body), request.headers['api-key']))
        def reply(value, status=200):
            return httpx.Response(status, json=value)
        if request.method == 'PUT':
            if self.before_write is not None:
                callback, self.before_write = self.before_write, None
                callback(self)
            if self.fault == 'denied':
                return reply({'error': 'SYNTHETIC-PRIVATE'}, 403)
            if self.fault == 'unsupported':
                return reply({'error': 'SYNTHETIC-PRIVATE'}, 404)
            for point in body['points']:
                old = self.points.get(point['id'])
                mode = body.get('update_mode', 'upsert')
                if mode == 'insert_only' and old is not None:
                    continue
                if mode == 'update_only' and old is None:
                    continue
                if old is not None and any(old['payload'].get(c['key']) != c['match']['value']
                        for c in body.get('update_filter', {}).get('must', [])):
                    continue
                self.points[point['id']] = copy.deepcopy(point)
            if self.fault == 'lost_ack':
                raise RuntimeError('SYNTHETIC-PRIVATE-PARTIAL-EFFECT')
            return reply({'status': 'ok', 'result': {'operation_id': 4,
                'status': 'acknowledged' if self.fault == 'ack' else 'completed'}})
        if request.method == 'POST' and request.url.path.endswith('/points'):
            self.reads += 1
            values = [copy.deepcopy(self.points[i]) for i in body['ids'] if i in self.points]
            if self.fault == 'readback' and self.reads > 1 and values:
                values[0]['vector'][0] += 1
            if self.fault == 'duplicate' and values:
                values.append(copy.deepcopy(values[0]))
            if self.fault == 'malformed':
                return httpx.Response(200, content=b'{"status":"ok","result":[],"result":[]}',
                                      headers={'content-type': 'application/json'})
            return reply({'status': 'ok', 'result': values})
        raise AssertionError('Unexpected product HTTP route')


class S3Tests(unittest.IsolatedAsyncioTestCase):
    client = S2Tests.client

    def binding(self, **overrides):
        self.assertIsNotNone(s3, 'S3 source contract absent')
        values = dict(installation_id=UUID(int=1), tenant_id=UUID(int=2), project_id=UUID(int=3),
                      module_id='qdrant', consumer_generation=1, collection_generation=UUID(int=4),
                      identity=EmbeddingIdentity('synthetic','model','v1',768,'text-v1'))
        return s3.ProjectionBinding(**(values | overrides))

    def event(self, revision=1, tombstone=False, **overrides):
        payload = b'committed synthetic text'
        import hashlib
        env = dict(installation_id=str(UUID(int=1)), tenant_id=str(UUID(int=2)),
                   project_id=str(UUID(int=3)), aggregate_id=str(UUID(int=10)),
                   event_id=str(UUID(int=100+revision)), aggregate_revision=revision,
                   aggregate_kind='memory', tombstone=tombstone, schema_version=1,
                   operation='delete' if tombstone else 'upsert',
                   payload_sha256=hashlib.sha256(payload).hexdigest())
        return SimpleNamespace(cursor=revision, envelope=env | overrides, payload=payload)

    @staticmethod
    def vector(first=1.):
        return [first, .5] + [0.]*766

    async def apply_refusal(self, client, binding, event, vector=None):
        caught = None
        try:
            await client.apply_projection(binding, event, self.vector() if vector is None else vector)
        except Exception as error:
            caught = error
        self.assertIsInstance(caught, module.ProtocolFailure)
        self.assertEqual(caught.code, 'capability_unavailable')
        self.assertNotIn('SYNTHETIC-PRIVATE', str(caught))
        return caught

    async def test_s3_insert_complete_key_receipt_and_no_ordinary_upsert(self):
        binding = self.binding()
        for platform in PLATFORMS:
            server = ProjectionServer()
            async with self.client(server, platform=platform) as client:
                receipt = await client.apply_projection(binding, self.event(), self.vector())
                self.assertEqual(s3.validate_receipt(receipt, binding, self.event()), 'applied')
                self.assertFalse(client.status()['module_ready'])
                self.assertFalse(client.status()['core_auth_bound'])
            self.assertEqual(len(server.points), 1)
            point = next(iter(server.points.values()))
            self.assertEqual(point, s3.build_point(binding, self.event(), self.vector()))
            write = [c for c in server.calls if c[0] == 'PUT']
            self.assertEqual(len(write), 1)
            self.assertEqual(write[0][2]['update_mode'], 'insert_only')
            self.assertTrue(all(c[1].startswith('/collections/cortex-') for c in server.calls))
            self.assertEqual(server.reads, 2)

    async def test_s3_full_key_and_model_generation_change_point_identity(self):
        binding = self.binding()
        point = s3.build_point(binding, self.event(), self.vector())
        fields = dict(installation_id=UUID(int=21),tenant_id=UUID(int=22),project_id=UUID(int=23),
                      module_id='other',consumer_generation=2,collection_generation=UUID(int=24),
                      identity=EmbeddingIdentity('synthetic','model','v2',768,'text-v1'))
        for field, value in fields.items():
            altered = replace(binding, **{field:value})
            event = self.event(**({field:str(value)} if field in ('installation_id','tenant_id','project_id') else {}))
            self.assertNotEqual(point['id'], s3.build_point(altered,event,self.vector())['id'])
        with self.assertRaises(ValueError):
            s3.build_point(binding,self.event(tenant_id=str(UUID(int=999))),self.vector())

    async def test_s3_update_cas_carries_every_key_and_observed_effect(self):
        binding = self.binding(); server = ProjectionServer()
        async with self.client(server) as client:
            await client.apply_projection(binding,self.event(),self.vector())
            receipt = await client.apply_projection(binding,self.event(2),self.vector(.25))
        self.assertEqual(s3.validate_receipt(receipt,binding,self.event(2)), 'applied')
        writes = [c[2] for c in server.calls if c[0]=='PUT']
        self.assertEqual(writes[1]['update_mode'],'update_only')
        conditions = {c['key']:c['match']['value'] for c in writes[1]['update_filter']['must']}
        for field,value in s3.point_key(binding,UUID(int=10)).items():
            self.assertEqual(conditions[field],value)
        self.assertEqual(conditions['revision'],1)
        self.assertEqual(conditions['effect_sha256'], writes[0]['points'][0]['payload']['effect_sha256'])

    async def test_s3_replay_stale_and_tombstone_preserve_complete_newer_effect(self):
        binding = self.binding(); server = ProjectionServer()
        async with self.client(server) as client:
            await client.apply_projection(binding,self.event(2,True),self.vector())
            tombstone = copy.deepcopy(server.points)
            receipt = await client.apply_projection(binding,self.event(2,True),self.vector())
            self.assertEqual(s3.validate_receipt(receipt,binding,self.event(2,True)),'applied')
            stale = await client.apply_projection(binding,self.event(),self.vector(.25))
            self.assertEqual(s3.validate_receipt(stale,binding,self.event()),'stale')
        self.assertEqual(server.points,tombstone)
        self.assertEqual(stale['observed']['revision'],2)
        self.assertTrue(stale['observed']['tombstone'])

    async def test_s3_equal_revision_different_effect_and_collision_refuse(self):
        binding = self.binding(); server = ProjectionServer()
        async with self.client(server) as client:
            await client.apply_projection(binding,self.event(),self.vector())
            original = copy.deepcopy(server.points)
            await self.apply_refusal(client,binding,self.event(),self.vector(.25))
            self.assertEqual(server.points,original)
            point=next(iter(server.points.values()))
            point['payload']['tenant_id']=str(UUID(int=999))
            count=len(server.calls)
            await self.apply_refusal(client,binding,self.event(2))
            self.assertFalse(any(c[0]=='PUT' for c in server.calls[count:]))

    async def test_s3_destination_race_cannot_overwrite_newer_or_accept_unknown(self):
        binding=self.binding(); server=ProjectionServer()
        async with self.client(server) as client:
            await client.apply_projection(binding,self.event(),self.vector())
            newer=s3.build_point(binding,self.event(3,True),self.vector(.75))
            server.before_write=lambda s:s.points.update({newer['id']:newer})
            receipt=await client.apply_projection(binding,self.event(2),self.vector(.25))
            self.assertEqual(s3.validate_receipt(receipt,binding,self.event(2)),'stale')
            self.assertEqual(server.points[newer['id']],newer)
            server.before_write=lambda s:s.points.clear()
            await self.apply_refusal(client,binding,self.event(4))
            self.assertFalse(server.points)  # update_only must not insert on racing removal

    async def test_s3_lost_ack_is_unavailable_then_replay_reconciles_without_duplicate(self):
        binding=self.binding();server=ProjectionServer('lost_ack')
        async with self.client(server) as client:
            await self.apply_refusal(client,binding,self.event())
            self.assertEqual(len(server.points),1)
            self.assertEqual(sum(c[0]=='PUT' for c in server.calls),1)
            server.fault=None
            receipt=await client.apply_projection(binding,self.event(),self.vector())
            self.assertEqual(s3.validate_receipt(receipt,binding,self.event()),'applied')
        self.assertEqual(len(server.points),1)

    async def test_s3_engine_refusals_and_incomplete_readback_never_receipt(self):
        binding=self.binding()
        for fault in ('ack','denied','unsupported','readback','duplicate','malformed'):
            with self.subTest(fault=fault):
                server=ProjectionServer(fault)
                async with self.client(server) as client:
                    await self.apply_refusal(client,binding,self.event())
                    self.assertFalse(client.status()['module_ready'])

    async def test_s3_receipt_validator_closed_scope_event_and_observation(self):
        binding=self.binding();server=ProjectionServer()
        async with self.client(server) as client:
            receipt=await client.apply_projection(binding,self.event(),self.vector())
        for path,value in [('version',1),('event_id',str(UUID(int=99))),('generation',True),
                           ('revision',True),('stale',True),('effect_sha256','0'*64),
                           ('key.tenant_id',str(UUID(int=99))),('key.model_identity','0'*64),
                           ('observed.revision',True),('observed.effect_sha256','0'*64),
                           ('observed.tombstone',True),('proof','acknowledged')]:
            altered=copy.deepcopy(receipt);target=altered;parts=path.split('.')
            for part in parts[:-1]:target=target[part]
            target[parts[-1]]=value
            with self.subTest(path=path):
                with self.assertRaises(ValueError):s3.validate_receipt(altered,binding,self.event())
        altered=copy.deepcopy(receipt);altered['extra']=True
        with self.assertRaises(ValueError):s3.validate_receipt(altered,binding,self.event())

    async def test_s3_event_and_vector_refuse_before_transport_and_deadline_is_owned(self):
        binding=self.binding(); server=ProjectionServer()
        async with self.client(server) as client:
            for event,vector in [(self.event(aggregate_revision=True),self.vector()),
                    (self.event(payload_sha256='0'*64),self.vector()),
                    (self.event(project_id=str(UUID(int=999))),self.vector()),
                    (self.event(),[float('nan')]+[0.]*767)]:
                await self.apply_refusal(client,binding,event,vector)
            self.assertEqual(server.calls,[])
            caught=None
            try:await client.apply_projection(binding,self.event(),self.vector(),deadline=time.monotonic()-1)
            except Exception as error:caught=error
            self.assertIsInstance(caught,module.ProtocolFailure)
            self.assertEqual(server.calls,[])

class S3CoreTests(unittest.TestCase):
    def consumer(self, binding, sink):
        from cortex_core.modules.consumer import ModuleConsumer
        port = ModuleConsumer(None,b'synthetic',binding.installation_id,binding.project_id,
            binding.module_id,sink,manifest=dict(module_id=binding.module_id,protocol_version=2,
                schema_version=1,aggregate_kinds=['memory'],sink='qdrant'),external_binding=binding)
        self.records=[]
        port._call=lambda *a:False
        port._record=lambda state,event,outcome,error=None,receipt=None:self.records.append((outcome,error,receipt))
        return port

    def test_s3_core_v2_receipt_required_before_success_and_exact_generations(self):
        self.assertIsNotNone(s3)
        from cortex_core.modules.consumer import ConsumerError
        x=S3Tests();binding=x.binding();event=x.event()
        point=s3.build_point(binding,event,x.vector())
        receipt=s3.make_receipt(binding,event,point,point,7)
        sink=SimpleNamespace(apply=lambda *a:receipt)
        caught=None
        try:port=self.consumer(binding,sink)
        except Exception as error:caught=error
        self.assertIsNone(caught,'C07 v2 binding absent')
        self.assertTrue(port._apply_event({'generation':1},event))
        self.assertEqual(self.records,[('applied',None,receipt)])
        for bad in [dict(receipt,generation=2),dict(receipt,proof='acknowledged'),
                    {k:receipt[k] for k in ('event_id','aggregate_id','revision','tombstone','payload_sha256','generation','stale')}]:
            sink.apply=lambda *a:bad
            self.records=[]
            with self.assertRaises(ConsumerError):port._apply_event({'generation':1},event)
            self.assertEqual(self.records,[])
        sink.apply=lambda *a:receipt
        self.records=[]
        with self.assertRaises(ConsumerError):port._apply_event({'generation':2},event)
        self.assertEqual(self.records,[])

    def test_s3_core_ambiguous_effect_never_records_and_scrubs_exception_chain(self):
        self.assertIsNotNone(s3)
        from cortex_core.modules.consumer import ConsumerError
        x=S3Tests();binding=x.binding()
        def fail(*a):raise RuntimeError('SYNTHETIC-PRIVATE')
        caught=None
        try:port=self.consumer(binding,SimpleNamespace(apply=fail))
        except Exception as error:caught=error
        self.assertIsNone(caught,'C07 v2 binding absent')
        with self.assertRaises(ConsumerError) as raised:port._apply_event({'generation':1},x.event())
        self.assertEqual(raised.exception.code,'capability_unavailable')
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertEqual(self.records,[])

    def test_s3_v1_manifest_and_receipt_remain_closed_and_v2_needs_binding(self):
        from cortex_core.modules.protocol import ModuleManifest
        from cortex_core.modules.consumer import ModuleConsumer,ConsumerError
        manifest=dict(module_id='qdrant',protocol_version=2,schema_version=1,aggregate_kinds=['memory'],sink='qdrant')
        caught=None
        try:parsed=ModuleManifest.parse(manifest)
        except Exception as error:caught=error
        self.assertIsNone(caught,'closed v2 manifest absent')
        self.assertEqual(parsed.protocol_version,2)
        with self.assertRaises(ConsumerError):
            ModuleConsumer(None,b'synthetic',UUID(int=1),UUID(int=3),'qdrant',None,manifest=manifest)
        for version,sink in [(1,'qdrant'),(2,'postgres'),(True,'postgres'),(3,'qdrant')]:
            with self.assertRaises(ValueError):ModuleManifest.parse(dict(manifest,protocol_version=version,sink=sink))
        self.assertEqual(ModuleManifest.parse(dict(manifest,protocol_version=1,sink='postgres')).protocol_version,1)

    def test_s3_sync_worker_bridge_owns_client_and_provider_and_replays(self):
        self.assertIsNotNone(s3)
        from cortex_core.modules.vector.consumer_adapter import QdrantSink
        x=S3Tests();binding=x.binding();server=ProjectionServer();clients=[];embeds=[]
        def factory():
            client=x.client(server);clients.append(client);return client
        async def embed(identity,payload):
            embeds.append((identity,payload));return x.vector()
        sink=QdrantSink(binding,factory,embed)
        port=self.consumer(binding,sink)
        self.assertTrue(port._apply_event({'generation':1},x.event()))
        self.assertTrue(port._apply_event({'generation':1},x.event()))
        self.assertEqual(len(server.points),1)
        self.assertEqual(len(clients),2)
        self.assertTrue(all(c._http.is_closed and c.status()['pending_operations']==0 for c in clients))
        self.assertEqual(embeds,[(binding.identity,x.event().payload)]*2)

try:
    from cortex_core.modules.vector import search as s3query
except ImportError:
    s3query=None
from contextlib import asynccontextmanager
from cortex_core.auth import Scope as AuthScope
from cortex_core.runtime.pg_adapters import _C04Search, _RequestMaterial


class QueryProjectionServer(ProjectionServer):
    def __init__(self,fault=None):
        super().__init__(fault);self.query_rows=None;self.on_query=None
    async def __call__(self,request):
        if request.url.path.endswith('/points/query'):
            body=json.loads(request.content)
            self.calls.append((request.method,request.url.path,copy.deepcopy(body),request.headers['api-key']))
            if self.on_query:self.on_query()
            rows=[dict(copy.deepcopy(p),score=.75) for p in self.points.values()] if self.query_rows is None else self.query_rows
            if self.fault=='query_failure':return httpx.Response(500,json={'detail':'SYNTHETIC-PRIVATE'})
            return httpx.Response(200,json={'status':'ok','result':{'points':rows[:body['limit']]}})
        return await super().__call__(request)


class S3QueryTests(unittest.IsolatedAsyncioTestCase):
    client=S2Tests.client
    binding=S3Tests.binding
    event=S3Tests.event
    vector=staticmethod(S3Tests.vector)

    def core(self,binding,points=(),pending=0):
        expected=AuthScope(binding.installation_id,binding.tenant_id,binding.project_id,UUID(int=50),1,'read')
        core=_C04Search(None,_RequestMaterial(b'synthetic',expected));self.core_calls=[]
        targets=[]
        for point in points:
            p=point['payload']
            targets.append(dict(record_id=p['aggregate_id'],kind=p['aggregate_kind'],revision=p['revision'],
                tombstone=p['tombstone'],indexed_revision=p['revision'],event_id=p['event_id'],
                payload_sha256=p['payload_sha256'],effect_sha256=p['effect_sha256'],point_id=point['id'],indexed=True))
        state=dict(binding=s3.binding_contract(binding),indexed_records=len(points),pending_records=pending,targets=targets)
        self.core_state=state;self.core_refused=False
        test=self
        class Conn:
            async def fetchval(self,query,module,ids):
                test.core_calls.append((query,module,ids))
                value=copy.deepcopy(test.core_state)
                value['targets']=[r for r in value['targets'] if UUID(r['record_id']) in ids]
                return value
        @asynccontextmanager
        async def request(subject,**kw):
            if test.core_refused:raise PermissionError('synthetic revoked Core permission')
            if subject!=str(expected.principal_id):raise PermissionError('changed subject')
            yield Conn(),SimpleNamespace(tenant_id=binding.tenant_id,project_id=binding.project_id)
            if test.core_refused:raise PermissionError('synthetic revoked Core permission')
        core._request=request
        return core

    def query(self,core,client,binding):
        self.assertIsNotNone(s3query,'S3 C10 query source absent')
        return s3query.QdrantProjection(binding,client).bind(core)

    async def test_s3_query_filters_full_key_tombstones_and_core_rechecks_freshness(self):
        binding=self.binding();point=s3.build_point(binding,self.event(),self.vector());server=QueryProjectionServer()
        server.points[point['id']]=point
        core=self.core(binding,[point])
        async with self.client(server) as client:
            query=self.query(core,client,binding)
            result=await query.search(str(UUID(int=50)),binding.identity,self.vector(),limit=5)
        self.assertEqual(len(result.hits),1)
        self.assertEqual(result.hits[0].record_id,str(UUID(int=10)))
        self.assertEqual(result.freshness.pending_records,0)
        self.assertEqual(result.freshness.indexed_records,1)
        self.assertGreaterEqual(len(self.core_calls),2)
        body=[c[2] for c in server.calls if c[1].endswith('/query')][0]
        conditions={c['key']:c['match']['value'] for c in body['filter']['must']}
        self.assertEqual(conditions,s3.binding_contract(binding)|{'tombstone':False})

    async def test_s3_query_valid_empty_is_distinct_from_engine_failure(self):
        from cortex_core.embeddings.projection_revision import CapabilityUnavailable
        binding=self.binding();server=QueryProjectionServer();core=self.core(binding)
        async with self.client(server) as client:
            query=self.query(core,client,binding)
            result=await query.search(str(UUID(int=50)),binding.identity,self.vector())
            self.assertEqual(result.hits,())
            server.fault='query_failure'
            with self.assertRaises(CapabilityUnavailable) as raised:
                await query.search(str(UUID(int=50)),binding.identity,self.vector())
            self.assertNotIn('SYNTHETIC-PRIVATE',str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)

    async def test_s3_query_cross_scope_model_stale_tombstone_or_duplicate_cannot_escape(self):
        from cortex_core.embeddings.projection_revision import CapabilityUnavailable
        binding=self.binding();point=s3.build_point(binding,self.event(),self.vector());server=QueryProjectionServer()
        core=self.core(binding,[point])
        async with self.client(server) as client:
            query=self.query(core,client,binding)
            for path,value in [('tenant_id',str(UUID(int=999))),('model_identity','0'*64),('revision',2),('tombstone',True)]:
                bad=copy.deepcopy(point);bad['payload'][path]=value
                server.query_rows=[dict(bad,score=.5)]
                with self.subTest(path=path):
                    with self.assertRaises(CapabilityUnavailable):await query.search(str(UUID(int=50)),binding.identity,self.vector())
            server.query_rows=[dict(point,score=.5),dict(point,score=.5)]
            with self.assertRaises(CapabilityUnavailable):await query.search(str(UUID(int=50)),binding.identity,self.vector())

    async def test_s3_query_core_lag_and_generation_or_permission_change_refuse(self):
        from cortex_core.embeddings.projection_revision import CapabilityUnavailable
        binding=self.binding();point=s3.build_point(binding,self.event(),self.vector());server=QueryProjectionServer()
        server.points[point['id']]=point;core=self.core(binding,[point],pending=1)
        async with self.client(server) as client:
            query=self.query(core,client,binding)
            with self.assertRaises(CapabilityUnavailable):await query.search(str(UUID(int=50)),binding.identity,self.vector())
            self.assertEqual(server.calls,[])
            self.core_state['pending_records']=0
            server.on_query=lambda:self.core_state['targets'][0].update(revision=2,indexed=False)
            with self.assertRaises(CapabilityUnavailable):await query.search(str(UUID(int=50)),binding.identity,self.vector())
            server.on_query=lambda:setattr(self,'core_refused',True)
            with self.assertRaises(PermissionError):await query.search(str(UUID(int=50)),binding.identity,self.vector())

    async def test_s3_projection_revision_requires_core_receipt_and_exact_engine_point(self):
        binding=self.binding();point=s3.build_point(binding,self.event(),self.vector());server=QueryProjectionServer()
        server.points[point['id']]=point;core=self.core(binding,[point]);checks=[]
        async def recheck(subject,scope):checks.append(subject);return True
        target=dict(record_id=UUID(int=10),revision=1)
        async with self.client(server) as client:
            query=self.query(core,client,binding)
            observed=await query.projection_revision(str(UUID(int=50)),target,binding.identity,recheck=recheck)
            self.assertEqual(observed.state,'visible')
            server.points.clear()
            observed=await query.projection_revision(str(UUID(int=50)),target,binding.identity,recheck=recheck)
            self.assertEqual(observed.state,'unavailable')
            self.assertEqual(len(checks),2)
            self.core_state['targets'][0]['indexed']=False
            observed=await query.projection_revision(str(UUID(int=50)),target,binding.identity,recheck=recheck)
            self.assertEqual(observed.state,'pending')

    async def test_s3_c11_source_binding_never_reports_qdrant_ready_or_uses_pg_miss(self):
        self.assertIsNotNone(s3query,'S3 C11 binding source absent')
        from cortex_core.api.c11b2 import C11b2ReadPort
        binding=self.binding();server=QueryProjectionServer()
        principal=dict(principal_id=str(UUID(int=50)),tenant_id=str(binding.tenant_id),project_id=str(binding.project_id),permission_generation=1)
        class Records:
            installation_id=binding.installation_id
            projects={'synthetic':binding.project_id}
            def _project(self,scope):return binding.project_id
            async def principal(self,scope):return dict(principal)
        async with self.client(server) as client:
            caught=None
            try:
                port=C11b2ReadPort(Records(),None,binding.identity,SimpleNamespace(embed=lambda *a:None),
                    'synthetic',{},vector_projection=s3query.QdrantProjection(binding,client))
            except Exception as error:caught=error
            self.assertIsNone(caught,'explicit C11 vector source binding absent')
            p=principal|{'_request_scope':{'headers':[(b'authorization',b'Bearer synthetic')]}}
            search,_=port._ports(p)
            self.assertIsInstance(search,s3query.QdrantSearch)
            self.assertEqual(await port.capability_source('pg_search',p),{'state':'unavailable','reason':'qdrant_native_unbound'})
            self.assertEqual(server.calls,[])
