"""Composition over actual private records; native components explicitly intercepted."""
import copy
import hmac
import importlib
import json
from pathlib import Path
import secrets
import time
from types import SimpleNamespace

import pytest
from test_cm2_linux_delivery_journal import fixture
from test_cm2_linux_engine_observation import POLICY

CASES = ['valid', 'provider-refused', 'security-refused', 'catalog-refused', 'private-exception',
         'distro', 'provider-schema', 'provider-extra', 'provider-version', 'provider-not-bottle',
         'provider-keg', 'provider-checksum', 'provider-url', 'security-schema', 'security-extra',
         'security-installation', 'storage-config', 'storage-custody', 'rootless', 'selinux',
         'catalog-schema', 'catalog-extra', 'catalog-installation', 'source-payload', 'migrations',
         'rls', 'app-superuser', 'app-owner', 'db-instance', 'catalog-integer', 'project-binding',
         'engine-version', 'engine-remote', 'engine-cgroups', 'runtime-change', 'context-change',
         'provider-change', 'security-change', 'catalog-change', 'roster-change', 'member-extra',
         'actor-collision', 'lead-permission', 'console-permission', 'member-project',
         'rotate-lead', 'rotate-console', 'helper-digest', 'store-installation', 'store-backend',
         'missing-check', 'deadline']


def required():
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert callable(getattr(module, 'read_linux_readiness', None)), 'whole Linux readiness composition missing'
    return module


def setup(module, tmp_path, monkeypatch):
    _, journal, path, args, response, store, verify, prior = fixture(tmp_path, monkeypatch)
    assert journal.begin() == 'new'; journal.persist(response, verify=verify)
    release = json.loads((Path(__file__).parent / 'fixtures/cm2-revision4/fixtures/release.linux.json').read_text())
    engine = {'schema': 'cortex.linux-engine-readiness.v1', 'mode': 'local', 'client_version': '6.1.3',
        'server_version': None, 'connection_name': None, 'delegation': {'schema': 'cortex.linux-cgroup-readiness.v1',
        'rootless': True, 'cgroup_version': 'v2', 'cgroup_controllers': ['cpu', 'memory', 'pids']}}
    context = {'installation_id': journal.installation, 'host_os': 'linux', 'runtime_root': str(tmp_path),
        'package_root': str(tmp_path / 'package'), 'release': release, 'release_manifest_sha256': 'b' * 64,
        'helper_sha256': release['files']['bin/cortex'], 'origin': 'http://127.0.0.1:18602', 'engine': engine,
        'containers': {r: format(i + 1, '064x') for i, r in enumerate(module.custody.ROLES)}, 'network_id': 'c' * 64}
    local = {'provider': 'linuxbrew', 'linked_keg': '6.1.3', 'uid': 1001, 'identity': (1, 2),
             'executable': '/home/linuxbrew/.linuxbrew/Cellar/podman/6.1.3/bin/podman', 'environment': {}}
    provider = {'schema': 'cortex.linuxbrew-provider.v1', 'provider': 'linuxbrew', 'formula': 'homebrew/core/podman',
        'version': '6.1.3', 'linked_keg': '6.1.3', 'poured_from_bottle': True, 'bottle_sha256': 'a' * 64,
        'bottle_url': 'https://ghcr.io/v2/homebrew/core/podman/blobs/sha256:' + 'a' * 64,
        'checksum_verifier': 'Homebrew', 'scope': 'current formula metadata and installed stock keg; Homebrew verifies downloaded bottle bytes'}
    security = {'schema': 'cortex.linux-host-security.v1', 'installation_id': journal.installation,
        'rootless': True, 'storage_config_matches': True, 'storage_custody_matches': True, 'selinux': 'Enforcing'}
    project = {'scope_id': response['project_id'], 'primary_alias': 'fresh', 'primary_root': args['create_project']['repo_root']}
    catalog = {'schema': 'cortex.linux-catalog-readiness.v1', 'installation_id': journal.installation,
        'source_payload_matches': True, 'migration_checksums_match': True, 'required_rls_enabled_and_forced': True,
        'app_role_non_superuser_without_bypassrls': True, 'app_role_not_migrator_or_table_owner': True,
        'database_instance_matches': True, 'project_binding': project}
    members = {role: {'roster_revision': 3, 'principal_id': response[role]['principal_id'],
        'actor_id': '30000000-0000-4000-8000-00000000000' + ('3' if role == 'lead' else '4'),
        'scope_id': response['project_id'], 'project': 'fresh',
        'member_name': args['create_project']['lead_name'] if role == 'lead' else 'console',
        'actor_kind': 'agent' if role == 'lead' else 'service', 'role': 'lead' if role == 'lead' else 'member',
        'status': 'active', 'can_read': True, 'can_write': True, 'can_publish': role == 'lead',
        'project_root': args['create_project']['repo_root']} for role in ['lead', 'console']}
    return journal, args, response, store, context, local, provider, security, catalog, members


