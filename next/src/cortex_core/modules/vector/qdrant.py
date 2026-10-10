"""Inert C09 source profile: injected fake protocol, no socket or module binding."""
import copy
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re

DESCRIPTOR_SHA256 = '8fc20cabdf88efeda64c79ebce98f98b593eaddc04cd2afb28f7ff2e3d898f33'
MAX_RESPONSE_BYTES = 1048576


class ProtocolFailure(RuntimeError):
    code = 'capability_unavailable'

    def __init__(self, reason, *, primary_reason=None, cleanup_reason=None):
        super().__init__(reason)
        self.reason = reason
        self.primary_reason = primary_reason
        self.cleanup_reason = cleanup_reason


def pinned_descriptor():
    raw = (Path(__file__).resolve().parents[4] / 'modules/qdrant/descriptor.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != DESCRIPTOR_SHA256:
        raise ValueError('bundled_descriptor_refused')
    return json.loads(raw)


def admit_descriptor(value, platform):
    try:
        raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
        valid = (type(value) is dict and len(raw) <= 16384
                 and hashlib.sha256(raw).hexdigest() == DESCRIPTOR_SHA256
                 and type(platform) is str and platform in value['platforms'])
    except (TypeError, ValueError, KeyError):
        valid = False
    if not valid:
        raise ValueError('descriptor_refused')
    return copy.deepcopy(value)


def private_config(address, *, writer_key_ref='qdrant/admin', reader_key_ref='qdrant/read',
                   published_ports=(), host_network=False, uid=10001):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError('private_interface_required') from None
    networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7')
    if not any(ip in ipaddress.ip_network(network) for network in networks):
        raise ValueError('private_interface_required')
    refs = (writer_key_ref, reader_key_ref)
    if (any(type(v) is not str or not re.fullmatch(r'[a-z][a-z0-9_/-]{0,127}', v)
            or '..' in v for v in refs) or writer_key_ref == reader_key_ref
            or published_ports or host_network is not False or type(uid) is not int or uid <= 0):
        raise ValueError('private_delivery_refused')
    return {'service': {'host': address, 'http_port': 6333, 'grpc_port': None, 'enable_cors': False,
                        'enable_tls': False, 'enable_snapshot_url_recovery': False},
            'cluster': {'enabled': False}, 'telemetry_disabled': True,
            'delivery': {'network': 'internal', 'published_ports': list(published_ports),
                         'host_network': host_network, 'uid': uid, 'read_only_root': True,
                         'cap_drop': ['ALL'], 'no_new_privileges': True},
            'secret_refs': {'writer': writer_key_ref, 'reader': reader_key_ref}}


class FakeProtocol:
    """Caller supplies an in-memory handler; this profile supplies no network adapter."""
    def __init__(self, handler):
        if not callable(handler):
            raise ValueError('injected_fake_handler_required')
        self.handler = handler

    def request(self, method, path, body=None, role='reader'):
        return self.handler(method, path, body, role)


class InertPort:
    def __init__(self, descriptor, platform, config, *, transport):
        self.descriptor = admit_descriptor(descriptor, platform)
        if type(transport) is not FakeProtocol:
            raise ValueError('injected_fake_protocol_required')
        try:
            expected = private_config(config['service']['host'],
                writer_key_ref=config['secret_refs']['writer'], reader_key_ref=config['secret_refs']['reader'],
                published_ports=config['delivery']['published_ports'], host_network=config['delivery']['host_network'],
                uid=config['delivery']['uid'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('private_configuration_refused') from None
        if (type(config) is not dict
                or json.dumps(config, sort_keys=True) != json.dumps(expected, sort_keys=True)):
            raise ValueError('private_configuration_refused')
        self.config, self.transport = copy.deepcopy(config), transport

    def status(self):
        return {'state': 'unbound', 'module_ready': False, 'core_auth_bound': False,
                'transport_profile': 'FAKE', 'engine_decision': 'UNDECIDED'}

    def _call(self, method, path, body=None, role='reader'):
        try:
            response = self.transport.request(method, path, body, role)
        except Exception:
            raise ProtocolFailure('fake_protocol_failed') from None
        if (type(response) is not dict or set(response) != {'status', 'body'}
                or type(response['status']) is not int or response['status'] != 200
                or type(response['body']) is not bytes or len(response['body']) > MAX_RESPONSE_BYTES):
            raise ProtocolFailure('fake_response_refused')
        try:
            value = json.loads(response['body'])
        except (ValueError, UnicodeError):
            raise ProtocolFailure('fake_response_refused') from None
        if type(value) is not dict:
            raise ProtocolFailure('fake_response_refused')
        return value

    @staticmethod
    def _point_matches(actual, expected):
        if type(actual) is not dict or type(actual.get('id')) is not int or actual['id'] != expected['id']:
            return False
        payload = actual.get('payload')
        if (type(payload) is not dict or set(payload) != set(expected['payload'])
                or any(type(payload[key]) is not type(value) or payload[key] != value
                       for key, value in expected['payload'].items())):
            return False
        vector = actual.get('vector')
        return (type(vector) is list and len(vector) == len(expected['vector'])
                and all(type(value) in (int, float) and math.isfinite(value) and value == wanted
                        for value, wanted in zip(vector, expected['vector'])))

    def probe(self):
        """Scripted synthetic API witness, never registration/readiness or real authority."""
        verified = []
        value = self._call('GET', '/')
        if value.get('version') != self.descriptor['version']:
            raise ProtocolFailure('engine_version_refused')
        verified.append('version')
        collection = '/collections/c09-probe'
        config = {'vectors': {'size': 768, 'distance': 'Dot'}}
        created = False
        primary = None
        try:
            created = True  # A lost acknowledgement may follow the fake effect.
            if self._call('PUT', collection, config, 'writer').get('result') is not True:
                raise ProtocolFailure('collection_refused')
            info = self._call('GET', collection)
            if info.get('result', {}).get('config', {}).get('params', {}).get('vectors') != config['vectors']:
                raise ProtocolFailure('collection_refused')
            verified.append('collection')
            schemas = {'tenant': 'keyword', 'project': 'keyword', 'generation': 'keyword',
                       'revision': 'integer', 'deleted': 'bool'}
            for field, kind in schemas.items():
                if self._call('PUT', collection+'/index', {'field_name': field, 'field_schema': kind}, 'writer').get('result') is not True:
                    raise ProtocolFailure('index_refused')
            info = self._call('GET', collection)
            schema = info.get('result', {}).get('payload_schema', {})
            if any(schema.get(field, {}).get('data_type') != kind for field, kind in schemas.items()):
                raise ProtocolFailure('index_refused')
            verified.append('indexes')
            points = []
            for identity in (1, 2, 3):
                vector = [0.0] * 768; vector[(identity-1) % 2] = 1.0
                points.append({'id': identity, 'vector': vector,
                    'payload': {'tenant': 'fake-other' if identity == 3 else 'fake-tenant',
                                'project': 'fake-project', 'generation': 'fake-generation',
                                'revision': 1, 'deleted': False}})
            if self._call('PUT', collection+'/points', {'points': points}, 'writer').get('result') is not True:
                raise ProtocolFailure('upsert_refused')
            verified.append('upsert')
            if self._call('POST', collection+'/points/count', {'exact': True}).get('result', {}).get('count') != 3:
                raise ProtocolFailure('count_refused')
            values = self._call('POST', collection+'/points', {'ids': [1, 2, 3], 'with_vector': True, 'with_payload': True}).get('result')
            if (type(values) is not list or len(values) != len(points)
                    or not all(self._point_matches(value, expected)
                               for value, expected in zip(sorted(values, key=lambda row: row['id']), points))):
                raise ProtocolFailure('readback_refused')
            verified.append('readback')
            conditions = [{'key': field, 'match': {'value': value}}
                          for field, value in [('tenant', 'fake-tenant'), ('project', 'fake-project'),
                                              ('generation', 'fake-generation'), ('deleted', False)]]
            values = self._call('POST', collection+'/points/query', {'query': points[0]['vector'],
                'filter': {'must': conditions}, 'limit': 1, 'with_payload': True, 'with_vector': True}).get('result', {}).get('points')
            if type(values) is not list or len(values) != 1 or not self._point_matches(values[0], points[0]):
                raise ProtocolFailure('filter_refused')
            verified.append('filtered_query')
            if self._call('POST', collection+'/points/delete', {'points': [2]}, 'writer').get('result') is not True:
                raise ProtocolFailure('delete_refused')
            if self._call('POST', collection+'/points/count', {'exact': True}).get('result', {}).get('count') != 2:
                raise ProtocolFailure('delete_refused')
            verified.append('delete')
            action = {'actions': [{'create_alias': {'collection_name': 'c09-probe', 'alias_name': 'c09-probe-alias'}}]}
            if self._call('POST', '/collections/aliases', action, 'writer').get('result') is not True:
                raise ProtocolFailure('alias_refused')
            expected = [{'collection_name': 'c09-probe', 'alias_name': 'c09-probe-alias'}]
            if self._call('GET', '/aliases').get('result', {}).get('aliases') != expected:
                raise ProtocolFailure('alias_refused')
            verified.append('aliases')
        except Exception as error:
            primary = error
        finally:
            if created:
                try:
                    if self._call('DELETE', collection, role='writer').get('result') is not True:
                        raise ProtocolFailure('cleanup_refused')
                    verified.append('cleanup')
                except Exception:
                    raise ProtocolFailure('fake_cleanup_unverified',
                        primary_reason=primary.reason if isinstance(primary, ProtocolFailure) else
                                       'fake_protocol_refused' if primary is not None else None,
                        cleanup_reason='fake_cleanup_unverified') from None
        if primary is not None:
            if isinstance(primary, ProtocolFailure):
                raise primary
            raise ProtocolFailure('fake_protocol_refused') from None
        return {**self.status(), 'version': value['version'], 'verified_operations': verified}
