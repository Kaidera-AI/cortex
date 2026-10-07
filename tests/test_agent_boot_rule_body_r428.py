"""Rule provenance source_file is not its materialized body_ref."""
from types import SimpleNamespace
from boot_r374_fixture import seed,native_expected,install_public_bodies,json_bytes
from cortex_v2.cli.agent_boot import validate


def test_materialized_rule_body_and_inline_body_agree_with_pin_at_distinct_path(tmp_path):
    root=tmp_path/'permanent-code-root';root.mkdir(mode=0o700)
    snapshot,original=seed(workspace_root=root);install_public_bodies(snapshot,root)
    data=native_expected(snapshot,original)
    pin=next(p for p in data['persona']['metadata']['boot_validation']['body_pins'] if p['entry_kind']=='rule')
    row=next(r for r in data['persona']['rules'] if r['rule_slug']==pin['slug'])
    pin['body_ref']='public-rules/'+pin['slug']+'/BODY.md'
    target=root/pin['body_ref'];target.parent.mkdir(parents=True);target.write_text(row['body'])
    assert pin['body_ref'] != row['source_file']
    validate(json_bytes(data),SimpleNamespace(default_scope='helix',workspace_root=str(root)),'kai')
