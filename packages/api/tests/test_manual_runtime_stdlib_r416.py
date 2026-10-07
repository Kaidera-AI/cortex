"""The existing runtime's recovery/help entrypoints stay stdlib-only."""
from pathlib import Path
import subprocess
import sys


def test_runtime_help_never_requires_signed_writer_dependencies():
    runtime=Path(__file__).resolve().parents[3]/'packages/deploy/cortex-runtime'
    result=subprocess.run([sys.executable,'-S',str(runtime),'--help'],capture_output=True,text=True,timeout=10)
    assert result.returncode==0, result.stderr
    assert 'enroll-console' in result.stdout and '--credential-dir' in result.stdout
