"""R423 actual signing input/writer path; public synthetic signatures only."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_manual_manifest_r407 import required as generator, seed as archive_seed, SOURCE, PODMAN_POLICY
from test_manual_prerequisite_r407 import required as writer, seed as writer_seed


@pytest.mark.parametrize('observed', ['6.1.3', '6.0.2', '7.0.0'])
def test_real_generator_emits_agreed_policy_and_records_version_without_equality_gate(tmp_path, observed):
    inventory, assets = archive_seed(tmp_path)
    policy = copy.deepcopy(PODMAN_POLICY); before = copy.deepcopy(policy)
    result = generator().build_manifest(source_revision=SOURCE, image_inventory=inventory,
        artifacts=assets, podman_policy=policy, podman_tested_version=observed)
    assert result['podman'] == PODMAN_POLICY and policy == before
    assert result['podman_tested_version'] == observed
    assert 'supported_family' not in result['podman'] and 'tested_baseline' not in result['podman']


@pytest.mark.parametrize('observed', [None, True, '6.1', '06.1.3', '6.1.3-dev'])
def test_generator_refuses_missing_or_nonstable_observation(tmp_path, observed):
    module = generator(); inventory, assets = archive_seed(tmp_path)
    with pytest.raises(module.ManifestRefusal):
        module.build_manifest(source_revision=SOURCE, image_inventory=inventory,
            artifacts=assets, podman_tested_version=observed)


def test_signed_writer_emits_exact_five_fields_and_complete_policy_digest(tmp_path, monkeypatch):
    module = writer(monkeypatch); args, manifest, health, expected, calls, write_manifest = writer_seed(tmp_path)
    manifest['podman_tested_version'] = '7.0.0'; write_manifest()
    module.write_prerequisite(**args)
    descriptor = json.loads((args['home'] / '.cortex/prerequisite.json').read_text())
    assert descriptor['podman'] == {
        'minimum_version': '6.0.2', 'policy_sha256': hashlib.sha256(json.dumps(PODMAN_POLICY,
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest(),
        'provider': 'cortex-native-lifecycle', 'connection_name': None, 'machine_name': None}
    assert len(calls) == 1


def damage_policy(kind):
    value = copy.deepcopy(PODMAN_POLICY)
    if kind == 'family': return {'supported_family': '6.0.x', 'tested_baseline': '6.0.2'}
    if kind == 'floor': value['minimum_version'] = '5.8.2'
    elif kind == 'provider': value['provider'] = 'ambient-compose'
    elif kind == 'forged-digest': value['policy_sha256'] = '0' * 64
    elif kind == 'unknown': value['can_bypass'] = True
    elif kind == 'future-denial': value['denylist']['entries'] = [{'version':'6.1.3', 'date':'2026-10-08', 'reason':'PUBLIC denial'}]
    elif kind == 'duplicate-denial': value['denylist']['entries'] = [{'version':'6.1.3', 'date':'2026-10-07', 'reason':'PUBLIC denial'}] * 2
    elif kind == 'boolean-version': value['minimum_version'] = True
    elif kind == 'invalid-date': value['denylist']['as_of'] = '20261007'
    elif kind == 'null-reason': value['minimum_reason'] = None
    elif kind == 'nul-reason': value['minimum_reason'] += '\x00'
    elif kind == 'missing-key': del value['linux_acquisition']
    return value


BAD = ['family','floor','provider','forged-digest','unknown','future-denial','duplicate-denial','boolean-version','invalid-date','null-reason','nul-reason','missing-key']


@pytest.mark.parametrize('kind', BAD)
def test_generator_refuses_old_family_and_malformed_forged_policy(tmp_path, kind):
    module = generator(); inventory, assets = archive_seed(tmp_path)
    with pytest.raises(module.ManifestRefusal):
        module.build_manifest(source_revision=SOURCE, image_inventory=inventory,
            artifacts=assets, podman_policy=damage_policy(kind), podman_tested_version='6.1.3')


@pytest.mark.parametrize('kind', BAD)
def test_signature_valid_writer_refuses_bad_policy_before_health_or_publication(tmp_path, monkeypatch, kind):
    module = writer(monkeypatch); args, manifest, health, expected, calls, write_manifest = writer_seed(tmp_path)
    manifest['podman'] = damage_policy(kind); write_manifest()
    with pytest.raises(module.PrerequisiteRefusal) as caught: module.write_prerequisite(**args)
    assert caught.value.code == 'cortex_podman_unsupported'
    assert calls == [] and not (args['home'] / '.cortex').exists()


def test_changed_existing_descriptor_cannot_forge_policy_digest(tmp_path, monkeypatch):
    module = writer(monkeypatch); args, manifest, health, expected, calls, write_manifest = writer_seed(tmp_path)
    module.write_prerequisite(**args)
    target = args['home'] / '.cortex/prerequisite.json'
    value = json.loads(target.read_text()); value['podman']['policy_sha256'] = '0' * 64
    body = (json.dumps(value) + '\n').encode(); target.write_bytes(body)
    with pytest.raises(module.PrerequisiteRefusal) as caught: module.write_prerequisite(**args)
    assert caught.value.code == 'cortex_instance_mismatch'
    assert target.read_bytes() == body
