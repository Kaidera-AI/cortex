"""Concrete staged shim help; native compiler admission is a separate gate."""
import json
from pathlib import Path
import pytest
from test_linux_builder_contract import load
from cortex_v2.cli.agent_boot import USAGE

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('defect',['valid','wrong-help','failed-help','no-stage'])
def test_native_freezer_finishes_boot_only_in_own_host_stage(defect,tmp_path,monkeypatch):
    module=load('build-candidate.py',monkeypatch)
    assert callable(getattr(module,'_finish_boot_stage',None))
    stage=tmp_path/'stage';(stage/'bin').mkdir(parents=True)
    script=stage/'bin/cortex-agent'
    if defect=='failed-help':body='raise SystemExit(3)'
    else:body='print('+repr('PUBLIC wrong' if defect=='wrong-help' else USAGE)+',end="")'
    script.write_text('#!/usr/bin/env python3\n'+body+'\n');script.chmod(0o755)
    frame={'required':True,'receipt':None}
    token=module._BOOT_STAGE.set(None if defect=='no-stage' else frame)
    try:
        if defect in ('wrong-help','failed-help'):
            with pytest.raises(Exception):module._finish_boot_stage(stage)
            assert frame['receipt'] is None
        else:
            module._finish_boot_stage(stage)
            if defect=='valid':
                assert frame['receipt']['help']==frame['receipt']['syntax']=='PASS'
                assert (stage/'bin/cortex-boot').read_bytes()==(ROOT/'scripts/agent-shims/cortex-boot').read_bytes()
            else:
                assert frame['receipt'] is None and not (stage/'bin/cortex-boot').exists()
    finally:module._BOOT_STAGE.reset(token)
