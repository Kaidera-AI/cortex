"""Additive R277-FV1 regression; the original frozen helper is unchanged."""
import pytest

from test_linux_ci_delegation import fixture


@pytest.mark.parametrize('raw', [b'0::/actions_job/cortex-build.scope\r\n', b'0::/\r\n'],
                         ids=['scope-crlf', 'root-crlf'])
def test_literal_crlf_placement_refuses_before_any_privileged_intent(raw, tmp_path, monkeypatch):
    module, path, calls, capture, info, private = fixture(tmp_path, monkeypatch, raw=raw)
    with pytest.raises(module.DelegationRefused):
        module.prepare_delegation(cgroup_path=path, capture=capture)
    assert calls == []
