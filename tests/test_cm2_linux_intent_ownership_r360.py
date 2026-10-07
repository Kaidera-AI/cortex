"""Returned intent fd lifetime through unchanged actual concrete-producer driver."""
import os
from pathlib import Path

import pytest
from test_cm2_linux_concrete_ports import test_concrete_producer_requires_exact_intent_lock_owner_both_records_and_fresh_runtime as original_driver


@pytest.mark.parametrize('case',['valid','resume','uncertain-create','malformed-response','owner-refused'])
def test_concrete_intent_new_resume_and_refusal_close_fd_without_changing_valid_bytes(case,tmp_path,monkeypatch):
    actual_open=os.open;opened=[]
    def opening(path,flags,*a,**kw):
        fd=actual_open(path,flags,*a,**kw)
        if Path(path).name.endswith('.request.json') and flags & os.O_RDWR and flags & os.O_CREAT and flags & os.O_EXCL:
            info=os.fstat(fd);opened.append((fd,tmp_path/Path(path).name,(info.st_dev,info.st_ino)))
        return fd
    monkeypatch.setattr(os,'open',opening)
    try:
        original_driver(case,tmp_path,monkeypatch)
        if case=='owner-refused':assert opened==[]
        else:
            assert len(opened)==1
            for fd,path,identity in opened:
                with pytest.raises(OSError):os.fstat(fd)
                assert path.exists() and path.stat().st_mode&0o777==0o600 and path.stat().st_nlink==1
                from cortex_v2.clients.native_prerequisite import read_private_json
                assert read_private_json(path)['schema']=='cortex.provision-intent.v1'
    finally:
        # Close a still-owned RED fixture fd only after the assertion has failed.
        for fd,path,identity in opened:
            try:
                info=os.fstat(fd)
                if (info.st_dev,info.st_ino)==identity:os.close(fd)
            except OSError:pass