@pytest.mark.parametrize('case', CASES)
def test_same_runtime_both_private_records_and_every_component_must_agree_before_ready(case, tmp_path, monkeypatch):
    module = required()
    journal, args, response, store, context, local, provider, security, catalog, members = setup(module, tmp_path, monkeypatch)
    if case == 'distro': local['provider'] = 'distro'
    for name, component in [('provider', provider), ('security', security), ('catalog', catalog)]:
        if case == name + '-schema': component['schema'] = 'foreign'
        if case == name + '-extra': component['cached'] = True
    if case == 'provider-version': provider['version'] = '6.1.4'
    if case == 'provider-not-bottle': provider['poured_from_bottle'] = False
    if case == 'provider-keg': provider['linked_keg'] = '6.1.3_1'
    if case == 'provider-checksum': provider['bottle_sha256'] = 'unknown'
    if case == 'provider-url': provider['bottle_url'] = 'https://foreign.invalid/bottle'
    if case == 'security-installation': security['installation_id'] = '20000000-0000-4000-8000-000000000002'
    if case == 'storage-config': security['storage_config_matches'] = False
    if case == 'storage-custody': security['storage_custody_matches'] = False
    if case == 'rootless': security['rootless'] = 1
    if case == 'selinux': security['selinux'] = 'Permissive'
    if case == 'catalog-installation': catalog['installation_id'] = '20000000-0000-4000-8000-000000000002'
    negatives = {'source-payload': 'source_payload_matches', 'migrations': 'migration_checksums_match',
        'rls': 'required_rls_enabled_and_forced', 'app-superuser': 'app_role_non_superuser_without_bypassrls',
        'app-owner': 'app_role_not_migrator_or_table_owner', 'db-instance': 'database_instance_matches'}
    if case in negatives: catalog[negatives[case]] = False
    if case == 'catalog-integer': catalog['migration_checksums_match'] = 1
    if case == 'project-binding': catalog['project_binding']['primary_alias'] = 'foreign'
    if case == 'engine-version': context['engine']['client_version'] = '6.1.4'
    if case == 'engine-remote': context['engine']['server_version'] = '6.1.3'
    if case == 'engine-cgroups': context['engine']['delegation']['cgroup_controllers'] = ['cpu', 'memory']
    if case == 'member-extra': members['console']['cached'] = True
    if case == 'actor-collision': members['lead']['actor_id'] = members['console']['actor_id']
    if case == 'lead-permission': members['lead']['can_publish'] = False
    if case == 'console-permission': members['console']['can_publish'] = True
    if case == 'member-project': members['lead']['project'] = 'foreign'
    if case == 'helper-digest': context['helper_sha256'] = 'e' * 64
    selected_store = SimpleNamespace(installation='foreign', backend='file') if case == 'store-installation' else SimpleNamespace(installation=journal.installation, backend='keychain') if case == 'store-backend' else store
    calls = []; deadlines = []; reads = {'runtime': 0, 'context': 0, 'provider': 0, 'security': 0, 'catalog': 0, 'members': 0}
    def runtime(root, *, kos_policy, deadline):
        assert root == tmp_path and kos_policy == POLICY
        calls.append('runtime'); deadlines.append(deadline); reads['runtime'] += 1
        value = copy.deepcopy(context)
        if case == 'runtime-change' and reads['runtime'] > 1: value['network_id'] = 'd' * 64
        return value
    def engine():
        calls.append('context'); reads['context'] += 1; value = copy.deepcopy(local)
        if case == 'context-change' and reads['context'] > 1: value['identity'] = (3, 4)
        return value
    def component(name, value, **kwargs):
        calls.append(name); reads[name] += 1; deadlines.append(kwargs['deadline'])
        assert kwargs['kos_policy'] == POLICY
        if name == 'provider': assert kwargs['cortex_policy'] == context['release']['podman']
        if name == 'catalog': assert kwargs['store'] is store
        if case == name + '-refused': raise module.PrerequisiteRefusal('cortex_image_mismatch')
        if case == 'private-exception': raise RuntimeError('PRIVATE-SYNTHETIC-DO-NOT-EXPOSE')
        if case in ('rotate-lead', 'rotate-console') and name == 'provider' and reads[name] == 1:
            role = case.removeprefix('rotate-'); label = args['create_project']['lead_name'] if role == 'lead' else 'console'
            store.put('fresh', label, secrets.token_urlsafe(32), managed_by=response[role]['manager'], expires_at=response[role]['expires_at'])
        if case == 'deadline' and name == 'provider': time.sleep(.07)
        result = copy.deepcopy(value)
        if case == name + '-change' and reads[name] > 1:
            if name == 'provider': result['bottle_sha256'] = 'f' * 64
            elif name == 'security': result['storage_custody_matches'] = False
            else: result['required_rls_enabled_and_forced'] = False
        return result
    snapshots = {role: store.read('fresh', args['create_project']['lead_name'] if role == 'lead' else 'console') for role in ['lead', 'console']}
    def recipients():
        calls.append('members'); reads['members'] += 1
        for role, selected in snapshots.items():
            record = store.read('fresh', args['create_project']['lead_name'] if role == 'lead' else 'console')
            if record is None or record.metadata != selected.metadata or not hmac.compare_digest(record.token, selected.token):
                raise module.PrerequisiteRefusal('cortex_credential_refused')
        value = copy.deepcopy(members)
        if case == 'roster-change' and reads['members'] > 1: value['lead']['roster_revision'] = 4
        return value
    monkeypatch.setattr(module, 'read_linux_runtime', runtime); monkeypatch.setattr(module, '_local_engine_context', engine)
    monkeypatch.setattr(module, 'observe_linuxbrew_provider', lambda **kw: component('provider', provider, **kw))
    monkeypatch.setattr(module, 'read_linux_host_security', lambda root, **kw: component('security', security, **kw))
    monkeypatch.setattr(module, 'read_linux_catalog', lambda root, request, receipt, **kw: component('catalog', catalog, **kw))
    deadline = time.monotonic() + (.04 if case == 'deadline' else 3)
    call = lambda: module.read_linux_readiness(tmp_path, args, response, kos_policy=POLICY, store=selected_store,
        recheck_recipients=None if case == 'missing-check' else recipients, deadline=deadline)
    public = ''
    if case == 'valid':
        value = call(); public = json.dumps(value, sort_keys=True)
        expected = {'schema': 'cortex.linux-readiness.v1', 'status': 'READY', 'installation_id': journal.installation,
            'release_manifest_sha256': context['release_manifest_sha256'], 'target': 'linux-x86_64',
            'engine': {'client_version': '6.1.3', 'server_version': None, 'rootless': True, 'os': 'linux',
                'architecture': 'amd64', 'provider': 'cortex-native-lifecycle', 'mode': 'local', 'connection_name': None},
            'api_binding': {'loopback_port': 18602, 'installation_label_matches': True,
                'api_image_id': context['release']['images']['api']['config_id'], 'exact_owned_network': True, 'network_internal': True},
            'images': {r: context['release']['images'][r]['config_id'] for r in module.custody.ROLES},
            'selinux': 'Enforcing', 'signed_helper_verified': True, 'helper_sha256': context['helper_sha256'],
            'project_binding': catalog['project_binding'], **{k: catalog[k] for k in ['source_payload_matches',
                'migration_checksums_match', 'required_rls_enabled_and_forced', 'app_role_non_superuser_without_bypassrls',
                'app_role_not_migrator_or_table_owner', 'database_instance_matches']}}
        assert value == expected
        assert reads['provider'] == reads['security'] == reads['catalog'] == 2
        assert reads['runtime'] >= 8 and reads['context'] >= 8 and reads['members'] >= 8
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: call()
        public = str(error.value)
        assert 'PRIVATE-SYNTHETIC' not in public
        if case.endswith('-refused'): assert error.value.code == 'cortex_image_mismatch'
        if case.startswith('rotate-'): assert error.value.code == 'cortex_credential_refused'
        if case == 'deadline': assert reads['provider'] == 1 and reads['security'] == reads['catalog'] == 0
        if case in ('distro', 'store-installation', 'store-backend', 'missing-check'): assert reads['provider'] == reads['security'] == reads['catalog'] == 0
    assert all(d == deadline for d in deadlines)
    if any(response[role + '_token'] in public for role in ['lead', 'console']):
        raise AssertionError('private recipient value entered output')
    assert not (tmp_path / 'connection.json').exists() and not (tmp_path / 'descriptor.json').exists()


@pytest.mark.parametrize('deadline', [True, float('nan'), float('inf'), 0, -1])
def test_bad_whole_deadline_refuses_before_components_or_recipient_reads(deadline, tmp_path, monkeypatch):
    module = required()
    for name in ['read_linux_runtime', '_local_engine_context', 'observe_linuxbrew_provider', 'read_linux_host_security', 'read_linux_catalog']:
        monkeypatch.setattr(module, name, lambda *a, **kw: pytest.fail('bad deadline reached component'))
    with pytest.raises(module.PrerequisiteRefusal):
        module.read_linux_readiness(tmp_path, {}, {}, kos_policy=POLICY, store=None,
            recheck_recipients=lambda: pytest.fail('bad deadline reached recipients'), deadline=deadline)
