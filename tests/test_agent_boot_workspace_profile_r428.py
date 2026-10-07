"""The permanent body root is explicit, separate from the private key root."""
import json
from pathlib import Path
import pytest
from cortex_v2.clients import config
from cortex_v2.clients.errors import ClientConfigError


def profile_file(tmp_path, workspace):
    data = dict(profile='v2', base_url='http://127.0.0.1:8501', installation='public',
                project='helix', name='kai', project_root=str(tmp_path/'credentials'))
    if workspace is not None:
        data['workspace_root'] = workspace
    p = tmp_path/'connection.json'; p.write_text(json.dumps(data)); p.chmod(0o600)
    return p


def test_explicit_workspace_root_survives_without_reading_member_key(tmp_path, monkeypatch):
    calls=[]
    def reader(**kwargs):
        calls.append(kwargs); return object()
    monkeypatch.setattr(config, 'MemberKeyReader', reader)
    workspace=str(tmp_path/'permanent-code-root')
    p=config.load_member_profile(profile_file(tmp_path,workspace),env={})
    assert p.workspace_root == workspace
    assert calls == [dict(installation='public',project='helix',name='kai',project_root=tmp_path/'credentials')]
    assert p.token == ''


def test_missing_workspace_root_stays_absent_without_credential_or_cwd_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'MemberKeyReader',lambda **kwargs:object())
    p=config.load_member_profile(profile_file(tmp_path,None),env={})
    assert p.workspace_root is None


@pytest.mark.parametrize('workspace', [True, [], {}, 42, 'relative/code', '', '/PUBLIC/../code'])
def test_invalid_workspace_root_refuses_before_member_reader(tmp_path,monkeypatch,workspace):
    def forbidden(**kwargs): pytest.fail('invalid root reached credential reader')
    monkeypatch.setattr(config,'MemberKeyReader',forbidden)
    with pytest.raises(ClientConfigError):
        config.load_member_profile(profile_file(tmp_path,workspace),env={})
