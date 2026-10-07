"""Strict reference refusal before canonical selection; public snapshots only."""
import pytest
from boot_r374_fixture import seed,required


@pytest.mark.parametrize('location', ['agent-persona-pin','entry-pin','publication-pin','canonical-revision'])
def test_reference_revisions_are_exact_integers_not_boolean_aliases(location):
    module=required();snapshot,_=seed()
    if location=='agent-persona-pin':snapshot['agent_rows'][0]['persona_revision']=True
    elif location=='entry-pin':snapshot['entry_rows'][0]['bound_revision']=True
    elif location=='publication-pin':snapshot['publication_rows'][0]['entry_revision']=True
    else:snapshot['persona_rows'][0]['revision']=True
    with pytest.raises(ValueError):module.project_boot_response(snapshot,budget=1200,query=None,full=False)


def test_entry_kind_never_admits_two_canonical_identifiers():
    module=required();snapshot,_=seed()
    entry=snapshot['entry_rows'][0]
    entry['skill_id']=snapshot['skill_rows'][0]['skill_id']
    with pytest.raises(ValueError):module.project_boot_response(snapshot,budget=1200,query=None,full=False)
