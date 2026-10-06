"""R240 reproduced real ELF fixture plus bounded cookie ambiguity regressions."""
import gzip
import hashlib
import json
from pathlib import Path
import struct

import pytest

from test_linux_builder_contract import ROOT, archive, elf, load, versions

FIXTURE = ROOT / 'tests/fixtures/linux-native-layout'


def real_binary(tmp_path):
    receipt = json.loads((FIXTURE / 'provenance.json').read_text())
    assert receipt['scope'] == 'separate reproduction; not run37434346182 bytes'
    assert receipt['pyinstaller_wheel_sha256'] == '451a4ae14b719365bf1a2f0a99dae7b3463060061c3a394c70d5264cfb439528'
    compressed = (FIXTURE / 'reproduced-layout.elf.gz').read_bytes()
    assert hashlib.sha256(compressed).hexdigest() == receipt['compressed_sha256']
    raw = gzip.decompress(compressed)
    assert hashlib.sha256(raw).hexdigest() == receipt['binary_sha256']
    assert receipt['cookie_offset'] + 88 < len(raw), 'fixture does not reproduce ELF bytes after the cookie'
    assert raw[receipt['cookie_offset']:receipt['cookie_offset']+8] == b'MEI\014\013\012\013\016'
    binary = tmp_path / 'reproduced-layout.elf'; binary.write_bytes(raw)
    return binary, receipt


def test_real_pinned_pyinstaller_layout_is_parsed_at_its_actual_position(tmp_path, monkeypatch):
    p = load('linux_binaries.py', monkeypatch)
    binary, receipt = real_binary(tmp_path)
    items = dict(p.embedded_payloads(binary))
    assert items and len(items) == receipt['payload_count']
    assert 'libpython3.12.so.1.0' in items
    for name, digest in receipt['selected_payloads_sha256'].items():
        assert hashlib.sha256(items[name]).hexdigest() == digest


def test_real_fixture_wheel_and_elf_provenance_is_explicit(tmp_path):
    binary, receipt = real_binary(tmp_path)
    assert receipt['builder_architecture'] == 'amd64'
    assert receipt['builder_os'] == 'linux'
    assert receipt['builder_image'].startswith('ami-')
    assert receipt['runtime']['python'] == '3.12.13'
    assert receipt['product_target_qualification'] is False
    assert receipt['qualification'] == 'CArchive geometry and Python3.12 binding only'
    assert receipt['command_receipt_sha256'] and receipt['elf_sections_sha256']


@pytest.mark.parametrize('suffix', [b'\0' * 16, b'public ELF section names\0' * 8,
                                  b'MEI\014\013\012\013\016not-a-valid-cookie'])
def test_unique_valid_cookie_can_precede_bounded_non_archive_elf_bytes(tmp_path, monkeypatch, suffix):
    p = load('linux_binaries.py', monkeypatch)
    binary = tmp_path / 'binary'; pairs = [('libpython3.12.so.1.0', elf()), ('opaque', b'public bytes')]
    binary.write_bytes(archive(pairs) + suffix)
    assert list(p.embedded_payloads(binary)) == pairs


def test_two_valid_archive_positions_are_ambiguous(tmp_path, monkeypatch):
    p = load('linux_binaries.py', monkeypatch)
    binary = tmp_path / 'binary'; blob = archive([('libpython3.12.so.1.0', elf())])
    binary.write_bytes(blob + blob)
    with pytest.raises(RuntimeError, match='ambig'): list(p.embedded_payloads(binary))


def test_cookie_search_does_not_scan_outside_its_declared_tail_window(tmp_path, monkeypatch):
    p = load('linux_binaries.py', monkeypatch)
    assert hasattr(p, 'MAX_COOKIE_WINDOW'), 'cookie scan has no explicit finite bound'
    monkeypatch.setattr(p, 'MAX_COOKIE_WINDOW', 256)
    binary = tmp_path / 'binary'; binary.write_bytes(archive([('libpython3.12.so.1.0', elf())]) + b'\0' * 257)
    with pytest.raises(RuntimeError): list(p.embedded_payloads(binary))


@pytest.mark.parametrize('case', ['wrong-python', 'wrong-library', 'package-before-file', 'toc-outside', 'truncated-cookie'])
def test_tail_search_preserves_native_binding_and_archive_bounds(tmp_path, monkeypatch, case):
    p = load('linux_binaries.py', monkeypatch)
    binary = tmp_path / 'binary'; raw = archive([('libpython3.12.so.1.0', elf())]); cookie = list(struct.unpack('!8sIIII64s', raw[-88:]))
    if case == 'wrong-python': cookie[4] = 313
    elif case == 'wrong-library': cookie[5] = b'libpython3.12.soevil'
    elif case == 'package-before-file': cookie[1] = len(raw) + 1
    elif case == 'toc-outside': cookie[2] = cookie[1] + 1
    else: raw = raw[:-65]
    if case != 'truncated-cookie': raw = raw[:-88] + struct.pack('!8sIIII64s', *cookie)
    binary.write_bytes(raw + b'\0' * 32)
    with pytest.raises(RuntimeError): list(p.embedded_payloads(binary))


def test_non_eof_archive_still_checks_every_embedded_elf_before_help(tmp_path, monkeypatch):
    p = load('linux_binaries.py', monkeypatch)
    binary = tmp_path / 'cortex-test'; binary.write_bytes(archive([('opaque', elf(183))]) + b'\0' * 32)
    calls = []
    def run(argv, *, read=False):
        calls.append(argv)
        return versions('2.35')
    with pytest.raises(RuntimeError, match='ELF'): p.verify_program(binary, run=run)
    assert [str(binary), '--help'] not in calls
