"""Final author F1: actual native-style engine output is bounded before parsing."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_linux_ci_diagnostics import SOURCE, engine_fixture, product


@pytest.mark.parametrize('oversized', ['info', 'version'])
def test_actual_native_info_and_version_producers_use_bounded_capture(tmp_path, monkeypatch, oversized):
    module = product(); script = tmp_path / 'public-engine-producer.py'
    script.write_text('import json,sys\n'
        + 'oversized=' + repr(oversized) + '\n'
        + 'info=' + repr(engine_fixture()) + '\n'
        + "value=info if sys.argv[1]=='info' else {'Client': {'Version': '6.0.2'}}\n"
        + "if sys.argv[1]==oversized:value['unused_public_padding']='x'*2097152\n"
        + 'print(json.dumps(value))\n')
    unbounded = []; captured = []

    class NativeProducer:
        def __init__(self, target):
            self.local_abi = True
            self.prefix = [sys.executable, str(script)]
        def run(self, args, *, read=False, input_data=None, timeout=90, allowed=(0,)):
            result = subprocess.run(self.prefix + args, capture_output=True, timeout=3)
            unbounded.append(len(result.stdout))
            return result.stdout.decode().strip() if read else str(result.returncode)

    actual_capture = module.capture_command
    def finite(prefix, args, *, input_data=None, timeout=90, public=False):
        assert timeout <= 5
        value = actual_capture(prefix, args, input_data=input_data, timeout=timeout, public=public)
        captured.append((args[0], len(value['stdout']), len(value['stderr']), value['overflow']))
        return value

    monkeypatch.setattr(module, 'native_context', lambda target: None)
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(module, 'capture_command', finite)
    engine = module.instrument_builder(NativeProducer, tmp_path / 'diagnostics', SOURCE)('linux-x86_64')
    assert unbounded == [], 'native engine JSON reads still use the unbounded shared capture'
    assert captured and any(kind == oversized and overflow for kind, out, err, overflow in captured)
    assert all(out <= 1048576 and err <= 65536 for kind, out, err, overflow in captured)
    report = json.loads((tmp_path / 'diagnostics/engine.json').read_text())
    assert report['status'] == 'unavailable'
    assert len(json.dumps(report)) < 1024 and 'unused_public_padding' not in report
