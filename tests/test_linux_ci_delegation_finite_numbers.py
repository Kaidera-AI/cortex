"""FA1: the actual intercepted CLI refuses exponent overflow in either engine read."""
import json
import sys

import pytest

from test_linux_ci_delegation_readonly import fixture


@pytest.mark.parametrize('stage', ['engine_info', 'engine_version'])
@pytest.mark.parametrize('number', ['1e999', '-1e999', '{"nested":[1e999]}', '{"nested":[-1e999]}', '1e308'])
def test_actual_cli_rejects_nonfinite_parsed_numbers_without_publishing_raw_fields(stage, number, tmp_path, monkeypatch, capsys):
    module, calls, original = fixture(monkeypatch)
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) == (2 if stage == 'engine_info' else 3):
            result['stdout'] = b'{"ignored_number":' + number.encode() + b',' + result['stdout'][1:]
        return result
    monkeypatch.setattr(module, 'capture_command', capture)
    output = tmp_path / 'public'; source = 'f' * 40
    monkeypatch.setattr(sys, 'argv', ['linux_ci_delegation.py', '--read-only', '--source-sha', source, '--output', str(output)])
    if number == '1e308':
        module.main()
    else:
        with pytest.raises(SystemExit) as error:
            module.main()
        assert error.value.code == 1
    value = json.loads((output / 'delegation.json').read_bytes())
    streams = capsys.readouterr(); public = streams.out if number == '1e308' else streams.err
    assert value == json.loads(public) and value['source_sha'] == source
    assert value['status'] == ('ready' if number == '1e308' else 'refused')
    if number != '1e308':
        assert value['reason'] == 'effective_cpu_memory_pids_required'
    assert 'ignored_number' not in public and 'Traceback' not in public
    assert len(calls) == 3 and (output / 'delegation.json').stat().st_mode & 0o777 == 0o600
