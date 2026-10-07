"""Actual held-parent sync order and failure effects; synthetic public bytes."""
import os
import stat

import pytest
from test_cm2_linux_publication import required


@pytest.mark.parametrize('case',['valid','directory-failure','directory-failure-replacement'])
def test_final_name_file_then_directory_sync_and_owned_only_failure(case,tmp_path,monkeypatch):
    module=required();tmp_path.chmod(0o700);target=tmp_path/'marker.json';held=tmp_path/'held.json'
    actual_sync=os.fsync;events=[];foreign=[];result=None
    def sync(fd):
        kind='directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file'
        events.append(kind)
        if kind=='directory' and case!='valid':
            if case=='directory-failure-replacement':
                target.rename(held);target.write_bytes(b'PUBLIC foreign replacement');target.chmod(0o600);foreign.append((target.read_bytes(),target.stat().st_ino))
            raise OSError('PUBLIC synthetic directory sync failure')
        return actual_sync(fd)
    monkeypatch.setattr(os,'fsync',sync)
    try:
        if case=='valid':
            result=module._publish_private_once(target,{'PUBLIC':'marker'},lambda:None)
            assert events[:2]==['file','directory'] and result['created'] is True
            assert module.custody.read_private_json(target)=={'PUBLIC':'marker'}
        else:
            with pytest.raises(module.PrerequisiteRefusal) as error:
                result=module._publish_private_once(target,{'PUBLIC':'marker'},lambda:None)
            assert 'PUBLIC synthetic' not in str(error.value) and events[:2]==['file','directory']
            owned=held if foreign else target
            assert owned.read_bytes()==b'' and owned.stat().st_nlink==1 and owned.stat().st_mode&0o777==0o600
            if foreign:assert foreign==[(target.read_bytes(),target.stat().st_ino)]
        assert not list(tmp_path.glob('.*'))
    finally:
        if result is not None:module._close_marker(result)
