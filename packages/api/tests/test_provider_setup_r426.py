"""Frozen host-only authority setup: pinned bytes, custody and no replacement."""
import hashlib
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[3]

@pytest.fixture
def runtime(tmp_path):
    module = runpy.run_path(str(ROOT/'packages/deploy/cortex-runtime'))
    obj = module['Runtime'].__new__(module['Runtime'])
    root = tmp_path.resolve()
    root.chmod(0o700)
    obj.state = root/'state'; obj.state.mkdir(mode=0o700)
    obj.payload = ROOT
    obj.args = SimpleNamespace(project='cortex', state_dir=str(obj.state))
    member = 'packages/installer/templates/openkai.env.example'
    obj.installation = {'files': {member: hashlib.sha256((ROOT/member).read_bytes()).hexdigest()}}
    def prepare():
        (obj.state/'runtime-owner.json').write_text(json.dumps({'schema':'cortex.runtime-owner.v1','project':'cortex'}))
        (obj.state/'runtime-owner.json').chmod(0o600)
    obj.prepare = prepare
    return obj, root/'provider'

def test_setup_exact_node_bytes_and_receipts(runtime):
    obj, home = runtime
    result = obj.provider_setup(str(home))
    template = (ROOT/'packages/installer/templates/openkai.env.example').read_bytes()
    assert (home/'.env').read_bytes() == template
    assert result == {'authored': True, 'path': str(home/'.env')}
    assert (home/'.env').stat().st_mode & 0o777 == 0o600
    assert home.stat().st_mode & 0o777 == 0o700
    assert json.loads((obj.state/'provider-settings.json').read_text()) == {'schema':'cortex.provider-settings.v1','path':str(home/'.env')}
    assert (obj.state/'provider-settings.json').stat().st_mode & 0o777 == 0o600
    provenance = json.loads((home/'cortex-provider-bootstrap.json').read_text())
    assert provenance == {'schema':'cortex.provider-bootstrap.v1','author':'cortex','source_repo':'Kaidera-AI/openkai','source_revision':'f3660f3c19939d2a6ff3b95be9aab3f85fb8312a','source_path':'.env.example','sha256':hashlib.sha256(template).hexdigest()}
    assert obj._provider_authority() == home/'.env'

def test_existing_authority_preserved(runtime):
    obj, home = runtime; home.mkdir(mode=0o700)
    target = home/'.env'; target.write_bytes(b'PRIVATE_SENTINEL=unchanged\n'); target.chmod(0o600)
    before = target.stat().st_ino
    assert obj.provider_setup(str(home)) == {'authored':False,'path':str(target)}
    assert target.read_bytes() == b'PRIVATE_SENTINEL=unchanged\n'
    assert target.stat().st_ino == before
    assert not (home/'cortex-provider-bootstrap.json').exists()

@pytest.mark.parametrize('unsafe', ['symlink','public','hardlink'])
def test_unsafe_existing_authority_refused(runtime, unsafe):
    obj, home=runtime;home.mkdir(mode=0o700)
    other=home/'other'; other.write_bytes(b'unchanged');other.chmod(0o600)
    target=home/'.env'
    if unsafe=='symlink':target.symlink_to(other)
    elif unsafe=='hardlink':os.link(other,target)
    else:target.write_bytes(b'unchanged');target.chmod(0o644)
    with pytest.raises(ValueError):obj.provider_setup(str(home))
    assert other.read_bytes()==b'unchanged'

@pytest.mark.parametrize('unsafe', ['relative','symlink','public'])
def test_unsafe_directory_refused(runtime, unsafe):
    obj,home=runtime
    if unsafe=='relative':home=Path('relative-provider')
    elif unsafe=='symlink':home.symlink_to(home.parent,target_is_directory=True)
    else:home.mkdir(mode=0o755)
    with pytest.raises(ValueError):obj.provider_setup(str(home))
    assert not (obj.state/'provider-settings.json').exists()

def test_template_inventory_mismatch_refused_before_writes(runtime):
    obj,home=runtime
    obj.installation['files']['packages/installer/templates/openkai.env.example']='0'*64
    with pytest.raises(ValueError):obj.provider_setup(str(home))
    assert not home.exists()

def test_conflicting_path_receipt_preserved(runtime):
    obj,home=runtime
    receipt=obj.state/'provider-settings.json';receipt.write_text('{"schema":"cortex.provider-settings.v1","path":"/different/.env"}');receipt.chmod(0o600)
    before=receipt.read_bytes()
    with pytest.raises(ValueError):obj.provider_setup(str(home))
    assert receipt.read_bytes()==before
    assert not home.exists()
