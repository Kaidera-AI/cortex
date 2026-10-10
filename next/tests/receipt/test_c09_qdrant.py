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
