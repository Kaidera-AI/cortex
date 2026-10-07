"""Enactment request trust boundaries; public canonical snapshots only."""
import copy
import importlib
import importlib.util

import pytest
from boot_r374_fixture import seed, uid as native_uid


def uid(label):
    return str(native_uid(label))


def models():
    assert importlib.util.find_spec('cortex_v2.agent_boot_models'), 'Typed boot enactment models missing'
    return importlib.import_module('cortex_v2.agent_boot_models')


@pytest.mark.parametrize('agent', ['kai', 'bob', 'vera'])
def test_manifests_preserve_original_nullable_canonical_metadata(agent):
    m = models(); snapshot, _ = seed(agent)
    for kind, rows in [('Persona', snapshot['persona_rows']), ('Rule', snapshot['rule_rows']), ('Skill', snapshot['skill_rows'])]:
        for row in rows:
            original = row['boot_manifest']
            actual = getattr(m, 'Boot' + kind + 'Manifest').model_validate(original).model_dump(mode='json', exclude_unset=True)
            assert actual == original


@pytest.mark.parametrize('defect', ['unknown', 'nul', 'escape', 'absolute', 'dot', 'backslash', 'control', 'duplicate-role', 'boolean-role', 'missing-version'])
def test_persona_manifest_refuses_ambiguous_or_unsafe_metadata(defect):
    m = models(); snapshot, _ = seed(); value = copy.deepcopy(snapshot['persona_rows'][0]['boot_manifest'])
    if defect == 'unknown': value['can_write'] = True
    elif defect == 'nul': value['identity_text'] += '\x00'
    elif defect == 'escape': value['body_ref'] = '../outside.md'
    elif defect == 'absolute': value['body_ref'] = '/outside.md'
    elif defect == 'dot': value['body_ref'] = '.'
    elif defect == 'backslash': value['body_ref'] = 'folder\\outside.md'
    elif defect == 'control': value['body_ref'] = 'body\n.md'
    elif defect == 'duplicate-role': value['functional_roles'] *= 2
    elif defect == 'boolean-role': value['functional_roles'] = [True]
    else: del value['version']
    with pytest.raises(ValueError): m.BootPersonaManifest.model_validate(value)


def request(kind):
    common = {'expected_revision': 0, 'state': 'active', 'source_reference': 'PUBLIC enactment provenance'}
    if kind == 'agent': return dict(common, actor_id=uid('actor-kai'), identity_id=uid('identity-kai'), persona_id=uid('persona-kai'), persona_revision=1, functional_roles=['lead'])
    if kind == 'entry': return dict(common, binding_id=uid('entry'), subject_kind='agent', actor_id=uid('actor-kai'), role_slug=None, entry_kind='skill', entry_scope_id=uid('scope'), skill_id=uid('skill'), rule_id=None, bound_revision=1, priority=10)
    return dict(common, publication_id=uid('publication'), catalogue_scope_id=uid('catalogue'), entry_kind='skill', entry_id=uid('skill'), entry_revision=1)


def validate(kind, value):
    return getattr(models(), {'agent': 'BootAgentBindRequest', 'entry': 'BootEntryBindRequest', 'publication': 'BootPublicationRequest'}[kind]).model_validate(value)


@pytest.mark.parametrize('kind', ['agent', 'entry', 'publication'])
def test_binding_requests_preserve_exact_revision_and_declared_provenance(kind):
    value = request(kind)
    assert validate(kind, value).model_dump(mode='json', exclude_unset=True) == value


@pytest.mark.parametrize('kind,pin', [('agent','persona_revision'), ('entry','bound_revision'), ('publication','entry_revision')])
@pytest.mark.parametrize('alias', [True, 1.0, '1'])
def test_binding_revision_numbers_refuse_boolean_float_and_text_aliases(kind, pin, alias):
    value = request(kind); value[pin] = alias
    with pytest.raises(ValueError): validate(kind, value)


@pytest.mark.parametrize('kind', ['agent', 'entry', 'publication'])
@pytest.mark.parametrize('defect', ['authority', 'expected-bool', 'nul-source'])
def test_callers_cannot_supply_provenance_authority_or_coerced_heads(kind, defect):
    value = request(kind)
    if defect == 'authority': value['enacted_by_principal'] = uid('owner')
    elif defect == 'expected-bool': value['expected_revision'] = True
    else: value['source_reference'] += '\x00'
    with pytest.raises(ValueError): validate(kind, value)


@pytest.mark.parametrize('defect', ['two-subjects', 'missing-actor', 'two-entries', 'missing-entry', 'project-actor', 'role-without-slug'])
def test_entry_bindings_require_exactly_one_typed_subject_and_canonical_entry(defect):
    value = request('entry')
    if defect == 'two-subjects': value['role_slug'] = 'lead'
    elif defect == 'missing-actor': value['actor_id'] = None
    elif defect == 'two-entries': value['rule_id'] = uid('rule')
    elif defect == 'missing-entry': value['skill_id'] = None
    elif defect == 'project-actor': value['subject_kind'] = 'project'
    else: value.update(subject_kind='functional_role', actor_id=None)
    with pytest.raises(ValueError): validate('entry', value)
