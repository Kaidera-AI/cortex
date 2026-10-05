"""Parser refusals obey the same unavailable and caller-data secrecy contract."""
import io

import pytest

from cortex_v2.cli import agent_request as bridge


@pytest.mark.parametrize('argv', [
    ['api', 'GET', '/health', '--connect-to', 'https://unissued-marker@foreign.example'],
    ['api', 'GET', '/projects', '--header', 'Bearer unissued-marker'],
    ['https://unissued-marker@foreign.example/unknown-command'],
    [],
])
def test_parser_errors_are_safe_unavailable_before_profile(monkeypatch, argv):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid caller options must not construct a profile or send HTTP')
    monkeypatch.setattr(bridge, 'load_member_profile', forbidden)
    monkeypatch.setattr(bridge, 'http_request', forbidden)
    stdout, stderr = io.StringIO(), io.StringIO()
    result = bridge.main(argv, stdin=io.StringIO(), stdout=stdout, stderr=stderr)
    assert result == 2 and stdout.getvalue() == ''
    assert stderr.getvalue() == 'ERROR: facade request unavailable in this release\n'
    assert 'unissued-marker' not in stderr.getvalue() and 'foreign.example' not in stderr.getvalue()
