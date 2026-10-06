"""Literal private pipe and shutdown bounds; synthetic credentials, no engine."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_cm2_linux_native_adapter import native_fixture, product


def child(code, raw, *, keep_input_open=False):
    env = dict(os.environ)
    for name in list(env):
        if name.startswith('CORTEX_') or name in ('DATABASE_URL', 'PGPASSWORD', 'PGPASSFILE'):
            env.pop(name)
    env['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
    program = subprocess.Popen([sys.executable, '-B', '-c', code], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    started = time.monotonic()
    if raw:
        program.stdin.write(raw)
        program.stdin.flush()
    if not keep_input_open:
        program.stdin.close()
    timed_out = False
    try:
        program.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        timed_out = True
        program.kill()
        program.wait(timeout=1)
    finally:
        program.stdin.close()
    out, err = program.stdout.read(), program.stderr.read()
    return program.returncode, out, err, timed_out, time.monotonic() - started


MAIN = '''
import cortex_v2.native_prerequisite as p
p.COMMAND_TIMEOUT = 0.15
async def fake(value):
    return {'status':'synthetic-ready'}
p.execute_private = fake
raise SystemExit(p.main())
'''


@pytest.mark.parametrize('raw', [b'', b'{', b'{"mode":"payload"}\n'])
def test_open_input_pipe_cannot_block_finite_command(raw):
    code, out, err, timeout, seconds = child(MAIN, raw, keep_input_open=True)
    assert not timeout, 'private input has no deadline while peer retains its pipe'
    assert code == 2 and out == b'' and 0 < len(err) <= 4096
    value = json.loads(err)
    assert value['code'] == 'cortex_health_unavailable'
    assert seconds < 1.5


@pytest.mark.parametrize('raw', [b'', b'[]', b'{"a":1,"a":2}', b'{"value":NaN}',
                               b'\xff', b'{}{}', b' ' * 65537])
def test_private_input_refusals_are_one_safe_json_frame(raw):
    code, out, err, timeout, _ = child(MAIN, raw)
    assert not timeout and code == 2 and out == b''
    assert 0 < len(err) <= 4096 and err.endswith(b'\n') and err.count(b'\n') == 1
    assert isinstance(json.loads(err)['code'], str)


def test_success_is_one_json_line_and_empty_stderr():
    code, out, err, timeout, _ = child(MAIN, b'{"mode":"payload"}\n')
    assert not timeout and code == 0 and err == b''
    assert out == b'{"status":"synthetic-ready"}\n'


def test_native_command_observation_shares_finite_deadline():
    script = MAIN.replace("return {'status':'synthetic-ready'}", 'await __import__("asyncio").sleep(60)')
    code, out, err, timeout, _ = child(script, b'{"mode":"payload"}\n')
    assert not timeout, 'native operation ignores the common finite deadline'
    assert code == 2 and out == b'' and json.loads(err)['code'] == 'cortex_health_unavailable'


def test_cancelled_native_observation_cannot_hang_in_connection_close(monkeypatch):
    p = product()
    db, settings, principal, response, frame, connect = native_fixture(p, monkeypatch)
    actions = []
    async def hanging_close():
        actions.append('close-started')
        await asyncio.sleep(60)
    db.close = hanging_close
    db.terminate = lambda: actions.append('terminate')
    monkeypatch.setattr(p, 'COMMAND_TIMEOUT', 0.05, raising=False)
    async def observe():
        task = asyncio.create_task(p.execute_private(frame, settings=settings, connect=connect))
        done, pending = await asyncio.wait([task], timeout=0.4)
        if pending:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=0.2)
            except (TimeoutError, asyncio.CancelledError):
                pass
        assert done, 'native connection shutdown extends the finite command deadline'
        with pytest.raises(p.NativeRefusal) as exc:
            task.result()
        assert exc.value.code == 'cortex_provisioning_reissue_required'
        assert 'terminate' in actions
        assert all(token not in str(exc.value) for token in (frame['credential'], response['lead_token'], response['console_token']))
    asyncio.run(observe())
