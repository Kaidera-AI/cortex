"""Actual finite public child overflow; denied owned-group kill is intercepted."""
import importlib
import os
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('denial', ['permission', 'io'])
def test_engine_cleanup_denied_kill_still_waits_closes_pipes_and_returns_fixed_refusal(stream, denial, monkeypatch, tmp_path):
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    original = subprocess.Popen; children = []; attempts = []
    def child(*args, **kwargs):
        value = original(*args, **kwargs); children.append(value); return value
    def denied(pid, signal):
        attempts.append((pid, signal))
        raise PermissionError('public fixture denied') if denial == 'permission' else OSError('public fixture io')
    monkeypatch.setattr(module.subprocess, 'Popen', child); monkeypatch.setattr(module.os, 'killpg', denied)
    number, length = (1, 65537) if stream == 'stdout' else (2, 4097)
    # Finite child exits normally. It never executes Podman or carries private data.
    command = [sys.executable, '-c', 'import os; os.write(' + str(number) + ', b"x" * ' + str(length) + ')']
    started = time.monotonic()
    try:
        with pytest.raises(module.PrerequisiteRefusal) as error:
            module._engine_json(command, environment={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'LC_ALL': 'C'}, deadline=started + 2)
        assert error.value.code == 'cortex_podman_unsupported'
        assert 'public fixture' not in str(error.value)
        assert len(children) == len(attempts) == 1 and attempts[0][0] == children[0].pid
        assert children[0].stdout.closed and children[0].stderr.closed
        assert time.monotonic() - started < 2.2
    finally:
        for value in children:
            value.wait(timeout=2)
            for pipe in (value.stdout, value.stderr):
                if pipe is not None and not pipe.closed: pipe.close()
