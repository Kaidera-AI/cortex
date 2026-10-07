"""Additional malformed-proof and physical root controls, before hardening."""
import copy
import json
from types import SimpleNamespace
import pytest
from boot_r374_fixture import seed, native_expected, install_public_bodies, json_bytes
from cortex_v2.cli.agent_boot import validate, BootRefused


@pytest.mark.parametrize('defect', ['overflow-number','missing-inline-ref','boolean-revision','extra-pin','linked-root','hardlinked-body','dot-ref'])
def test_rejects_malformed_proof_and_nonphysical_root(defect,tmp_path):
    root=tmp_path/'permanent-code-root';root.mkdir(mode=0o700)
    snapshot,original=seed(workspace_root=root);install_public_bodies(snapshot,root)
    data=native_expected(snapshot,original)
    proof=data['persona']['metadata']['boot_validation']
    if defect=='missing-inline-ref':
        pin=next(p for p in proof['body_pins'] if p['entry_kind']=='rule');del pin['body_ref']
    elif defect=='boolean-revision':proof['agent_binding_revision']=True
    elif defect=='extra-pin':proof['body_pins'].append(copy.deepcopy(proof['body_pins'][0]))
    elif defect=='linked-root':
        real=tmp_path/'real-root';root.rename(real);root.symlink_to(real,target_is_directory=True)
    elif defect=='hardlinked-body':
        import os
        target=root/proof['body_pins'][0]['body_ref'];os.link(target,tmp_path/'public-link')
    elif defect=='dot-ref':proof['body_pins'][0]['body_ref']='.';data['persona']['skills'][0]['body_ref']='.'
    raw=json_bytes(data)
    if defect=='overflow-number':raw=raw[:-1]+b',"untrusted":1e999}'
    with pytest.raises((BootRefused,OSError)):
        validate(raw,SimpleNamespace(default_scope='helix',workspace_root=str(root)),'kai')
