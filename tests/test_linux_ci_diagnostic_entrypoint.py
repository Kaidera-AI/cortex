"""Additive R263 entrypoint and real-process refusal effects."""
import json
from pathlib import Path
import secrets
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_linux_ci_diagnostics import ROOT, SOURCE, NONCE, InterceptedBuilder, database_args, option, product


def test_actual_driver_reaches_original_native_gate_after_resolving_shared_imports(tmp_path):
    program = """import pathlib,sys
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
import linux_ci_diagnostics as module
module.native_context=lambda target:None
args=SimpleNamespace(target='linux-x86_64',source_sha=sys.argv[2],version='0.2.001-test.20261006.1',output=pathlib.Path(sys.argv[3])/'images',diagnostics_output=pathlib.Path(sys.argv[3])/'diagnostics')
try: module.run_images(args)
except RuntimeError as error:
 assert str(error)=='native Linux amd64 builder required; no emulation'
else: raise AssertionError('unsupported host was allowed past original builder gate')
"""
    value=subprocess.run([sys.executable,'-c',program,str(ROOT/'scripts/release'),SOURCE,str(tmp_path)],capture_output=True,timeout=5)
    assert value.returncode==0, 'Linux driver did not reach the original builder gate'
    assert value.stdout==value.stderr==b''
    assert not (tmp_path/'images').exists() and not (tmp_path/'diagnostics').exists()


def test_real_spawn_refusal_still_writes_fixed_role_and_unavailable_exit(tmp_path, monkeypatch):
    module=product(); private=secrets.token_urlsafe(32).encode(); captured=[]
    monkeypatch.setattr(module,'native_context',lambda target:None)
    monkeypatch.setattr(module.shutil,'which',lambda name:'/usr/bin/'+name)
    engine=module.instrument_builder(InterceptedBuilder,tmp_path/'diagnostics',SOURCE)('linux-x86_64')
    def capture(prefix,args,*,input_data=None,timeout=90,public=False):
        captured.append((list(args),public))
        if not public:
            raise module.DiagnosticsRefused('diagnostic process unavailable') from OSError(private.decode())
        if args[:2] in (['volume','create'],['secret','create']):engine.objects[args[-1] if args[-1]!='-' else args[-2]]=NONCE
        elif args[0]=='create':engine.objects[option(args,'--name')]=NONCE
        return {'exit_code':0,'stdout':b'','stderr':b'','timed_out':False,'overflow':False}
    monkeypatch.setattr(module,'capture_command',capture);monkeypatch.setattr(module,'new_nonce',lambda:NONCE)
    engine.run(database_args())
    with pytest.raises(Exception):engine.run(['start',option(database_args(),'--name')])
    path=tmp_path/'diagnostics/real-start.json'
    assert path.is_file(), 'real process refusal lost the required role/exit diagnostic'
    report=json.loads(path.read_text())
    assert report['role']=='db' and report['exit_code'] is None
    assert report['signature']=='process_unavailable' and report['component']=='podman'
    public=b'\n'.join(p.read_bytes() for p in (tmp_path/'diagnostics').glob('*.json'))
    assert bool(private not in public)
    assert len([c for c in captured if c[0][0]=='start' and not c[1]])==1
    assert not engine.objects
