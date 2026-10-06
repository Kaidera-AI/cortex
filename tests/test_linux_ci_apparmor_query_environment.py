"""Live native guard and actual query CLI under a stripped public child environment."""
import json
import subprocess
import sys

import pytest

from test_linux_ci_apparmor_userns import PATH, REAL

CASES = [
    ('query-missing', 'query', 'Linux', 'x86_64', 0, None, False, True),
    ('query-false', 'query', 'Linux', 'x86_64', 0, 'false', False, True),
    ('query-true', 'query', 'Linux', 'x86_64', 0, 'true', False, True),
    ('query-os', 'query', 'Darwin', 'x86_64', 0, 'true', False, False),
    ('query-arch', 'query', 'Linux', 'aarch64', 0, 'true', False, False),
    ('query-nonroot', 'query', 'Linux', 'x86_64', 1001, 'true', False, False),
    ('query-extra', 'query', 'Linux', 'x86_64', 0, 'true', True, False),
    ('parent-missing', 'parent', 'Linux', 'x86_64', 1001, None, False, False),
    ('parent-false', 'parent', 'Linux', 'x86_64', 1001, 'false', False, False),
    ('parent-true', 'parent', 'Linux', 'x86_64', 1001, 'true', False, True),
]

DRIVER = '''
import json, sys
from pathlib import Path
options = json.loads(sys.argv[1])
helper = Path(options['helper'])
sys.path.insert(0, str(helper.parent))
import linux_ci_apparmor as product
import linux_ci_diagnostics as diagnostics
# Keep native_context live. Only the platform and effective UID are fixtures.
diagnostics.platform.system = lambda: options['system']
diagnostics.platform.machine = lambda: options['machine']
product.os.geteuid = lambda: options['uid']
events = Path(options['events'])
def event(name):
    with events.open('a') as stream: stream.write(name + '\\n')
original = product.profile_coverage
def coverage(path):
    event('coverage')
    return original(path, policy_root=Path(options['policy']))
product.profile_coverage = coverage
def prepare(**kwargs):
    event('prepare')
    return {'status': 'ready', 'steps': [], 'profile_loaded': False}
product.prepare_userns = prepare
args = ['--profiles-for-path', options['real']] if options['mode'] == 'query' else []
if options['extra'] or options['mode'] == 'parent':
    args += ['--source-sha', 'd' * 40, '--output', options['output']]
sys.argv = [str(helper), *args]
product.main()
'''


@pytest.mark.parametrize('case,mode,system,machine,uid,flag,extra,accepted', CASES)
def test_readonly_query_uses_root_platform_and_parent_keeps_ci_admission(case, mode, system, machine, uid, flag, extra, accepted, tmp_path):
    policy = tmp_path / 'profiles'; policy.mkdir(); profile = policy / 'p0'; profile.mkdir()
    attachment = profile / 'attach'; attachment.write_text('podman\n')
    before = attachment.read_bytes()
    driver = tmp_path / 'driver.py'; driver.write_text(DRIVER)
    events = tmp_path / 'events'; output = tmp_path / 'output'
    options = {'helper': str(PATH), 'real': REAL, 'policy': str(policy), 'events': str(events),
               'output': str(output), 'mode': mode, 'system': system, 'machine': machine,
               'uid': uid, 'extra': extra}
    environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
                   'PYTHONPYCACHEPREFIX': str(tmp_path / 'pycache')}
    if flag is not None: environment['GITHUB_ACTIONS'] = flag
    result = subprocess.run([sys.executable, str(driver), json.dumps(options)],
                            env=environment, capture_output=True, timeout=10)
    calls = events.read_text().splitlines() if events.exists() else []
    if accepted:
        assert result.returncode == 0, 'permitted read-only root query lost public environment admission'
        assert result.stderr == b''
        if mode == 'query':
            assert json.loads(result.stdout) == {'covered': False, 'profile_count': 1}
            assert calls == ['coverage'] and not output.exists()
        else:
            assert calls == ['prepare']
            value = json.loads(result.stdout)
            assert value['status'] == 'ready' and value['source_sha'] == 'd' * 40
            assert json.loads((output / 'apparmor.json').read_bytes()) == value
    else:
        assert result.returncode != 0, 'nonroot or unsupported query was admitted'
        assert calls == [] and not output.exists()
    assert attachment.read_bytes() == before
    assert not any(call in ('parser', 'engine') for call in calls)
