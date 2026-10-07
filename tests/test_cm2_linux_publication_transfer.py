"""Literal interruption while a returned new inode is awaiting registration."""
import inspect
import os
import sys

import pytest
from test_cm2_linux_publication import publication


@pytest.mark.parametrize('which',['state','connection','descriptor'])
def test_returned_owned_inode_closes_on_registration_interruption_and_marker_is_invalid(which,tmp_path,monkeypatch):
    module,args,response,store,context,proof,members,connection,descriptor,state,calls,call=publication(tmp_path,monkeypatch)
    target={'state':state,'connection':connection,'descriptor':descriptor}[which]
    lines,start=inspect.getsourcelines(module.publish_linux_prerequisite)
    site=start+next(i for i,s in enumerate(lines) if s.strip()=='published[path] = result')
    captured=[]
    def trace(frame,event,arg):
        if frame.f_code is module.publish_linux_prerequisite.__code__ and event=='line' and frame.f_lineno==site and frame.f_locals['path']==target:
            captured.append(frame.f_locals['result']['fd'])
            raise RuntimeError('PUBLIC synthetic handoff interruption')
        return trace
    try:
        sys.settrace(trace)
        with pytest.raises(module.PrerequisiteRefusal) as error:call()
    finally:
        sys.settrace(None)
    assert 'PUBLIC synthetic' not in str(error.value) and len(captured)==1
    try:
        with pytest.raises(OSError):os.fstat(captured[0])
        assert target.exists() and target.stat().st_nlink==1 and target.stat().st_mode&0o777==0o600
        if which=='descriptor':assert target.read_bytes()==b''
        else:assert module.custody.read_private_json(target)
        assert not list(tmp_path.glob('.*'))
    finally:
        # A failing RED may leave the observed owned fixture FD open; close it
        # for isolation after the actual assertion, never hide the failure.
        try:os.close(captured[0])
        except OSError:pass
