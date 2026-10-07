"""Physical source shim bytes and private stage; compiler/native binary seam excluded."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest
from test_linux_builder_contract import load

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('defect',['valid','missing','linked','hardlinked','writable','nonexec','empty','bad-syntax','existing'])
def test_boot_shim_freeze_binds_physical_source_and_never_overwrites(defect,tmp_path,monkeypatch):
    module=load('build-candidate.py',monkeypatch)
    assert callable(getattr(module,'freeze_boot_shim',None)), 'source-owned boot shim freezer missing'
    root=tmp_path/'source';source=root/'scripts/agent-shims/cortex-boot';source.parent.mkdir(parents=True)
    source.write_bytes((ROOT/'scripts/agent-shims/cortex-boot').read_bytes());source.chmod(0o755)
    stage=tmp_path/'host';(stage/'bin').mkdir(parents=True)
    if defect=='missing':source.unlink()
    if defect=='linked':source.rename(tmp_path/'real-shim');source.symlink_to(tmp_path/'real-shim')
    if defect=='hardlinked':os.link(source,tmp_path/'alias')
    if defect=='writable':source.chmod(0o777)
    if defect=='nonexec':source.chmod(0o644)
    if defect=='empty':source.write_bytes(b'')
    if defect=='bad-syntax':source.write_bytes(b'#!/bin/bash\nif then\n')
    if defect=='existing':(stage/'bin/cortex-boot').write_bytes(b'PUBLIC retained')
    if defect=='valid':
        receipt=module.freeze_boot_shim(root,stage)
        target=stage/'bin/cortex-boot'
        assert target.read_bytes()==source.read_bytes()
        assert target.stat().st_mode & 0o777==0o755
        assert receipt['sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
        assert receipt['syntax']=='PASS'
        (stage/'bin/cortex-agent').write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n');(stage/'bin/cortex-agent').chmod(0o755)
        result=subprocess.run(['bash',str(target),'KAI@helix','--query','PUBLIC / é+?','--full'],env=dict(os.environ,CORTEX_CONNECTION_PROFILE='/PUBLIC/profile.json'),capture_output=True,text=True)
        assert result.returncode==0 and result.stderr==''
        assert json.loads(result.stdout)==['--config','/PUBLIC/profile.json','boot','KAI@helix','--query','PUBLIC / é+?','--full']
    else:
        with pytest.raises((RuntimeError,OSError)):module.freeze_boot_shim(root,stage)
        if defect=='existing':assert (stage/'bin/cortex-boot').read_bytes()==b'PUBLIC retained'
        else:assert not (stage/'bin/cortex-boot').exists()
