"""Complete source host stage writes the CM-2 digest/license receipt, no native build."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_cm2_linux_host_freeze import product

def test_complete_host_stage_records_cm2_helper_and_exact_bootstrap_license_bytes(tmp_path,monkeypatch):
    module=product(monkeypatch)
    runtime=tmp_path/'runtime';runtime.mkdir();(runtime/'bin').mkdir();python=runtime/'bin/python';python.write_bytes(b'PUBLIC pinned interpreter observation')
    (runtime/'licenses').mkdir();notice=runtime/'licenses/LICENSE';notice.write_bytes(b'PUBLIC synthetic license notice')
    expected={'openssl':{'version':'3.5.8','url':'https://public.invalid/openssl'}}
    native={'schema':'cortex.native-ci-runtime.v1','inputs':expected,'runtime':{'python':'3.12.14'},
            'licenses':{'LICENSE':hashlib.sha256(notice.read_bytes()).hexdigest()}}
    (runtime/'runtime-inventory.json').write_text(json.dumps(native))
    monkeypatch.setattr(module.platform,'system',lambda:'Linux');monkeypatch.setattr(module.platform,'machine',lambda:'x86_64')
    monkeypatch.setattr(module.platform,'python_version',lambda:'3.12.14');monkeypatch.setattr(module.sys,'executable',str(python))
    calls=[];source='a'*40;version='0.2.001-test.20261007.1'
    monkeypatch.setattr(module,'source_identity',lambda value:calls.append(('source',value)))
    def run(args,*,read=False):
        if args[-1]=='--describe':
            assert read and Path(args[1]).name=='bootstrap-linux-runtime.py';calls.append(('bootstrap',));return json.dumps(expected)
        assert args[:4]==[str(python),'-m','pip','install'] and '--require-hashes' in args and '--only-binary=:all:' in args
        assert Path(args[-1]).name=='requirements-linux-host-build.txt';calls.append(('lock',));return ''
    monkeypatch.setattr(module,'run',run)
    distribution=SimpleNamespace(version='PUBLIC fixture',files=['fixture.dist-info/licenses/LICENSE'],locate_file=lambda name:notice)
    monkeypatch.setattr(module.metadata,'distribution',lambda name:distribution)
    programs={}
    def freeze(out):
        calls.append(('cm2-freeze',));(out/'bin').mkdir();(out/'work').mkdir();(out/'spec').mkdir()
        for name,inventory in [('cortex','cm2-host-archive-inventory.txt'),('cortex-agent','agent-archive-inventory.txt')]:
            binary=out/'bin'/name;binary.write_bytes(('PUBLIC '+name+' native observation').encode());binary.chmod(0o700)
            (out/inventory).write_text('PUBLIC archive inventory\n')
            programs[name]={'sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'dependencies':['libc.so.6'],
                            'archive_inventory':inventory,'architecture':'x86_64','maximum_glibc':'2.35','help':'PASS','embedded_elf_count':1}
        return programs
    monkeypatch.setattr(module,'freeze_linux_cm2_programs',freeze)
    monkeypatch.setattr(module,'freeze_linux_programs',lambda *a:pytest.fail('legacy TEST freezer selected'))
    monkeypatch.setattr(module,'freeze_host_programs',lambda *a:pytest.fail('Mac freezer selected'))
    out=tmp_path/'out';module.host(out,source,version,runtime,target='linux-x86_64',cm2=True)
    receipt=json.loads((out/'host-inventory.json').read_text())
    assert receipt['host_contract']=='linux-cm2' and receipt['source_sha']==source and receipt['target']=='linux-x86_64'
    assert receipt['programs']==programs and receipt['dependencies']==programs['cortex']['dependencies']
    assert receipt['native_runtime_bootstrap']==native and receipt['build_lock_sha256']==hashlib.sha256((module.ROOT/'scripts/release/requirements-linux-host-build.txt').read_bytes()).hexdigest()
    assert (out/'licenses/LICENSE').read_bytes()==notice.read_bytes()
    spdx=json.loads((out/'host.spdx.json').read_text())
    assert any(p['SPDXID']=='SPDXRef-cortex-helper' and p['name']=='Cortex CM-2 native helper' for p in spdx['packages'])
    assert not any(p['SPDXID']=='SPDXRef-cortex-installer' for p in spdx['packages'])
    assert calls==[('source',source),('bootstrap',),('lock',),('cm2-freeze',)]
    assert not (out/'work').exists() and not (out/'spec').exists() and not (out/'bin/cortex-test').exists()
