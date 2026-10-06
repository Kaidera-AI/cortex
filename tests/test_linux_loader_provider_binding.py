"""Version checks must name the same OpenSSL providers actually initialized."""
from types import SimpleNamespace

import pytest

from test_linux_builder_contract import load


@pytest.mark.parametrize('foreign', ['libcrypto.so.3', 'libssl.so.3'])
def test_version_checked_provider_must_match_loaded_provider(foreign, tmp_path, monkeypatch):
    p = load('linux_runtime_closure.py', monkeypatch)
    binary = tmp_path / 'cortex-test'
    binary.write_bytes(b'public provider-binding fixture')
    names = ('libcrypto.so.3', 'libssl.so.3')
    libraries = {name: {'definitions': ['OPENSSL_3.0.0']} for name in names}
    lines = [f'  42: calling init: /tmp/_MEIverified/{name}' for name in names]
    for name in names:
        root = '_MEIother' if name == foreign else '_MEIverified'
        lines.append(f"  42: checking for version `OPENSSL_3.0.0' in file /tmp/{root}/{name} [0] required by file /tmp/_MEIverified/_ssl.so [0]")
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout='public help\n', stderr='\n'.join(lines))
    with pytest.raises(RuntimeError):
        p.trace_product(binary, libraries, run=run)
