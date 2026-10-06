"""Bundled OpenSSL must match compiled bytes, supply nodes, and load literally."""
import hashlib
import json
from pathlib import Path

import pytest

from test_linux_builder_contract import archive, elf, load, versions


def fixture(tmp_path, monkeypatch, case='valid'):
    p = load('linux_runtime_closure.py', monkeypatch)
    runtime = tmp_path / 'runtime'; libs = runtime / 'openssl/lib'; libs.mkdir(parents=True)
    ssl = elf() + b'SSL_BYTES'; crypto = elf() + b'CRYPTO_BYTES'
    (libs / 'libssl.so.3').write_bytes(ssl); (libs / 'libcrypto.so.3').write_bytes(crypto)
    inventory = {'schema': 'cortex.native-ci-runtime.v1',
                 'runtime': {'python': '3.12.14', 'architecture': 'x86_64', 'openssl': 'OpenSSL 3.5.8 fixture'}}
    if case == 'wrong-runtime': inventory['runtime']['openssl'] = 'OpenSSL 3.0.2 host'
    (runtime / 'runtime-inventory.json').write_text(json.dumps(inventory))
    pairs = [('libssl.so.3', ssl), ('libcrypto.so.3', crypto), ('_ssl.fixture.so', elf() + b'CONSUMER_BYTES')]
    if case == 'missing-crypto': pairs = [x for x in pairs if x[0] != 'libcrypto.so.3']
    elif case == 'missing-ssl': pairs = [x for x in pairs if x[0] != 'libssl.so.3']
    elif case == 'wrong-crypto-bytes': pairs[1] = ('libcrypto.so.3', elf() + b'HOST_CRYPTO')
    elif case == 'wrong-ssl-bytes': pairs[0] = ('libssl.so.3', elf() + b'HOST_SSL')
    binary = tmp_path / 'cortex-test'; binary.write_bytes(archive(pairs))
    calls = []
    def run(argv, *, read=False):
        calls.append(argv)
        if argv[0] == 'readelf':
            raw = Path(argv[-1]).read_bytes()
            if '--dynamic' in argv:
                if raw == ssl: return ' (SONAME) Library soname: [libssl.so.3]\n (NEEDED) Shared library: [libcrypto.so.3]\n'
                if raw == crypto: return ' (SONAME) Library soname: [libcrypto.so.3]\n'
                return ' (NEEDED) Shared library: [libssl.so.3]\n'
            if raw == crypto:
                name = 'OPENSSL_3.0.0' if case == 'missing-node' else 'OPENSSL_3.4.0'
                return "Version definition section '.gnu.version_d' contains 1 entry:\n Name: " + name + '\n' + versions('2.35')
            if raw == ssl:
                return "Version definition section '.gnu.version_d' contains 1 entry:\n Name: OPENSSL_3.0.0\nVersion needs section '.gnu.version_r' contains 1 entry:\n File: libcrypto.so.3\n Name: OPENSSL_3.4.0\n" + versions('2.35')
            return "Version needs section '.gnu.version_r' contains 1 entry:\n File: libssl.so.3\n Name: OPENSSL_3.0.0\n" + versions('2.35')
        assert argv[0] == 'env' and argv[1] == '-i'
        assert any(v.startswith('LD_LIBRARY_PATH=') for v in argv)
        paths = argv[-2:]
        assert all(Path(v).is_file() for v in paths)
        if case == 'library-mutated': Path(paths[1]).write_bytes(b'changed after observation')
        loaded = ['/usr/lib/host/libssl.so.3', paths[1]] if case == 'host-loaded' else paths
        value = {'openssl': 'OpenSSL 3.0.2 host' if case == 'wrong-loaded-version' else 'OpenSSL 3.5.8 fixture',
                 'loaded': loaded}
        return 'not-json' if case == 'invalid-probe' else json.dumps(value)
    return p, binary, runtime, run, calls, ssl, crypto


def test_compiled_bundled_openssl_nodes_and_actual_loaded_paths_are_bound(tmp_path, monkeypatch):
    p, binary, runtime, run, calls, ssl, crypto = fixture(tmp_path, monkeypatch)
    result = p.qualify(binary, runtime, run=run)
    assert result['openssl'].startswith('OpenSSL 3.5.8 ')
    assert result['libraries']['libssl.so.3']['sha256'] == hashlib.sha256(ssl).hexdigest()
    assert result['libraries']['libcrypto.so.3']['sha256'] == hashlib.sha256(crypto).hexdigest()
    assert 'OPENSSL_3.4.0' in result['libraries']['libcrypto.so.3']['definitions']
    assert result['host_fallback'] is False
    assert result['binary_sha256'] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert any('--dynamic' in a for a in calls) and any('--version-info' in a for a in calls)
    assert calls[-1][0] == 'env'


@pytest.mark.parametrize('case', ['wrong-runtime', 'missing-crypto', 'missing-ssl', 'wrong-crypto-bytes',
    'wrong-ssl-bytes', 'missing-node', 'host-loaded', 'wrong-loaded-version', 'invalid-probe', 'library-mutated'])
def test_openssl_refuses_without_complete_literal_closure(tmp_path, monkeypatch, case):
    p, binary, runtime, run, calls, ssl, crypto = fixture(tmp_path, monkeypatch, case)
    with pytest.raises(RuntimeError): p.qualify(binary, runtime, run=run)


def test_missing_version_report_cannot_qualify_openssl(tmp_path, monkeypatch):
    p, binary, runtime, run, calls, ssl, crypto = fixture(tmp_path, monkeypatch)
    def unknown(argv, *, read=False):
        return 'unknown tool output' if '--version-info' in argv else run(argv, read=read)
    with pytest.raises(RuntimeError): p.qualify(binary, runtime, run=unknown)
