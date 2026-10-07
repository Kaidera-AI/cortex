"""A successful write call can still report an incomplete public stream."""
import copy
import io

import pytest

from test_cm2_linux_cli import product
from test_cm2_linux_publication import publication


@pytest.mark.parametrize('case', ['short-write', 'zero-write', 'flush-error'])
def test_incomplete_public_write_never_returns_success(case, tmp_path, monkeypatch):
    cli = product()
    host, _, _, _, _, proof, _, _, descriptor, _, _, _ = publication(tmp_path, monkeypatch)
    nonce = '0123456789abcdef0123456789abcdef'
    proof.pop('status')
    proof.update(schema='cortex.prerequisite-proof.v2', nonce=nonce, descriptor_sha256='a'*64)
    monkeypatch.setattr(host, 'read_linux_prerequisite_proof', lambda *a, **kw: copy.deepcopy(proof))
    monkeypatch.setattr(cli.platform, 'system', lambda: 'Linux')

    class Writer(io.StringIO):
        def write(self, value):
            return super().write(value[:12] if case == 'short-write' else '' if case == 'zero-write' else value)

        def flush(self):
            if case == 'flush-error':
                raise OSError('PUBLIC_FLUSH_FAILURE')
            return super().flush()

    out = Writer()
    err = io.StringIO()
    result = cli.main(['prerequisite-proof', '--descriptor', str(descriptor), '--nonce', nonce],
                      out=out, err=err)
    assert len(out.getvalue()) == (12 if case == 'short-write' else 0 if case == 'zero-write' else len(cli._public_line(proof,65536)))
    assert result == 4, 'short or failed writes cannot qualify the exit0 proof protocol'
    assert err.getvalue() == ''


def test_incomplete_refusal_write_never_claims_exit2(tmp_path, monkeypatch):
    cli = product()
    monkeypatch.setattr(cli.platform, 'system', lambda: 'Linux')

    class Writer(io.StringIO):
        def write(self, value):
            return super().write(value[:12])

    out = io.StringIO()
    err = Writer()
    result = cli.main(['prerequisite-proof', '--descriptor', str(tmp_path/'descriptor.json'), '--nonce', 'bad'],
                      out=out, err=err)
    assert out.getvalue() == '' and len(err.getvalue()) == 12
    assert result == 4, 'short stderr cannot qualify exit2 single complete refusal'
