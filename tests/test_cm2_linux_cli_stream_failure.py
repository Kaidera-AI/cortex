"""Real stream failures cannot masquerade as exit2's empty-stdout refusal."""
import copy
import io
import time

import pytest

from test_cm2_linux_cli import product
from test_cm2_linux_publication import publication


@pytest.mark.parametrize('case', ['partial-error', 'late-output', 'keyboard', 'signal-bound'])
def test_started_public_output_failure_is_nonprotocol_and_finite(case, tmp_path, monkeypatch):
    cli = product()
    host, _, _, _, _, proof, _, _, descriptor, _, _, _ = publication(tmp_path, monkeypatch)
    nonce = '0123456789abcdef0123456789abcdef'
    proof.pop('status')
    proof.update(schema='cortex.prerequisite-proof.v2', nonce=nonce, descriptor_sha256='a'*64)
    monkeypatch.setattr(host, 'read_linux_prerequisite_proof', lambda *a, **kw: copy.deepcopy(proof))
    monkeypatch.setattr(cli.platform, 'system', lambda: 'Linux')
    actual_clock = time.monotonic
    started = actual_clock()
    if case == 'signal-bound':
        actual_wall = cli._wall_deadline
        monkeypatch.setattr(cli, '_wall_deadline', lambda deadline: actual_wall(actual_clock()+0.12))

    class Writer(io.StringIO):
        def write(self, value):
            if case == 'late-output':
                result = super().write(value)
                monkeypatch.setattr(cli.time, 'monotonic', lambda: started+31)
                return result
            super().write(value[:12])
            if case == 'keyboard':
                raise KeyboardInterrupt
            if case == 'signal-bound':
                time.sleep(1)
            raise RuntimeError('PUBLIC_STREAM_FAILURE')

    out = Writer()
    err = io.StringIO()
    result = cli.main(['prerequisite-proof', '--descriptor', str(descriptor), '--nonce', nonce],
                      out=out, err=err)
    assert out.getvalue(), 'counterexample must exercise an actual started stream'
    assert result == 4, 'started output cannot become exit2 with nonempty stdout'
    assert err.getvalue() == '', 'do not emit a misleading typed refusal after starting stdout'
    if case == 'signal-bound':
        assert actual_clock()-started < 0.9, 'actual wall timer must bound the blocked writer'
