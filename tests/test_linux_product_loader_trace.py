"""A paired library probe alone cannot prove what the product binary loads."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_linux_builder_contract import load


@pytest.mark.parametrize('case', ['valid', 'host-crypto', 'host-ssl', 'missing-library',
    'missing-version-check', 'unsupported-node', 'different-extraction-roots', 'failed-help', 'oversized-trace'])
def test_product_help_loader_must_use_the_verified_bundle(case, tmp_path, monkeypatch):
    p = load('linux_runtime_closure.py', monkeypatch)
    assert callable(getattr(p, 'trace_product', None)), 'product loader observation is absent'
    binary = tmp_path / 'cortex-test'; binary.write_bytes(b'public fixture binary')
    libraries = {'libcrypto.so.3': {'definitions': ['OPENSSL_3.0.0', 'OPENSSL_3.4.0'], 'sha256': 'a' * 64},
                 'libssl.so.3': {'definitions': ['OPENSSL_3.0.0'], 'sha256': 'b' * 64}}
    crypto = '/tmp/_MEIfixture/libcrypto.so.3'; ssl = '/tmp/_MEIfixture/libssl.so.3'
    if case == 'host-crypto': crypto = '/usr/lib64/libcrypto.so.3'
    elif case == 'host-ssl': ssl = '/usr/lib64/libssl.so.3'
    elif case == 'different-extraction-roots': ssl = '/tmp/_MEIother/libssl.so.3'
    trace = (f"  42: calling init: {crypto}\n  42: calling init: {ssl}\n"
             f"  42: checking for version `OPENSSL_3.4.0' in file {crypto} [0] required by file {ssl} [0]\n"
             f"  42: checking for version `OPENSSL_3.0.0' in file {ssl} [0] required by file /tmp/_MEIfixture/_ssl.so [0]\n")
    if case == 'missing-library': trace = trace.replace(f'  42: calling init: {ssl}\n', '')
    elif case == 'missing-version-check': trace = '\n'.join(x for x in trace.splitlines() if 'checking for version' not in x)
    elif case == 'unsupported-node': trace = trace.replace('OPENSSL_3.4.0', 'OPENSSL_9.0.0')
    elif case == 'oversized-trace': trace = 'x' * (2 * 1024 * 1024 + 1)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv == [str(binary), '--help']
        env = kwargs['env']
        assert env == {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C',
                       'LD_DEBUG': 'libs,versions', 'LD_BIND_NOW': '1'}
        assert 0 < kwargs['timeout'] <= 10
        return SimpleNamespace(returncode=1 if case == 'failed-help' else 0,
                               stdout='public help\n', stderr=trace)
    if case == 'valid':
        result = p.trace_product(binary, libraries, run=run)
        assert result['help'] == 'PASS' and result['host_fallback'] is False
        assert result['loaded'] == {'libcrypto.so.3': crypto, 'libssl.so.3': ssl}
        assert result['checked_nodes']['libcrypto.so.3'] == ['OPENSSL_3.4.0']
    else:
        with pytest.raises(RuntimeError): p.trace_product(binary, libraries, run=run)
    assert len(calls) == 1
