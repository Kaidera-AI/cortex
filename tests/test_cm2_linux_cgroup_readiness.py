"""R277 customer preflight: fixed controller proof and safe finite refusal."""
import importlib
import json
import secrets
import sys

import pytest

CODE = 'cortex_cgroup_delegation_unavailable'
FIX = 'Enable systemd delegation of cpu, memory and pids for the kos user; see INSTALL-linux.md.'


@pytest.mark.parametrize('case', ['valid', 'superset', 'cpu', 'memory', 'pids', 'v1',
    'rootful', 'missing', 'duplicate', 'unknown', 'invalid-rootless', 'malformed', 'oversize'])
def test_actual_linux_cgroup_observation_has_fixed_readiness_or_a_typed_fix(case):
    module = importlib.import_module('cortex_v2.clients.native_prerequisite')
    assert hasattr(module, 'check_linux_delegation'), 'accepted customer controller preflight is absent'
    private = secrets.token_urlsafe(32)
    value = {'host': {'cgroupVersion': 'v2', 'cgroupControllers': ['pids', 'cpu', 'memory'],
                     'security': {'rootless': True}}, 'unused': private}
    if case in ('cpu', 'memory', 'pids'): value['host']['cgroupControllers'].remove(case)
    elif case == 'superset': value['host']['cgroupControllers'] += ['io', 'cpuset']
    elif case == 'v1': value['host']['cgroupVersion'] = 'v1'
    elif case == 'rootful': value['host']['security']['rootless'] = False
    elif case == 'missing': del value['host']['cgroupControllers']
    elif case == 'duplicate': value['host']['cgroupControllers'].append('cpu')
    elif case == 'unknown': value['host']['cgroupControllers'].append(private)
    elif case == 'invalid-rootless': value['host']['security']['rootless'] = 1
    raw = json.dumps(value).encode()
    if case == 'malformed': raw = b'not JSON'
    elif case == 'oversize': raw = b'x' * 65537
    if case in ('valid', 'superset'):
        proof = module.check_linux_delegation(raw)
        assert proof == {'schema': 'cortex.linux-cgroup-readiness.v1', 'rootless': True,
                         'cgroup_version': 'v2', 'cgroup_controllers': ['cpu', 'memory', 'pids']}
        assert private not in json.dumps(proof)
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: module.check_linux_delegation(raw)
        assert error.value.code == CODE and FIX in str(error.value)
        public = error.value.public()
        assert public['safe_message'] == FIX and public['install_guide'] == 'INSTALL-linux.md'
        assert private not in str(error.value) and private not in json.dumps(public)


def test_actual_finite_child_refusal_preserves_typed_fix_and_discards_child_text():
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    private = secrets.token_urlsafe(32)
    script = ('import json,sys; v=json.load(sys.stdin); '
              'print(json.dumps({"code":v["code"],"safe_message":v["private"]}),file=sys.stderr); '
              'raise SystemExit(2)')
    with pytest.raises(module.ProvisionRefusal) as error:
        module.private_json_command([sys.executable, '-c', script],
                                    {'code': CODE, 'private': private}, timeout=2)
    assert error.value.code == CODE and FIX in str(error.value)
    assert error.value.public()['safe_message'] == FIX
    assert private not in str(error.value) and private not in json.dumps(error.value.public())
