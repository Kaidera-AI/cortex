"""R258 same-version Linux wheel hashes and real pip hash refusal."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import zipfile

import pytest
from packaging.utils import parse_wheel_filename

ROOT = Path(__file__).parents[1]
VERSION = '0.12.19'
WHEELS = {
    'x86_64': ('uv-0.12.19-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl',
               'a63d18a0aa38ee9f21a5406afbbaeb41303bcd954be9d6b7c1b95ac275e53958'),
    'aarch64': ('uv-0.12.19-py3-none-manylinux_2_28_aarch64.whl',
                'a36d92c137098fdb9dce27261c5fa8ef5519ba82dcd15d70864c4676d889738d'),
}


def requirement():
    return (ROOT / 'requirements-build.txt').read_text()


def test_single_original_package_and_exact_version():
    lines = [l for l in requirement().splitlines() if l and not l.startswith(' ')]
    assert lines == ['uv==' + VERSION + ' \\']
    assert re.fullmatch(r'uv==0\.12\.19\s*(?:\\\s*)?(?:--hash=sha256:[0-9a-f]{64}\s*(?:\\\s*)?)+', requirement())


@pytest.mark.parametrize('architecture', list(WHEELS))
def test_each_exact_pypi_wheel_platform_has_its_audited_hash(architecture):
    filename, digest = WHEELS[architecture]
    name, version, _, tags = parse_wheel_filename(filename)
    assert name == 'uv' and str(version) == VERSION
    assert all(t.interpreter == 'py3' and t.abi == 'none' and t.platform.endswith('_' + architecture) for t in tags)
    assert '--hash=sha256:' + digest in requirement(), 'missing audited platform wheel hash'


def test_hash_set_contains_only_the_two_audited_same_version_wheels():
    hashes = re.findall(r'--hash=sha256:([0-9a-f]{64})', requirement())
    assert len(hashes) == 2 and set(hashes) == {v[1] for v in WHEELS.values()}


@pytest.mark.parametrize('recipe', ['deploy/release/Dockerfile.linux-amd64', 'Dockerfile'])
def test_both_original_recipe_hash_checks_remain_enforced(recipe):
    text = (ROOT / recipe).read_text()
    assert text.count('python -m pip install --no-cache-dir --require-hashes -r requirements-build.txt') == 2
    assert '--no-deps' not in text and '--no-hash' not in text


@pytest.mark.parametrize('architecture', list(WHEELS))
def test_real_pip_refuses_nonmatching_bytes_of_the_selected_platform_wheel(architecture, tmp_path):
    filename, _ = WHEELS[architecture]
    wheel = tmp_path / filename
    platform = 'manylinux2014_x86_64' if architecture == 'x86_64' else 'manylinux_2_28_aarch64'
    with zipfile.ZipFile(wheel, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('uv-0.12.19.dist-info/METADATA', 'Metadata-Version: 2.1\nName: uv\nVersion: 0.12.19\n')
        archive.writestr('uv-0.12.19.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: R258-hash-refusal-fixture\nRoot-Is-Purelib: false\nTag: py3-none-' + platform + '\n')
        archive.writestr('uv-0.12.19.dist-info/RECORD', '')
    result = subprocess.run([
        sys.executable, '-m', 'pip', '--isolated', 'download', '--no-cache-dir', '--disable-pip-version-check',
        '--no-index', '--no-deps', '--only-binary=:all:', '--platform', platform,
        '--implementation', 'cp', '--python-version', '3.12', '--require-hashes',
        '--find-links', str(tmp_path), '--dest', str(tmp_path / 'download'),
        '-r', str(ROOT / 'requirements-build.txt'),
    ], capture_output=True, timeout=20)
    assert result.returncode == 1
    assert b'THESE PACKAGES DO NOT MATCH THE HASHES' in result.stderr
    assert not list((tmp_path / 'download').glob('*.whl'))
