"""R263 parallel CM2: the lock preserves the operation's typed refusal."""
import importlib
import stat

import pytest


@pytest.mark.parametrize('code', ['cortex_provisioning_reissue_required', 'cortex_health_unavailable'])
def test_actual_owned_lock_preserves_body_refusal_and_releases_same_inode(tmp_path, code):
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    path = tmp_path / 'operation.lock'
    original = module.ProvisionRefusal(code)
    identity = None
    with pytest.raises(module.ProvisionRefusal) as error:
        with module.operation_lock(path) as acquired:
            assert acquired is True
            identity = (path.stat().st_dev, path.stat().st_ino)
            raise original
    assert error.value is original
    assert error.value.code == code
    with module.operation_lock(path) as acquired:
        assert acquired is True
    assert identity == (path.stat().st_dev, path.stat().st_ino)
    assert path.read_bytes() == b'' and stat.S_IMODE(path.stat().st_mode) == 0o600
