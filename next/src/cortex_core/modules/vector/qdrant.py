"""Inert C09 source profile: injected fake protocol, no socket or module binding."""
import json
from pathlib import Path

DESCRIPTOR_SHA256 = '8fc20cabdf88efeda64c79ebce98f98b593eaddc04cd2afb28f7ff2e3d898f33'
MAX_RESPONSE_BYTES = 1048576


class ProtocolFailure(RuntimeError):
    code = 'capability_unavailable'


def pinned_descriptor():
    return json.loads((Path(__file__).resolve().parents[4] / 'modules/qdrant/descriptor.json').read_text())


def admit_descriptor(value, platform):
    return value


def private_config(address, *, writer_key_ref='qdrant/admin', reader_key_ref='qdrant/read',
                   published_ports=(), host_network=False, uid=10001):
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
        self.handler = handler

    def request(self, method, path, body=None, role='reader'):
        return self.handler(method, path, body, role)


class InertPort:
    def __init__(self, descriptor, platform, config, *, transport):
        self.descriptor = admit_descriptor(descriptor, platform)
        self.config, self.transport = config, transport

    def status(self):
        return {'state': 'unbound', 'module_ready': False, 'core_auth_bound': False,
                'transport_profile': 'FAKE', 'engine_decision': 'UNDECIDED'}

    def probe(self):
        response = self.transport.request('GET', '/', None, 'reader')
        value = json.loads(response['body'])
        return {**self.status(), 'version': value.get('version'), 'verified_operations': []}
