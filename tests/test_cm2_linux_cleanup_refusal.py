"""Exact owned child cleanup errors stay typed; fixed public fixture bytes only."""
import errno
import signal
import sys

import pytest

CASES = ['stdout-permission', 'stderr-permission', 'stdout-io', 'stderr-io']
MARKER = 'PUBLIC_CLEANUP_EXCEPTION_SENTINEL'


@pytest.mark.parametrize('case', CASES)
def test_cleanup_error_still_waits_closes_pipes_and_returns_only_safe_refusal(case, monkeypatch):
    from cortex_v2.clients import linux_provisioning as module
    actual_popen = module.subprocess.Popen
    actual_killpg = module.os.killpg
    children = []; waits = []; signals = []
    def popen(*args, **kwargs):
        process = actual_popen(*args, **kwargs); children.append(process)
        original_wait = process.wait
        def wait(*args, **kwargs):
            waits.append(kwargs['timeout']); return original_wait(*args, **kwargs)
        process.wait = wait
        return process
    def killpg(pid, number):
        assert len(children) == 1 and pid == children[0].pid and number == signal.SIGKILL
        signals.append((pid, number))
        error = PermissionError if case.endswith('permission') else OSError
        raise error(errno.EPERM if case.endswith('permission') else errno.EIO, MARKER)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    monkeypatch.setattr(module.os, 'killpg', killpg)
    descriptor = 1 if case.startswith('stdout') else 2
    count = 70000 if descriptor == 1 else 8192
    script = 'import os; os.write(' + str(descriptor) + ',b"x"*' + str(count) + ')'
    try:
        with pytest.raises(module.ProvisionRefusal) as error:
            module.private_json_command([sys.executable, '-c', script], {'mode': 'payload'}, timeout=1)
        assert error.value.code == 'cortex_health_unavailable'
        assert MARKER not in str(error.value) and MARKER not in str(error.value.public())
        assert len(signals) == len(waits) == 1 and 0 <= waits[0] <= 1
        assert all(stream.closed for stream in (children[0].stdin, children[0].stdout, children[0].stderr))
        assert children[0].poll() is not None
    finally:
        for process in children:
            if process.poll() is None:
                try: actual_killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed: stream.close()
