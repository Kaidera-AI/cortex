"""Real finite private host subprocess boundaries; no engine or DB fixture."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import secrets
import sys
import time

import pytest


def product():
    path = Path(__file__).parents[1] / 'src/cortex_v2/clients/linux_provisioning.py'
    assert path.is_file(), 'accepted Linux finite host process port is absent'
    return importlib.import_module('cortex_v2.clients.linux_provisioning')


def command(script):
    return [sys.executable, '-c', script]


@pytest.mark.parametrize('value', [
    {'ok': True}, {'data': [1, 2, 3]}, {'string': 'unicode \u2603'},
    {'mode': 'payload', 'files': {}}, {'nested': {'a': None}},
], ids=['object', 'array-value', 'utf8-value', 'payload-object', 'nested-object'])
def test_success_is_exactly_one_bounded_json_line_and_empty_stderr(value):
    module = product()
    result = module.private_json_command(command('import json,sys; v=json.load(sys.stdin); print(json.dumps(v))'), value, timeout=2)
    assert result == value


@pytest.mark.parametrize('stdout,stderr,status', [
    (b'', b'', 0),
    (b'{"ok":true}', b'', 0),
    (b' {"ok":true}\n', b'', 0),
    (b'{"ok":true}\n\n', b'', 0),
    (b'{"ok":true}\n{}\n', b'', 0),
    (b'{"x":1,"x":2}\n', b'', 0),
    (b'{"x":NaN}\n', b'', 0),
    (b'{"x":Infinity}\n', b'', 0),
    (b'{"x":"\xff"}\n', b'', 0),
    (b'[]\n', b'', 0),
    (b'null\n', b'', 0),
    (b'{"ok":true}\n', b'unexpected warning\n', 0),
    (b'{"ok":true}\n', b'', 1),
    (b'{"ok":true}\n', b'', 2),
    (b'{"ok":true}\n', b'', 3),
    (b'x' * 65537, b'', 0),
    (b'', b'x' * 4097, 2),
], ids=['empty', 'no-lf', 'prefix-space', 'extra-lf', 'two-lines', 'duplicate-key',
        'nan', 'infinity', 'invalid-utf8', 'array', 'null', 'warning',
        'exit-one', 'exit-two-with-output', 'exit-three', 'stdout-overflow', 'stderr-overflow'])
def test_literal_protocol_failures_are_safe_refusals(stdout, stderr, status):
    module = product()
    script = 'import os; os.write(1,' + repr(stdout) + '); os.write(2,' + repr(stderr) + '); raise SystemExit(' + str(status) + ')'
    with pytest.raises(module.ProvisionRefusal) as error:
        module.private_json_command(command(script), {'mode': 'payload'}, timeout=2)
    assert error.value.code == 'cortex_health_unavailable'


@pytest.mark.parametrize('code', ['cortex_health_degraded', 'cortex_credential_refused', 'cortex_provisioning_reissue_required'])
def test_typed_refusal_is_validated_but_child_message_is_discarded(code):
    module = product()
    secret = secrets.token_urlsafe(32)
    script = 'import json,sys; value=json.load(sys.stdin); print(json.dumps({"code":value["code"],"safe_message":value["private"]}),file=sys.stderr); raise SystemExit(2)'
    with pytest.raises(module.ProvisionRefusal) as error:
        module.private_json_command(command(script), {'code': code, 'private': secret}, timeout=2)
    assert error.value.code == code
    assert bool(secret not in str(error.value))
    assert bool(secret not in json.dumps(error.value.public()))


@pytest.mark.parametrize('frame', [[], {'bad': float('nan')}, {'large': 'x' * 65536}], ids=['array', 'nan', 'oversize'])
def test_invalid_private_input_refuses_before_process_creation(frame, tmp_path):
    module = product()
    marker = tmp_path / 'never-started'
    with pytest.raises(module.ProvisionRefusal) as error:
        module.private_json_command(command('from pathlib import Path; Path(' + repr(str(marker)) + ').touch()'), frame, timeout=2)
    assert error.value.code == 'cortex_provisioning_setup_required'
    assert not marker.exists()


@pytest.mark.parametrize('script,frame', [
    ('import time; time.sleep(5)', {'mode': 'payload'}),
    ('import sys,time; print("{}",flush=True); time.sleep(5)', {'mode': 'payload'}),
    ('import time; time.sleep(5)', {'private': 'x' * 64000}),
    ('import os,time; child=os.fork(); time.sleep(5) if child==0 else None; os._exit(0)', {'mode': 'payload'}),
])
def test_open_input_output_or_inherited_child_pipes_obey_one_deadline(script, frame):
    module = product()
    started = time.monotonic()
    with pytest.raises(module.ProvisionRefusal) as error:
        module.private_json_command(command(script), frame, timeout=.15)
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert error.value.code == 'cortex_health_unavailable'


def test_owned_process_group_is_gone_after_deadline(tmp_path):
    module = product()
    pidfile = tmp_path / 'parent-pid'
    script = 'import os,time; open(' + repr(str(pidfile)) + ',"w").write(str(os.getpid())); time.sleep(5)'
    with pytest.raises(module.ProvisionRefusal):
        module.private_json_command(command(script), {}, timeout=.3)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_child_does_not_inherit_ambient_credentials_or_network_overrides(monkeypatch):
    module = product()
    names = ['CORTEX_API_KEY', 'CORTEX_DATABASE_URL', 'DATABASE_URL', 'PGPASSWORD',
             'PGPASSFILE', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'PYTHONPATH',
             'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH']
    for name in names: monkeypatch.setenv(name, 'private-ambient-fixture')
    script = 'import json,os; names=' + repr(names) + '; print(json.dumps({"present":[n for n in names if n in os.environ]}))'
    value = module.private_json_command(command(script), {}, timeout=2)
    assert value == {'present': []}


def test_command_is_an_argument_array_with_absolute_executable(tmp_path):
    module = product()
    for invalid in ['echo unsafe', ['python', '-c', 'print("{}")'], [], [sys.executable, '\x00']]:
        with pytest.raises(module.ProvisionRefusal) as error:
            module.private_json_command(invalid, {}, timeout=2)
        assert error.value.code == 'cortex_provisioning_setup_required'


def test_private_stdin_stays_out_of_argv_environment_and_error_message():
    module = product()
    secret = secrets.token_urlsafe(32)
    script = 'import json,os,sys; frame=json.load(sys.stdin); secret=frame["credential"]; print(json.dumps({"in_argv":any(secret in a for a in sys.argv),"in_env":any(secret in v for v in os.environ.values())}))'
    value = module.private_json_command(command(script), {'credential': secret}, timeout=2)
    assert value == {'in_argv': False, 'in_env': False}


def test_nonexistent_command_is_a_safe_transport_refusal(tmp_path):
    module = product()
    with pytest.raises(module.ProvisionRefusal) as error:
        module.private_json_command([str(tmp_path / 'no-executable')], {}, timeout=2)
    assert error.value.code == 'cortex_health_unavailable'
