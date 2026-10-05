"""R96 negative boot/context contract; missing mappings never look like success."""
import io
import subprocess
from pathlib import Path

import pytest

from cortex_v2.cli import agent_request as bridge

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ('/health', '/projects/fixture-project', '/roster', '/agents', '/skills',
          '/projects/fixture-project/runtime', '/state')
UNAVAILABLE = 'facade request unavailable in this release'


@pytest.mark.parametrize('path', ROUTES)
def test_unmapped_context_is_explicitly_unavailable_before_profile_or_key(monkeypatch, path):
    def forbidden(*args, **kwargs):
        pytest.fail('an unmapped route must not construct a profile or send HTTP')
    monkeypatch.setattr(bridge, 'load_member_profile', forbidden)
    monkeypatch.setattr(bridge, 'http_request', forbidden)
    stdout, stderr = io.StringIO(), io.StringIO()
    result = bridge.main(['api', 'GET', path], stdin=io.StringIO(), stdout=stdout, stderr=stderr)
    assert result == 2 and stdout.getvalue() == ''
    assert UNAVAILABLE in stderr.getvalue()
    assert 'enrollment' not in stderr.getvalue() and 'Bearer' not in stderr.getvalue()


@pytest.mark.parametrize('path', ROUTES)
def test_release_shell_helper_has_the_same_unavailable_contract(path):
    helper = ROOT / 'scripts/agent-shims/_cortex_api.sh'
    result = subprocess.run(['bash', '-c', 'source "$1"; cortex_api_call GET "$2"',
                             'fixture', str(helper), path], capture_output=True, text=True)
    assert result.returncode == 2 and result.stdout == ''
    assert UNAVAILABLE in result.stderr and 'No such file' not in result.stderr


def test_unavailable_never_echoes_request_supplied_credentials_or_url(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('unmapped caller URL must refuse before profile construction')
    monkeypatch.setattr(bridge, 'load_member_profile', forbidden)
    stdout, stderr = io.StringIO(), io.StringIO()
    result = bridge.main(['api', 'GET', 'https://unissued-marker@foreign.example/roster'],
                         stdin=io.StringIO(), stdout=stdout, stderr=stderr)
    assert result == 2 and stdout.getvalue() == '' and UNAVAILABLE in stderr.getvalue()
    assert 'unissued-marker' not in stderr.getvalue() and 'foreign.example' not in stderr.getvalue()
