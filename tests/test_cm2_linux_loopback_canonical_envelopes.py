"""R272: automatically validate the actual simulated API response fixtures."""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import test_cm2_linux_loopback_transport as scenario


def test_every_simulated_admission_response_matches_its_canonical_envelope(tmp_path, monkeypatch):
    home = Path(__file__).parent / 'fixtures/cm2-revision4/fixtures'
    connection = json.loads((home / 'connection.linux.json').read_text())
    canonical = {
        '/health/ready': {'status': 'ready'},
        '/v1/auth/principal': json.loads((home / 'principal.linux.json').read_text()),
        '/v1/scopes/' + connection['project'] + '/roster': json.loads((home / 'roster.linux.json').read_text()),
    }
    state = {}; origin = 'http://127.0.0.1:18612'

    @contextmanager
    def validate_endpoint(routes):
        assert set(routes) == set(canonical)
        for path, expected in canonical.items():
            status, raw, extra, delay = routes[path]
            assert status == 200 and extra == {} and delay == 0
            assert json.loads(raw) == expected, 'simulated response is not the canonical API envelope'
        state['routes'] = routes; state['calls'] = []
        yield origin, state['calls']

    class ProtocolBoundary:
        # This regression measures scenario inputs, independently of whether
        # the production transport exists. The scenario's HTTP tests remain.
        def __init__(self, observed): assert observed == origin
        def get(self, url, *, headers, timeout, max_bytes):
            assert url.startswith(origin + '/')
            path = url[len(origin):]
            state['calls'].append((path, dict(headers)))
            return state['routes'][path][:2]

    monkeypatch.setattr(scenario, 'endpoint', validate_endpoint)
    monkeypatch.setattr(scenario, 'product', lambda: SimpleNamespace(LoopbackTransport=ProtocolBoundary))
    scenario.test_actual_transport_drives_existing_member_health_principal_and_roster_admission(tmp_path)
    assert len(state['calls']) == 3
