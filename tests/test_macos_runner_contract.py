"""R134/R135 fixed native runner contract; source only, no builder or target."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
BASE_WORKFLOW_SHA256 = 'c1b3dd93acd30dd66a850d2869ebdd8375886bae8751bc65c938b23f5cb05631'


def test_supported_native_runner_preserves_the_accepted_build_contract():
    text = (ROOT / '.github/workflows/cortex-candidate.yml').read_text()
    jobs = yaml.safe_load(text)['jobs']
    assert jobs['host']['runs-on'] == 'macos-15'
    assert text.count('runs-on: macos-15') == 1
    original = text.replace('runs-on: macos-15', 'runs-on: macos-14', 1)
    assert hashlib.sha256(original.encode()).hexdigest() == BASE_WORKFLOW_SHA256
    for name in ('identity', 'images', 'package'):
        assert jobs[name]['runs-on'] == 'ubuntu-24.04-arm'
    assert jobs['host']['env'] == {
        'SSL_CERT_FILE': '/etc/ssl/cert.pem',
        'PIP_CERT': '/etc/ssl/cert.pem',
    }
    script = ROOT / 'scripts/release/bootstrap-macos-runtime.py'
    result = subprocess.run(
        [sys.executable, str(script), '--describe'],
        check=True, capture_output=True, text=True,
    )
    inputs = json.loads(result.stdout)
    assert inputs['target'] == 'macos-arm64'
    assert inputs['deployment_target'] == '14.0'
    assert inputs['python']['version'] == '3.12.14'
    assert inputs['openssl']['version'] == '3.5.8'
    source = script.read_text()
    assert "platform.system() != 'Darwin'" in source
    assert "platform.machine() != 'arm64'" in source
    assert "MACOSX_DEPLOYMENT_TARGET='14.0'" in source
